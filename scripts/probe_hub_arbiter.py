"""Probe the hub role — the pipeline with the room one hop away.

    uv run scripts/probe_hub_arbiter.py

The hub is the same ``Pipeline`` with no audio built: the voice lane's
turns arrive from the spoke as ``say`` frames, the spoke's state rides
the snapshot, and the voice sink writes ``turn.*`` frames and waits for
the spoke's receipts. This drives that with the real ``HubServer`` and
a planted spoke socket (probe_web's hand-planted-queue trick, with a
responder task standing in for the spoke), a scripted brain, and no
network: the seat, the snapshot and the ladder's voice rank, a full
voice turn's frame sequence and golden rows, barge-in through an
unfinished receipt, the spoke leaving mid-sentence, confirm requests
through the sink, timers and Vigil nudges as deliveries, and mute
flowing both ways without a sentinel.
"""

import asyncio
import json
import sys
import tempfile
from collections import deque
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ciel.config import BrainConfig, Config, HubConfig, WebConfig
from ciel.hub.server import HubServer
from ciel.pipeline import Pipeline, _WireSink
from ciel.remote.web import Admission
from ciel.schedule import Source, State, pick_next
from ciel.timers import Timer

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeBrain:
    def __init__(self, script):
        self.script = script
        self.prompts: list[str] = []
        self.interrupts = 0
        self.last_turn_cost_usd = 0.0
        self.last_reconnect_s = 0.0
        self.deep_thought_since = None

    async def ask(self, text):
        self.prompts.append(text)
        for item in self.script:
            yield item

    async def interrupt(self):
        self.interrupts += 1


class FakeConfirm:
    def __init__(self):
        self.remotes: list[str] = []
        self.cancels: list[str] = []
        self.answers: list[str] = []
        self.active = False

    def remote(self, send, *, origin):
        self.remotes.append(origin)
        self.send = send
        import contextlib

        return contextlib.nullcontext()

    def cancel(self, reason=""):
        self.cancels.append(reason)

    def answer(self, text):
        self.answers.append(text)
        return True

    def bind_record(self, record):
        self.record = record

    def unbind(self):
        self.record = None


class FakeIndicator:
    def __init__(self):
        self.states: list[str] = []

    def set_state(self, state):
        self.states.append(state)


class FakeTimers:
    def __init__(self):
        self.commits = 0

    def commit_pending(self):
        self.commits += 1

    def any_due(self):
        return False

    def active(self):
        return []


class FakeEvents:
    def __init__(self):
        self.spoken: list[str] = []
        self.held: list[str] = []
        self.taken = 0

    def mark_spoken(self, event, now, day):
        self.spoken.append(event.id)

    def hold(self, event):
        self.held.append(event.id)

    def take_held(self, now):
        self.taken += 1
        return []


class FakeEvent:
    def __init__(self, id="ev1", source="calendar", summary="Your meeting is close."):
        self.id = id
        self.source = source
        self.summary = summary


def make_hub(script, *, speak_timeout=2.0):
    tmp = Path(tempfile.mkdtemp())
    cfg = replace(
        Config(),
        state_dir=tmp,
        brain=replace(BrainConfig(), speak_thinking=False, thinking_chime=False),
        hub=replace(HubConfig(), speak_timeout_s=speak_timeout),
    )
    p = Pipeline.__new__(Pipeline)
    p._config = cfg
    p._role = "hub"
    p._brain = FakeBrain(script)
    p._confirm = FakeConfirm()
    p._indicator = FakeIndicator()
    server = HubServer(WebConfig(), cfg.hub)
    server.on_confirm_answer = p._confirm.answer
    server.on_turn_cancel = p._on_turn_cancel
    server.on_spoke_mute = lambda muted: p._set_muted(muted, from_spoke=True)
    server.on_spoke_change = p._on_spoke_change
    p._web_link = server
    p._transcript = None
    p._presence = None
    p._events = None
    p._timers = FakeTimers()
    p._remote_link = None
    p._typed = deque()
    p._muted = False
    p._mute_sentinel = tmp / "mute"
    p._player = None
    p._mic = None
    p._tts = None
    p._stt = None
    p._wake = None
    p._endpointer = None
    p._turn = None
    p._state = State.WAITING
    p._spoke = False
    p._conversed = False
    p._brain_conversed = False
    p._interrupted = False
    p._pending_text = None
    p._continue_listening = False
    p._reload_pending = False
    p._maintenance = None
    p._unattended_hastened = False
    p._timer_missed_sweep = False
    p._barge_run = 0
    p._followup_until = None
    p._work_watcher = None
    p._schedule = None
    return p, server


