"""Probe the world table (Phase Space) — facts, freshness, the block, the wire.

    uv run scripts/probe_world.py

The world is the one place everything Ciel holds true is written, so the
things worth pinning are the things every reader depends on: a fact is
what was observed at the time it was observed (a re-observation of the
same value is not a change, but does refresh the age); a fact past its
ttl is still shown, as "last known"; the rendering states ages in the
runtime's words, never the model's; the block opens a turn in the fixed
room-first order and says nothing for a switch in its default state;
the file mirror carries facts across a restart at their true age; the
spoke's relay sends one ``fact`` frame per name, from the loop, and
resends the set after a reconnect; the hub absorbs those frames into
its table, source-stamped; and a pipeline built with a table opens its
turn with the block after the lane's note and before the held notes.
"""

import asyncio
import json
import sys
import tempfile
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ciel import wire
from ciel import world as W
from ciel.config import BrainConfig, Config, HubConfig, WebConfig, WorldConfig
from ciel.hub.server import HubServer
from ciel.oura import today_readings
from ciel.pipeline import Pipeline, _TextSink
from ciel.remote.web import Admission
from ciel.spoke.publisher import WorldRelay
from ciel.turn import _WEB_NOTE, TurnRequest
from ciel.world import Fact, World

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# A fixed afternoon: 2026-09-04 15:42 local.
NOON = time.mktime((2026, 9, 4, 15, 42, 0, 0, 0, -1))


# ── the table ────────────────────────────────────────────────────────────────


def probe_facts() -> None:
    print("\nfacts and versions")
    clock = Clock(NOON)
    world = World(clock=clock)
    check("empty table renders the clock alone",
          world.render().startswith("(Now — It is 3:42 PM on Friday 4 September 2026"))
    v0 = world.version
    check("first observation is a change", world.observe(W.MUTED, True, source="mac"))
    check("...and bumps the version", world.version == v0 + 1)
    v1 = world.version
    clock.t += 5
    check("same value again is not a change", not world.observe(W.MUTED, True, source="mac"))
    check("...but moves observed_at", world.get(W.MUTED).observed_at == NOON + 5)
    check("...and within the notify window leaves the version alone", world.version == v1)
    clock.t += 30
    world.observe(W.MUTED, True, source="mac")
    check("a steady re-observation bumps the version on the notify clock", world.version == v1 + 1)
    check("a new value is a change", world.observe(W.MUTED, False, source="mac"))
    check("a different source for the same value is a change",
          world.observe(W.MUTED, False, source="hub"))
    check("value() answers with a default", world.value("nope", 7) == 7)
    check("forget() drops and bumps", world.forget(W.MUTED) and world.get(W.MUTED) is None)
    check("forgetting nothing is False", not world.forget(W.MUTED))


def probe_staleness() -> None:
    print("\nfreshness")
    clock = Clock(NOON)
    world = World(clock=clock)
    world.observe(W.PRESENCE, {"present": True, "locked": False, "idle_s": 3.0,
                               "since_conversation_s": None}, source="spoke:mac", ttl_s=30.0)
    fact = world.get(W.PRESENCE)
    check("fresh within the ttl", not fact.stale(NOON + 10))
    check("stale past the ttl", fact.stale(NOON + 31))
    check("no ttl never goes stale",
          not Fact("x", 1, NOON, "mac").stale(NOON + 10 ** 9))
    block = world.render(NOON + 10)
    check("a fresh presence reads as around, at the keyboard",
          "The user is around — at the keyboard." in block)
    block = world.render(NOON + 120)
    check("a stale presence says when the Mac last reported",
          "The Mac has not reported since 3:42 PM" in block)
    clock.t = NOON
    world.observe(W.PLACE, {"place": "home", "via": "wifi", "network": "Nest",
                            "device": None, "at": NOON - 1500}, source="mac")
    block = world.render(NOON)
    check("a place carries the fix's own age, not the fold-in time",
          "Place: home, per the Mac's Wi-Fi (25 minutes ago)." in block)
    world.observe(W.PLACE, {"place": None, "via": "findmy", "network": None,
                            "device": "iPhone", "at": NOON - 4 * 3600}, source="mac")
    block = world.render(NOON)
    check("an old phone position says as of a clock time",
          "Place: not a known one, per their iPhone via Find My (as of 11:42 AM)." in block)


