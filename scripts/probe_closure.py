"""Fake-driven verification of Closure (reflection + rotation) and Atlas.

    uv run python scripts/probe_closure.py

Everything runs on fakes and fake clocks — no audio hardware, no model, no
ten-minute waits. Covers the invariants that make the continuity design
safe: rotation is guaranteed regardless of reflection's fate, the turn lock
survives every exit path, rotation discards the old session and rehydrates
fresh indexes, project writes are atomic, and suppression keeps a reflection
from speaking into an empty room.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from contextlib import aclosing
from pathlib import Path

from ciel.brain.agent import Brain
from ciel.config import Config, JournalConfig, ProjectsConfig, VoiceConfig
from ciel.confirm import VoiceConfirmBroker
from ciel.pipeline import IdleSchedule
from ciel.projects import ProjectStore

CHECKS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        sys.exit(1)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeResult:
    """Duck-typed ResultMessage — isinstance is checked, so we subclass."""


def make_result(session_id: str, cost: float):
    from claude_agent_sdk import ResultMessage

    try:
        return ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id=session_id, total_cost_usd=cost,
        )
    except TypeError:
        # Field set drifted across SDK versions; build by __new__ and attrs.
        msg = ResultMessage.__new__(ResultMessage)
        for key, value in dict(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id=session_id, total_cost_usd=cost,
            usage=None, result=None,
        ).items():
            try:
                object.__setattr__(msg, key, value)
            except Exception:
                setattr(msg, key, value)
        return msg


class FakeClient:
    """Scripted stand-in for ClaudeSDKClient."""

    def __init__(self, results: list, stall: asyncio.Event | None = None) -> None:
        self.results = list(results)
        self.stall = stall
        self.queries: list[str] = []
        self.interrupted = False
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def interrupt(self) -> None:
        self.interrupted = True
        if self.stall is not None:
            self.stall.set()

    async def receive_response(self):
        if self.stall is not None and not self.stall.is_set():
            await self.stall.wait()
        yield self.results.pop(0)


def make_brain(tmp: Path, results: list, stall=None, index_provider=None) -> Brain:
    config = Config()
    object.__setattr__(config, "state_dir", tmp)  # frozen dataclass, test-only
    brain = Brain(config, memory_index_provider=index_provider)
    client = FakeClient(results, stall)
    brain._client = client  # bypass real connect
    brain._sessions._path = tmp / "session.json"
    return brain


async def main() -> None:
    print("IdleSchedule:")
    schedule = IdleSchedule(60.0, 600.0)
    check("nothing due before arming", schedule.due(1e9) is None)
    schedule.conversation_ended(1000.0)
    check("nothing due immediately", schedule.due(1010.0) is None)
    check("reflect due at +60", schedule.due(1061.0) == "reflect")
    check("reflect fires once", schedule.due(1062.0) is None)
    check("rotate not due early", schedule.due(1300.0) is None)
    check("rotate due at +600 after reflect consumed", schedule.due(1601.0) == "rotate")
    check("rotate fires once", schedule.due(1700.0) is None)

    # Failed reflection still rotates: the schedule tracked the *attempt* —
    # consuming "reflect" is all it takes, success is never consulted.
    schedule.conversation_ended(2000.0)
    check("reflect consumed even if the attempt will fail",
          schedule.due(2061.0) == "reflect")
    check("failed reflection still rotates", schedule.due(2601.0) == "rotate")

    # Re-arm moves BOTH deadlines: rotation counts from the latest end.
    schedule.conversation_ended(3000.0)
    schedule.due(3061.0)  # consume the first conversation's reflect
    schedule.conversation_ended(3500.0)  # user came back; new conversation ends
    check("re-armed reflect fires for the new conversation",
          schedule.due(3561.0) == "reflect")
    check("re-arm defers rotation past the old deadline",
          schedule.due(3601.0) is None)  # old rotate_at was 3600 — superseded
    check("rotation is +window from the latest end", schedule.due(4101.0) == "rotate")

    no_reflect = IdleSchedule(None, 600.0)
    no_reflect.conversation_ended(0.0)
    check("disabled reflection still rotates", no_reflect.due(601.0) == "rotate")

    print("ProjectStore (Atlas):")
    tmp = Path(tempfile.mkdtemp(prefix="ciel-atlas-"))
    store = ProjectStore(tmp / "projects", max_state_chars=200, log_tail=3)
    project = store.write("Wake Word", "attempt three, threshold 0.7",
                          description="Custom wake word training")
    check("create via write", project.name == "wake-word" and project.status == "active")
    check("slug stability", store.get("wake word") is not None
          and store.get("Wake-Word") is not None)
    store.write("wake word", "attempt four")
    fetched = store.get("wake word")
    check("update replaces state, keeps identity",
          fetched.state == "attempt four"
          and fetched.description == "Custom wake word training")
    for i in range(5):
        store.append_log("wake word", f"milestone {i}")
    fetched = store.get("wake word")
    check("log tail trims to 3", len(fetched.log) == 3 and "milestone 4" in fetched.log[-1])
    check("disk keeps the full log", len(store.all()[0].log) == 5)
    check("log entries carry ISO timestamps", fetched.log[0][:4].isdigit()
          and "T" in fetched.log[0][:20])
    try:
        store.write("wake word", "x" * 500)
        check("oversize state raises", False)
    except ValueError as exc:
        check("oversize state raises", "Condense" in str(exc))
    check("oversize refusal left previous state intact",
          store.get("wake word").state == "attempt four")
    index = store.index_prompt()
    check("index lists the project with status and recency",
          "wake-word (active)" in index and "ago" in index)

    # Atomicity: a crash between temp-write and replace must leave the old
    # file readable. Simulate by breaking os.replace for one call.
    import ciel.memory.store as mstore
    real_replace = mstore.os.replace
    def broken_replace(a, b):
        raise OSError("simulated crash")
    mstore.os.replace = broken_replace
    try:
        store.write("wake word", "should never land")
        crashed = False
    except OSError:
        crashed = True
    finally:
        mstore.os.replace = real_replace
    check("failed write crashes loudly", crashed)
    check("failed write left previous state intact",
          store.get("wake word").state == "attempt four")
    shutil.rmtree(tmp)

    print("Brain cost + rotation:")
    tmp = Path(tempfile.mkdtemp(prefix="ciel-closure-"))
    versions = ["v1"]
    brain = make_brain(tmp, [make_result("s1", 0.05)],
                       index_provider=lambda: versions[-1])
    async with aclosing(brain.ask("hello")) as stream:
        async for _ in stream:
            pass
    check("first session cost", abs(brain.total_cost_usd - 0.05) < 1e-9
          and abs(brain.last_turn_cost_usd - 0.05) < 1e-9)
    check("session file saved", (tmp / "session.json").exists())

    # Rotation: swap the fake connect to install a fresh FakeClient.
    fresh = FakeClient([make_result("s2", 0.02)])
    async def fake_connect_locked(mcp_servers=None, extra_tools=None):
        brain._client = fresh
        options = brain._build_options(mcp_servers, extra_tools)
        fresh.options = options
    brain._connect_locked = fake_connect_locked
    versions.append("v2")
    await brain.rotate()
    check("rotation cleared the session file", not (tmp / "session.json").exists())
    check("rotation rehydrated the index", "v2" in fresh.options.system_prompt)
    async with aclosing(brain.ask("again")) as stream:
        async for _ in stream:
            pass
    check("cost across rotation: total", abs(brain.total_cost_usd - 0.07) < 1e-9)
    check("cost across rotation: last turn never negative",
          abs(brain.last_turn_cost_usd - 0.02) < 1e-9)

    print("turn lock lifecycle:")
    # Path 1: interrupt-hastened reflection releases the lock cleanly.
    stall = asyncio.Event()
    brain = make_brain(tmp, [make_result("s3", 0.01), make_result("s3", 0.02)], stall)
    reflection = asyncio.create_task(brain.reflect("(check-in)"))
    await asyncio.sleep(0.05)
    check("reflection holds the lock", brain._turn_lock.locked())
    await brain.interrupt()  # fake sets the stall event → stream completes
    await asyncio.wait_for(reflection, 2.0)
    check("interrupt releases the lock", not brain._turn_lock.locked())
    check("no drain debt after consumed reflection", not brain._needs_drain)
    async with aclosing(brain.ask("next")) as stream:
        async for _ in stream:
            pass
    check("user turn proceeds after reflection",
          brain._client.queries == ["(check-in)", "next"])

    # Path 2: cancellation (the wait_for timeout path) releases the lock.
    stall2 = asyncio.Event()
    brain2 = make_brain(tmp, [make_result("s4", 0.01)], stall2)
    reflection2 = asyncio.create_task(brain2.reflect("(check-in)"))
    await asyncio.sleep(0.05)
    reflection2.cancel()
    try:
        await reflection2
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.05)  # let aclosing finalize the generator
    check("cancellation releases the lock", not brain2._turn_lock.locked())

    # Path 3: abandoning ask() without aclosing would deadlock; with it, not.
    stall3 = asyncio.Event()
    stall3.set()
    brain3 = make_brain(tmp, [make_result("s5", 0.01)], stall3)
    async with aclosing(brain3.ask("hi")) as stream:
        async for _ in stream:
            break  # abandon immediately
    check("early break with aclosing releases the lock",
          not brain3._turn_lock.locked())
    shutil.rmtree(tmp)

    print("suppression:")
    broker = VoiceConfirmBroker(Config())
    with broker.suppress():
        check("suppressed confirmation denies instantly",
              await broker.ask("Run: rm -rf / — okay?") is False)
        with broker.suppress():
            pass
        check("nesting keeps suppression", broker._suppressed == 1)
    check("suppression lifts after the block", broker._suppressed == 0)
    try:
        with broker.suppress():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("suppression survives exceptions", broker._suppressed == 0)

    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