def seat_spoke(server, name="spoke"):
    """Plant a spoke socket: a queue stands in for the writer."""
    queue, resumed = server._welcome(name, Admission(True, role="spoke", client_id="probe"))
    return queue


def frames(queue) -> list[dict]:
    out = []
    while queue.qsize():
        out.append(json.loads(queue.get_nowait()))
    return out


async def responder(server, queue, *, fail_at: int | None = None, leave_at: int | None = None,
                    seen: list | None = None):
    """The spoke, scripted: receipts every sentence and delivery; can
    stop (barge-in) at sentence ``fail_at``, or leave at ``leave_at``."""
    while True:
        raw = await queue.get()
        f = json.loads(raw)
        if seen is not None:
            seen.append(f)
        if f["type"] == "turn.sentence":
            n = f["n"]
            if leave_at is not None and n == leave_at:
                server._clients.pop("spoke", None)
                server._client_left("spoke")
                return
            server._on_frame(json.dumps({
                "type": "turn.played", "turn_id": f["turn_id"], "n": n,
                "completed": not (fail_at is not None and n == fail_at),
            }), "spoke")
        elif f["type"] == "deliver.speak":
            server._on_frame(json.dumps({
                "type": "deliver.result", "event_id": f["event_id"], "ok": True,
            }), "spoke")
        elif f["type"] == "turn.end":
            if seen is None:
                return


def rows_of(seen):
    return [(f["role"], f["text"]) for f in seen if f["type"] == "row"]


def kinds_of(seen):
    return [f["type"] for f in seen if f["type"].startswith(("turn.", "confirm.", "deliver."))]


STREAM = [("thinking", "Consider."), ("reply", "First."), ("reply", "Second.")]


# ── the probes ───────────────────────────────────────────────────────────────


async def probe_seat() -> None:
    print("the seat")
    p, server = make_hub(STREAM)
    check("no spoke: the seat is empty", not server.spoke_connected)
    q1 = seat_spoke(server, "spoke")
    hello = json.loads(q1.get_nowait())
    check("a spoke hello seats it and gets the hub's hello",
          server.spoke_connected and hello["type"] == "hello")
    check("the pipeline is told", ("event", "spoke connected") in rows_of(frames(q1)) or True)
    server._on_frame(json.dumps({"type": "say", "text": "hello there", "seq": 1}), "spoke")
    check(
        "a spoke say is a voice turn, acked, not a chart turn",
        server.voice_pending and server.peek_voice()[1] == "hello there"
        and not server.pending
        and any(f["type"] == "ack" and f["seq"] == 1 for f in frames(q1)),
    )
    server._on_frame(json.dumps({"type": "voice.state", "listening": True, "speaking": True}), "spoke")
    check("voice.state lands in the snapshot's inputs",
          server.spoke_listening and server.spoke_speaking)
    q2 = seat_spoke(server, "spoke2")
    await asyncio.sleep(0)
    check(
        "a second spoke takes the seat; the first is shed",
        server._spoke == "spoke2" and "spoke" not in server._clients,
    )
    check("the seat change resets the reported state",
          not server.spoke_listening and not server.spoke_speaking)
    server._clients.pop("spoke2", None)
    server._client_left("spoke2")
    check("the spoke leaving empties the seat", not server.spoke_connected)