def probe_rendering() -> None:
    print("\nthe block")
    clock = Clock(NOON)
    world = World(clock=clock)
    world.observe(W.MUTED, False, source="mac")
    world.observe(W.HOLD, False, source="mac")
    block = world.render()
    check("switches in their default state say nothing",
          "muted" not in block and "hold" not in block)
    world.observe(W.MUTED, True, source="mac")
    world.observe(W.HOLD, True, source="mac")
    block = world.render()
    check("muted is a sentence", "The speakers are muted" in block)
    check("the hold is a sentence", "paused by the hold sentinel" in block)

    world = World(clock=clock)
    world.observe(W.TIMERS, [
        {"kind": "timer", "label": "", "due_at": NOON + 240, "duration_s": 600, "pending": False},
        {"kind": "alarm", "label": "Wake up.", "due_at": NOON + 3600, "duration_s": 0, "pending": False},
        {"kind": "timer", "label": "", "due_at": NOON, "duration_s": 120, "pending": True},
    ], source="hub")
    block = world.render()
    check("a running timer says what is left",
          "a 10 minute timer with 4 minutes left" in block)
    check("an alarm says its clock time and label",
          'an alarm at 4:42 PM ("Wake up.")' in block)
    check("a pending timer says it starts at end of turn",
          "a 2 minute timer that starts when this turn ends" in block)
    world.observe(W.TIMERS, [], source="hub")
    check("no timers, no line", "Timers:" not in world.render())

    world.observe(W.WATCHES, [
        {"label": "The export has finished.", "kind": "file", "target": "/tmp/out.csv",
         "expires_at": NOON + 1800},
    ], source="mac")
    check("a watch names the thing and its deadline",
          "Background watches: The export has finished. (file /tmp/out.csv until 4:12 PM)."
          in world.render())

    world.observe(W.AGENDA, {"day": "2026-09-04", "lines": ["5:00 PM — Standup", "7:30 PM — Dinner"]},
                  source="calendar")
    check("today's agenda lists the rest of the day",
          "Calendar for the rest of today: 5:00 PM — Standup; 7:30 PM — Dinner." in world.render())
    world.observe(W.AGENDA, {"day": "2026-09-04", "lines": []}, source="calendar")
    check("an empty agenda says so", "The calendar shows nothing more today." in world.render())
    world.observe(W.AGENDA, {"day": "2026-09-03", "lines": ["9:00 AM — Old"]}, source="calendar")
    check("yesterday's agenda is not rendered", "Calendar" not in world.render())

    readings = today_readings(
        "2026-09-04",
        [{"day": "2026-09-04", "score": 71, "contributors": {"sleep_balance": 58, "hrv_balance": 90}}],
        [{"day": "2026-09-04", "score": 80}],
        [{"day": "2026-09-04", "score": 45, "steps": 3200}],
        [{"day": "2026-09-04", "type": "long_sleep", "total_sleep_duration": 24000}],
    )
    check("today_readings is flat and complete",
          readings == {"day": "2026-09-04", "readiness": 71, "weakest": ["sleep balance", 58],
                       "sleep_score": 80, "sleep_s": 24000.0, "activity": 45, "steps": 3200})
    check("today_readings leaves out what was not fetched",
          today_readings("2026-09-04", [], [], [], []) == {"day": "2026-09-04"})
    world.observe(W.OURA, readings, source="oura")
    check("the ring line leads with readiness and its weakest contributor",
          "The ring, today: readiness 71 (weakest: sleep balance, 58), sleep score 80 with "
          "6 hours 40 minutes asleep, activity 45 so far, 3200 steps." in world.render())

    world.observe(W.SECTIONS, {"spots": {"12": 0, "31": 2}}, source="sections@mac", ttl_s=120)
    check("sections say full or open", "Watched sections: 12 is full; 31 has 2 open spots." in world.render())
    check("stale sections say last known",
          "Watched sections (last known, at 3:42 PM — no newer reading): 12 is full"
          in world.render(NOON + 300))

    world.observe(W.SPOKE, {"connected": False, "node": "mac"}, source="hub")
    block = world.render()
    check("a missing spoke opens the block, right after the clock",
          block.index("It is") < block.index("The user's Mac (mac) is not connected")
          < block.index("Background watches"))
    check("the block ends with the reading rule", block.endswith("fresher than that.)"))
    check("an unknown fact rides the snapshot but stays out of the block",
          world.observe("weather", {"c": 21}, source="x") and "weather" in world.snapshot()
          and "weather" not in world.render())


# ── the file ─────────────────────────────────────────────────────────────────


def probe_file() -> None:
    print("\nthe file mirror")
    tmp = Path(tempfile.mkdtemp()) / "world.json"
    clock = Clock(NOON)
    world = World(tmp, clock=clock)
    check("nothing to flush at first", not world.flush())
    world.observe(W.PLACE, {"place": "home", "via": "wifi", "network": "Nest", "device": None,
                            "at": NOON}, source="mac")
    check("a change flushes", world.flush() and tmp.exists())
    check("...once", not world.flush())
    again = World(tmp, clock=Clock(NOON + 600))
    fact = again.get(W.PLACE)
    check("a restart carries the fact at its true age",
          fact is not None and fact.observed_at == NOON and fact.source == "mac")
    check("the carried fact renders as old",
          "Place: home, per the Mac's Wi-Fi (10 minutes ago)." in again.render())
    tmp.write_text("not json")
    check("an unreadable file starts empty", World(tmp).facts() == [])
    tmp.write_text(json.dumps({"place": {"value": 1}, "bad": {"observed_at": "x"}}))
    check("a malformed entry is skipped, the rest kept",
          [f.name for f in World(tmp).facts()] == [])
    tmp.write_text(json.dumps({"muted": {"value": True, "observed_at": 1.0, "source": "mac"}}))
    check("a good entry loads", World(tmp).value(W.MUTED) is True)


# ── the wire ─────────────────────────────────────────────────────────────────


def probe_relay() -> None:
    print("\nthe spoke's relay")
    sent: list[dict] = []
    up = {"ok": True}

    def send(frame):
        if not up["ok"]:
            return False
        sent.append(frame)
        return True

    relay = WorldRelay(send, node="mac")
    check("a first observation is a change", relay.observe(W.PLACE, {"place": "home"}, source="mac"))
    check("nothing goes up until the loop flushes", not sent and relay.outstanding == 1)
    check("flush sends one fact frame", relay.flush() == 1 and sent[-1]["type"] == "fact"
          and sent[-1]["name"] == "place" and sent[-1]["node"] == "mac")
    check("the frame passes the catalog", wire.validate(sent[-1], "c2h") is sent[-1])
    check("an unchanged re-observation is not a change",
          not relay.observe(W.PLACE, {"place": "home"}, source="mac"))
    check("...but still refreshes the age on the wire", relay.flush() == 1 and len(sent) == 2)
    check("nothing pending after a flush", relay.flush() == 0 and relay.outstanding == 0)
    up["ok"] = False
    relay.observe(W.SECTIONS, {"spots": {"31": 1}}, source="sections", ttl_s=120)
    check("a failed send stays pending", relay.flush() == 0 and relay.outstanding == 1)
    relay.observe(W.SECTIONS, {"spots": {"31": 2}}, source="sections", ttl_s=120)
    up["ok"] = True
    check("the newest value wins when the hub is back",
          relay.flush() == 1 and sent[-1]["value"] == {"spots": {"31": 2}} and sent[-1]["ttl_s"] == 120)
    relay.resend()
    check("a reconnect resends the whole set", relay.outstanding == 2 and relay.flush() == 2)


def probe_hub_absorbs() -> None:
    print("\nthe hub's table")
    cfg = replace(Config(), state_dir=Path(tempfile.mkdtemp()))
    server = HubServer(WebConfig(), cfg.hub)
    world = World()
    seen: list[str] = []

    def on_fact(frame):
        node = frame.get("node") or "mac"
        source = frame.get("source") or node
        world.absorb(frame["name"], frame, source=source if source == node else f"{source}@{node}")
        seen.append(frame["name"])

    server.on_fact = on_fact
    spoke = "spoke-ws"
    queue, _ = server._welcome(spoke, Admission(True, role="spoke", client_id="mac"))
    server._on_frame(json.dumps({
        "type": "fact", "name": "place", "value": {"place": "home"},
        "observed_at": NOON, "source": "mac", "node": "mac",
    }), spoke)
    check("a fact frame reaches the hook", seen == ["place"])
    fact = world.get(W.PLACE)
    check("...and lands source-stamped at the spoke's time",
          fact is not None and fact.source == "mac" and fact.observed_at == NOON)
    server._on_frame(json.dumps({
        "type": "fact", "name": "sections", "value": {"spots": {}},
        "observed_at": NOON, "source": "sections", "node": "mac",
    }), spoke)
    check("a producer other than the node is named with it",
          world.get(W.SECTIONS).source == "sections@mac")
    server._on_frame(json.dumps({"type": "fact", "name": "x", "value": 1}), spoke)
    check("a frame missing observed_at is refused by the catalog", seen == ["place", "sections"])
    check("absorb refuses a bad shape", not world.absorb("x", {"value": 1, "observed_at": "no"}))

    # The broadcast side: note_world dedupes and rides the ring.
    server.note_world(world.snapshot())
    server.note_world(world.snapshot())
    frames = []
    while not queue.empty():
        frames.append(json.loads(queue.get_nowait()))
    kinds = [f["type"] for f in frames]
    check("the hello carries the (empty) world", frames[0]["type"] == "hello" and frames[0]["world"] == {})
    check("one world frame per change, seq-stamped",
          kinds.count("world") == 1 and "seq" in frames[-1] and "place" in frames[-1]["facts"])
    check("world is a broadcast type", "world" in wire.BROADCAST_TYPES)