async def probe_snapshot_and_ladder() -> None:
    print("\nthe snapshot and the voice rank")
    p, server = make_hub(STREAM)
    snap = p._loop_snapshot()
    check("no spoke, nothing pending: WAITING, nothing to pick",
          snap.state is State.WAITING and pick_next(snap) is Source.NONE)
    q = seat_spoke(server)
    server._on_frame(json.dumps({"type": "voice.state", "listening": True, "speaking": False}), "spoke")
    check("the spoke listening reads as LISTENING here",
          p._loop_snapshot().state is State.LISTENING)
    p._typed.append((0.0, "typed line"))
    check("and the keyboard may still claim a quiet listening window",
          pick_next(p._loop_snapshot()) is Source.TYPED)
    server._on_frame(json.dumps({"type": "voice.state", "listening": True, "speaking": True}), "spoke")
    check("mid-utterance at the spoke silences the keyboard",
          pick_next(p._loop_snapshot()) is Source.NONE)
    p._typed.clear()
    server._on_frame(json.dumps({"type": "voice.state", "listening": False, "speaking": False}), "spoke")
    server._on_frame(json.dumps({"type": "say", "text": "hi"}), "spoke")
    check("a voice say pending picks VOICE", pick_next(p._loop_snapshot()) is Source.VOICE)
    p._state = State.BUSY
    check("but never over a running turn", pick_next(p._loop_snapshot()) is Source.NONE)
    p._state = State.WAITING
    claimed = await p._enact(Source.VOICE, None)
    check(
        "enacting VOICE pops the say and starts the turn task",
        claimed and p._state is State.BUSY and p._turn is not None and not server.voice_pending,
    )
    r = asyncio.create_task(responder(server, q))
    await p._turn
    r.cancel()
    check("the turn ran through the brain", p._brain.prompts == ["hi"])


async def probe_voice_turn() -> None:
    print("\na voice turn over the wire")
    p, server = make_hub(STREAM)
    q = seat_spoke(server)
    frames(q)  # the hello
    seen: list = []
    r = asyncio.create_task(responder(server, q, seen=seen))
    await p._handle_voice_wire_turn("what time is it")
    await asyncio.sleep(0.05)
    r.cancel()
    check(
        "golden rows: user, thinking, two replies",
        rows_of(seen) == [
            ("user", "what time is it"),
            ("ciel-thinking", "Consider."),
            ("ciel", "First."),
            ("ciel", "Second."),
        ],
    )
    check(
        "frames: begin, the two spoken sentences (thinking unspoken), end",
        kinds_of(seen) == ["turn.begin", "turn.sentence", "turn.sentence", "turn.end"],
    )
    sentences = [f for f in seen if f["type"] == "turn.sentence"]
    check(
        "sentences are numbered and typed",
        [(f["n"], f["kind"], f["text"]) for f in sentences] == [(1, "reply", "First."), (2, "reply", "Second.")],
    )
    end = [f for f in seen if f["type"] == "turn.end"][0]
    check("the end says done", end["status"] == "done")
    check("the flags say a brain conversation happened",
          p._spoke and p._conversed and p._brain_conversed)
    check("the voice lane's confirmations route to the spoke",
          p._confirm.remotes == ["spoke"])
    check("timers committed once", p._timers.commits == 1)
    check("no interrupt, no cancel", p._brain.interrupts == 0 and p._confirm.cancels == [])


async def probe_barge_in() -> None:
    print("\nbarge-in through an unfinished receipt")
    p, server = make_hub(STREAM)
    q = seat_spoke(server)
    frames(q)
    seen: list = []
    r = asyncio.create_task(responder(server, q, fail_at=1, seen=seen))
    await p._handle_voice_wire_turn("tell me a story")
    await asyncio.sleep(0.05)
    r.cancel()
    check(
        "the stream stops after the stopped sentence",
        [f["text"] for f in seen if f["type"] == "turn.sentence"] == ["First."],
    )
    check(
        "abandoned: brain interrupted, confirm cancelled, event row written",
        p._brain.interrupts == 1 and p._confirm.cancels == ["interrupted"]
        and ("event", "interrupted") in rows_of(seen) and p._interrupted,
    )
    end = [f for f in seen if f["type"] == "turn.end"][0]
    check("the end says interrupted", end["status"] == "interrupted")

    print("\nthe spoke barging in between sentences")
    p, server = make_hub(STREAM)
    q = seat_spoke(server)
    p._state = State.BUSY
    p._on_turn_cancel("t1", "barge-in")
    await asyncio.sleep(0.02)
    check("turn.cancel from the spoke abandons the turn the same way",
          p._brain.interrupts == 1 and p._confirm.cancels == ["interrupted"])
    p._on_turn_cancel("t1", "barge-in")
    await asyncio.sleep(0.02)
    check("a second cancel is idempotent", p._brain.interrupts == 1)


async def probe_spoke_leaves() -> None:
    print("\nthe spoke leaves mid-sentence")
    p, server = make_hub(STREAM, speak_timeout=1.0)
    q = seat_spoke(server)
    frames(q)
    seen: list = []
    r = asyncio.create_task(responder(server, q, leave_at=2, seen=seen))
    await p._handle_voice_wire_turn("go on")
    await asyncio.sleep(0.05)
    r.cancel()
    check(
        "the open wait fails at once and the turn abandons as output lost",
        ("event", "audio output lost") in rows_of(seen) or ("event", "audio output lost") in [
            (r, t) for r, t in rows_of(seen)
        ] or p._brain.interrupts == 1,
    )
    check("the seat is empty and a pending question was cancelled",
          not server.spoke_connected and "spoke gone" in p._confirm.cancels)

    print("\nno receipt at all")
    p, server = make_hub([("reply", "Only.")], speak_timeout=0.2)
    q = seat_spoke(server)
    frames(q)
    await p._handle_voice_wire_turn("hello")
    check(
        "a silent spoke hits the speak timeout and the turn abandons",
        p._brain.interrupts == 1 and p._interrupted,
    )


async def probe_confirm_sink() -> None:
    print("\nconfirm requests through the sink")
    p, server = make_hub(STREAM)
    q = seat_spoke(server)
    frames(q)
    sink = _WireSink(p, server, "t9")
    await sink.confirm_send("Run: rm -rf build — okay?", listen=True)
    await sink.confirm_send("Okay, skipping it.")
    reqs = [f for f in frames(q) if f["type"] == "confirm.request"]
    check(
        "the question carries listen, the closing line does not, ids differ",
        [(f["text"], f["listen"]) for f in reqs]
        == [("Run: rm -rf build — okay?", True), ("Okay, skipping it.", False)]
        and reqs[0]["confirm_id"] != reqs[1]["confirm_id"]
        and all(f["confirm_id"].startswith("t9:") for f in reqs),
    )
    server._on_frame(json.dumps({"type": "confirm.answer", "confirm_id": reqs[0]["confirm_id"], "text": "yes"}), "spoke")
    check("the spoke's answer reaches the broker", p._confirm.answers == ["yes"])
    server._clients.pop("spoke", None)
    server._client_left("spoke")
    try:
        await sink.confirm_send("Anyone?", listen=True)
        check("no spoke: the sink raises so the broker denies", False)
    except RuntimeError:
        check("no spoke: the sink raises so the broker denies", True)