# ── the turn ─────────────────────────────────────────────────────────────────


class FakeBrain:
    def __init__(self):
        self.prompts: list[str] = []
        self.last_turn_cost_usd = 0.0
        self.last_reconnect_s = 0.0

    async def ask(self, text):
        self.prompts.append(text)
        yield ("reply", "Sure.")

    async def interrupt(self):
        return None


class FakeIndicator:
    def set_state(self, state):
        return None


class FakeConfirm:
    def remote(self, send, *, origin):
        import contextlib

        return contextlib.nullcontext()

    def cancel(self, reason=""):
        return None


class FakeLink:
    def __init__(self):
        self.rows = []
        self.worlds = []

    def note_row(self, speaker, text):
        self.rows.append((speaker, text))

    def note_world(self, facts):
        self.worlds.append(facts)


class FakeEvents:
    pending = False

    def take_held(self, now):
        return [type("N", (), {"summary": "Your 2pm moved."})()]


class FakePresence:
    def state(self, now):
        from ciel.proactive.presence import PresenceState

        return PresenceState(screen_locked=False, seconds_since_input=2.0,
                             seconds_since_conversation=None, present=True)

    def note_conversation(self, now):
        return None


def make_pipeline(*, in_prompt=True, world=True):
    tmp = Path(tempfile.mkdtemp())
    cfg = replace(
        Config(), state_dir=tmp,
        brain=replace(BrainConfig(), speak_thinking=False, thinking_chime=False),
        world=replace(WorldConfig(), in_prompt=in_prompt, file=tmp / "world.json"),
    )
    p = Pipeline.__new__(Pipeline)
    p._config = cfg
    p._role = "local"
    p._brain = FakeBrain()
    p._confirm = FakeConfirm()
    p._indicator = FakeIndicator()
    p._web_link = FakeLink()
    p._transcript = None
    p._presence = FakePresence()
    p._events = FakeEvents()
    p._timers = None
    p._remote_link = None
    p._remote = None
    p._muted = False
    p._spoke = False
    p._conversed = False
    p._brain_conversed = False
    p._state = None
    p._typed = deque()
    p._work_watcher = None
    p._tts = None
    p._interrupted = False
    p._pending_text = None
    p._continue_listening = False
    p._reload_pending = False
    if world:
        p._world = World(cfg.world.file, clock=Clock(NOON))
        p._world.observe(W.MUTED, False, source="mac")
    return p


async def probe_turn() -> None:
    print("\nthe turn's opening block")
    p = make_pipeline()
    await p._run_turn(TurnRequest(lane="web", text="hi"), _TextSink(p))
    prompt = p._brain.prompts[0]
    check("lane note first, then the block, then held notes, then the words",
          prompt.startswith(_WEB_NOTE + "(Now — It is 3:42 PM")
          and prompt.index("(Now —") < prompt.index("Your 2pm moved.")
          and prompt.endswith("hi"))
    check("the block took a presence reading for this turn",
          "The user is around — at the keyboard." in prompt
          and p._world.get(W.PRESENCE).source == "mac")
    check("the transcript keeps the raw words", p._web_link.rows[0] == ("user-web", "hi"))

    p = make_pipeline(in_prompt=False)
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    check("in_prompt = false opens the turn bare", "(Now" not in p._brain.prompts[0])

    p = make_pipeline(world=False)
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    check("no table (the probes' pipelines) means no block",
          "(Now" not in p._brain.prompts[0])

    p = make_pipeline()
    p._world_tick()
    check("the tick flushes the file", p._config.world.file.exists())
    check("...and hands the Chart the snapshot once per version",
          len(p._web_link.worlds) == 1 and W.MUTED in p._web_link.worlds[0])
    p._world_tick()
    check("an unchanged tick hands over nothing", len(p._web_link.worlds) == 1)


async def main() -> None:
    probe_facts()
    probe_staleness()
    probe_rendering()
    probe_file()
    probe_relay()
    probe_hub_absorbs()
    await probe_turn()
    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