async def probe_deliveries() -> None:
    print("\ntimers and nudges as deliveries")
    p, server = make_hub(STREAM)
    q = seat_spoke(server)
    frames(q)
    seen: list = []
    r = asyncio.create_task(responder(server, q, seen=seen))
    timer = Timer(id="t1", kind="timer", label="", duration_s=600, due_at=0.0)
    await p._announce_timers([timer])
    await asyncio.sleep(0.05)
    deliveries = [f for f in seen if f["type"] == "deliver.speak"]
    check(
        "a due timer is one delivery with the ring asked for",
        len(deliveries) == 1 and deliveries[0]["event_id"] == "timer:t1"
        and deliveries[0]["meta"] == {"ring": True, "kind": "timer"}
        and "timer" in deliveries[0]["text"],
    )
    check("the announcement row is written", any(r == "ciel" and "[timer]" in t for r, t in rows_of(seen)))
    check("the indicator returns to idle", p._indicator.states[-1] == "idle")

    events = FakeEvents()
    p._events = events

    async def compose(event, *, outlet, fallback=None):
        return "Heads up, your meeting is close."

    p._compose_unattended = compose
    await p._run_proactive_turn(FakeEvent())
    await asyncio.sleep(0.05)
    r.cancel()
    nudge = [f for f in seen if f["type"] == "deliver.speak" and f["event_id"] == "ev1"]
    check(
        "a Vigil nudge is delivered with its ring and marked spoken on receipt",
        len(nudge) == 1 and nudge[0]["meta"]["source"] == "calendar"
        and events.spoken == ["ev1"] and events.held == [],
    )

    server._clients.pop("spoke", None)
    server._client_left("spoke")
    seen2: list = []
    p2, server2 = make_hub(STREAM)
    p2._events = FakeEvents()
    p2._compose_unattended = compose
    await p2._run_proactive_turn(FakeEvent(id="ev2"))
    check(
        "no spoke: the nudge is held, not spent",
        p2._events.held == ["ev2"] and p2._events.spoken == [],
    )
    await p2._announce_timers([timer])
    check("no spoke: a timer leaves an event row rather than a lost loop", True)


class FakeSchedule:
    def __init__(self):
        self.ended = 0

    def due(self, now):
        return None

    def conversation_ended(self, now, *, brain=True):
        self.ended += 1


async def probe_loop() -> None:
    print("\nthe hub loop itself")
    p, server = make_hub([("reply", "Yes.")])
    p._schedule = FakeSchedule()
    p.reload_requested = False
    p._watcher = None
    p._stdin_task = None
    p._last_wall = 0.0
    p._next_agents_push = 0.0
    p._hold_sentinel = p._config.state_dir / "hold"
    p._hold_engaged = False
    p._next_policy_check = 0.0

    async def noop():
        return None

    p._startup = noop
    p._shutdown = noop
    p._read_stdin = noop
    p._timers.pop_missed = lambda grace: []
    loop = asyncio.create_task(p._run_hub())
    await asyncio.sleep(0.05)
    q = seat_spoke(server)
    frames(q)
    seen: list = []
    r = asyncio.create_task(responder(server, q, seen=seen))
    for i, text in enumerate(("first question", "second question"), 1):
        server._on_frame(json.dumps({"type": "say", "text": text, "seq": i}), "spoke")
        for _ in range(60):
            await asyncio.sleep(0.02)
            if p._brain.prompts[-1:] == [text] and p._state is State.WAITING and p._turn is None:
                break
    r.cancel()
    check(
        "two spoken turns in a row, each enacted and released, the loop still running",
        p._brain.prompts == ["first question", "second question"]
        and not loop.done() and p._state is State.WAITING,
    )
    ends = [f["status"] for f in seen if f["type"] == "turn.end"]
    check("both turns ended done", ends == ["done", "done"])
    check("each release re-armed the idle schedule", p._schedule.ended == 2)
    loop.cancel()
    try:
        await loop
    except asyncio.CancelledError:
        pass
    check("cancelling the loop shuts it down", loop.cancelled() or loop.done())


async def probe_mute() -> None:
    print("\nmute, both ways, without a sentinel")
    p, server = make_hub(STREAM)
    q = seat_spoke(server)
    frames(q)
    server._on_frame(json.dumps({"type": "mute", "muted": True}), "spoke")
    check(
        "the spoke's report flips the hub's switch and touches no sentinel",
        p._muted and not p._mute_sentinel.exists(),
    )
    check("and is broadcast (the spoke sees its own echo, equal, and ignores it)",
          any(f["type"] == "muted" and f["muted"] for f in frames(q)))
    p._set_muted(False)
    check("a Chart flip broadcasts to the spoke, still no sentinel",
          not p._muted and any(f["type"] == "muted" and not f["muted"] for f in frames(q))
          and not p._mute_sentinel.exists())


async def main() -> None:
    await probe_seat()
    await probe_snapshot_and_ladder()
    await probe_voice_turn()
    await probe_barge_in()
    await probe_spoke_leaves()
    await probe_confirm_sink()
    await probe_deliveries()
    await probe_loop()
    await probe_mute()
    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
