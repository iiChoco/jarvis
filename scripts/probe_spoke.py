"""Probe the spoke — the room's half, with a fake hub on the other end.

    uv run scripts/probe_spoke.py

The spoke is the pipeline's audio state machine with the thinking
taken out: an utterance becomes a ``say``, sentences come back as
``turn.sentence`` frames and are played and receipted, questions
arrive as ``confirm.request`` and are spoken and listened for. This
drives the frontend with fakes for every component that touches
hardware (mic, player, STT, TTS, wake, endpointer, speaker gate, HUD)
and a fake hub link that records what went up: a full voice turn with
its receipts and the follow-up window after; the speaker gate, the
Cauchy hold, and a dismissal before the merge; barge-in reported as
an unfinished receipt; a hub that is down (said once, then a chime);
a confirmation spoken and answered, and one that times out; a
delivery rung and receipted; mute from the hub and from the sentinel;
and the state machine's reports.
"""

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ciel.config import AudioConfig, BrainConfig, Config
from ciel.schedule import State
from ciel.spoke.frontend import Spoke

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeLink:
    def __init__(self, connected=True):
        self.connected = connected
        self.frames: list[dict] = []
        self.says: list[tuple[str, str]] = []

    def send(self, frame):
        if not self.connected:
            return False
        self.frames.append(frame)
        return True

    def say(self, text, lane="voice"):
        self.says.append((text, lane))
        return self.connected

    def of(self, kind):
        return [f for f in self.frames if f["type"] == kind]


class FakeSTT:
    def __init__(self, text="hello there"):
        self.text = text

    async def transcribe(self, pcm):
        return self.text


class FakeTTS:
    sample_rate = 16000

    def stream(self, text):
        async def chunks():
            yield b"TTS:" + text.encode()

        return chunks()


class FakePlayer:
    def __init__(self, complete_after=None):
        self.played: list[str] = []
        self.complete_after = complete_after
        self.device_lost = False
        self.stops = 0
        self.is_playing = False

    async def play(self, stream):
        text = b""
        async for chunk in stream:
            text += chunk
        label = text[4:].decode() if text.startswith(b"TTS:") else "<pcm>"
        self.played.append(label)
        if self.complete_after is not None and len(self.played) > self.complete_after:
            return False
        return True

    def stop(self):
        self.stops += 1


class FakeMic:
    def __init__(self):
        self.drains = 0

    def drain(self):
        self.drains += 1


class FakeGate:
    def __init__(self, recognized=True, similarity=0.9):
        self.recognized = recognized
        self.similarity = similarity

    async def check(self, utterance):
        return self.recognized, self.similarity


class FakeEndpointer:
    def __init__(self, utterance_at=None):
        self.speaking = False
        self.pushes = 0
        self.resets = 0
        self.utterance_at = utterance_at

    def push(self, frame):
        self.pushes += 1
        if self.utterance_at is not None and self.pushes == self.utterance_at:
            return np.zeros(16000, dtype=np.float32)
        return None

    def reset(self):
        self.resets += 1


class FakeWake:
    def reset(self):
        pass


class FakeIndicator:
    def __init__(self):
        self.states: list[str] = []

    def set_state(self, state):
        self.states.append(state)


def make_spoke(*, stt="hello there.", connected=True, complete_after=None,
               recognized=True, followup_ms=1500, ack=False):
    tmp = Path(tempfile.mkdtemp())
    cfg = replace(
        Config(),
        state_dir=tmp,
        audio=replace(AudioConfig(), followup_ms=followup_ms, filler_extend_ms=1200),
        brain=replace(
            BrainConfig(),
            ack_phrases=("Hmm.",) if ack else (),
            ack_delay_s=0.01 if ack else 0.6,
            speak_thinking=False,
            thinking_chime=False,
        ),
    )
    s = Spoke.__new__(Spoke)
    s._config = cfg
    s._stt = FakeSTT(stt)
    s._tts = FakeTTS()
    s._wake = FakeWake()
    s._endpointer = FakeEndpointer()
    s._confirm_endpointer = FakeEndpointer()
    s._speaker = FakeGate(recognized)
    s._indicator = FakeIndicator()
    s._link = FakeLink(connected)
    s._mute_sentinel = tmp / "mute"
    s._muted = False
    s._next_mute_check = 0.0
    s._player = FakePlayer(complete_after)
    s._mic = FakeMic()
    s._state = State.BUSY
    s._interrupted = False
    s._barge_run = 0
    s._noise_floor = None
    s._followup_until = None
    s._spoke = False
    s._pending_text = None
    s._continue_listening = False
    s._prelude = None
    s._awaiting_hub = False
    s._hub_turn = None
    s._turn_deadline = 0.0
    s._playing = None
    s._prev_kind = None
    s._ack_task = None
    s._ack_speaking = False
    s._confirm = None
    s._delivering = False
    s._hub_lost_said = False
    s._reported = None
    s._watcher = None
    s.reload_requested = False

    class _Outbox:
        def __init__(self):
            self.acked_ids = []
            self.resends = 0

        def acked(self, publish_id):
            self.acked_ids.append(publish_id)

        def resend(self):
            self.resends += 1
            return 0

    class _Beat:
        def __init__(self):
            self.beats = 0

        def beat(self, *, force=False):
            self.beats += 1
            return True

    class _Exec:
        def __init__(self):
            self.frames = []

        def handle(self, frame):
            self.frames.append(frame)

    s._publish = _Outbox()
    s._heartbeat = _Beat()
    s._executor = _Exec()
    s._watchers = []
    return s


UTT = np.zeros(16000, dtype=np.float32)


async def settle():
    for _ in range(3):
        await asyncio.sleep(0.01)


# ── the probes ───────────────────────────────────────────────────────────────


async def probe_voice_turn() -> None:
    print("a voice turn, end to end")
    s = make_spoke()
    await s._handle_utterance(UTT)
    check("the utterance went up as a voice say", s._link.says == [("hello there.", "voice")])
    check("the spoke waits for the hub to begin", s._awaiting_hub and s._state is State.BUSY)
    s._on_frame({"type": "turn.begin", "turn_id": "t1", "lane": "voice"})
    check("turn.begin claims the turn", s._hub_turn == "t1" and not s._awaiting_hub)
    s._on_frame({"type": "turn.sentence", "turn_id": "t1", "n": 1, "kind": "reply", "text": "First."})
    await settle()
    s._on_frame({"type": "turn.sentence", "turn_id": "t1", "n": 2, "kind": "reply", "text": "Second."})
    await settle()
    check("sentences play in order", s._player.played == ["First.", "Second."])
    check(
        "each is receipted as completed",
        [(f["n"], f["completed"]) for f in s._link.of("turn.played")] == [(1, True), (2, True)],
    )
    s._on_frame({"type": "turn.sentence", "turn_id": "other", "n": 1, "kind": "reply", "text": "Stray."})
    await settle()
    check("a sentence for another turn is ignored", "Stray." not in s._player.played)
    s._on_frame({"type": "turn.end", "turn_id": "t1", "status": "done"})
    check("turn.end releases the turn", s._hub_turn is None)
    s._finish_turn(s._mic)
    check(
        "after a spoken reply the follow-up window opens, the mic drained",
        s._state is State.LISTENING and s._mic.drains == 1 and s._followup_until is not None,
    )
    check("the hub's idle is ignored while a window is open",
          (s._on_frame({"type": "state", "state": "idle"}) or s._indicator.states[-1]) == "listening")
    s._on_frame({"type": "state", "state": "thinking"})
    check("other hub states drive the HUD", s._indicator.states[-1] == "thinking")


async def probe_prelude() -> None:
    print("\nthe prelude: gate, hold, dismissal")
    s = make_spoke(recognized=False)
    await s._handle_utterance(UTT)
    check("a stranger's sentence never leaves the machine", s._link.says == [])

    s = make_spoke(stt="check my")
    await s._handle_utterance(UTT)
    check(
        "a trailing-off transcript is held, not sent",
        s._link.says == [] and s._pending_text == "check my" and s._continue_listening,
    )
    s._stt = FakeSTT("calendar for today.")
    await s._handle_utterance(UTT)
    check("the continuation merges onto the held thought and goes up",
          s._link.says == [("check my calendar for today.", "voice")] and s._pending_text is None)

    s = make_spoke(stt="never mind")
    s._pending_text = "set a timer for"
    await s._handle_utterance(UTT)
    check(
        "a dismissal clears the held thought and goes up alone",
        s._pending_text is None and s._link.says == [("never mind", "voice")],
    )

    s = make_spoke(stt="")
    await s._handle_utterance(UTT)
    check("nothing intelligible sends nothing", s._link.says == [])


async def probe_barge_in() -> None:
    print("\nbarge-in as an unfinished receipt")
    s = make_spoke(complete_after=1)
    s._on_frame({"type": "turn.begin", "turn_id": "t2", "lane": "voice"})
    s._on_frame({"type": "turn.sentence", "turn_id": "t2", "n": 1, "kind": "reply", "text": "One."})
    await settle()
    s._on_frame({"type": "turn.sentence", "turn_id": "t2", "n": 2, "kind": "reply", "text": "Two."})
    await settle()
    check(
        "the stopped sentence is receipted as not completed, and the turn marked interrupted",
        [(f["n"], f["completed"]) for f in s._link.of("turn.played")] == [(1, True), (2, False)]
        and s._interrupted,
    )


async def probe_hub_down() -> None:
    print("\nthe hub is down")
    s = make_spoke(connected=False)
    await s._handle_utterance(UTT)
    await settle()
    check(
        "the say is held in the ledger and the loss is said once",
        s._link.says == [("hello there.", "voice")]
        and s._player.played == ["I can't reach the hub right now."]
        and not s._awaiting_hub,
    )
    s._stt = FakeSTT("still there?")
    await s._handle_utterance(UTT)
    await settle()
    check("the second time is a chime", s._player.played[-1] == "<pcm>" and len(s._player.played) == 2)
    s._on_connect({"type": "hello", "muted": False})
    check("reconnecting resets the notice, resends the outbox, beats at once",
          not s._hub_lost_said and s._publish.resends == 1 and s._heartbeat.beats == 1)
    s._on_frame({"type": "tool.request", "rpc_id": "r1", "tool": "screen.capture", "args": {}})
    s._on_frame({"type": "event.ack", "publish_id": "p-mac-1"})
    check("tool requests go to the executor, event acks to the outbox",
          s._executor.frames[0]["rpc_id"] == "r1" and s._publish.acked_ids == ["p-mac-1"])

    s = make_spoke()
    s._on_frame({"type": "turn.begin", "turn_id": "t3", "lane": "voice"})
    s._on_disconnect()
    check("a drop mid-turn releases the turn", s._hub_turn is None and not s._awaiting_hub)


async def probe_confirm() -> None:
    print("\na confirmation, spoken and answered")
    s = make_spoke(stt="yes")
    s._on_frame({"type": "turn.begin", "turn_id": "t4", "lane": "voice"})
    s._on_frame({"type": "confirm.request", "confirm_id": "c1", "text": "Run: git push — okay?",
                 "deadline_wall": 0.0, "listen": True})
    await settle()
    check(
        "the question is spoken, the mic drained, the answer window open",
        s._player.played == ["Run: git push — okay?"] and s._mic.drains == 1
        and s._confirm is not None and s._confirm[0] == "c1",
    )
    s._confirm_endpointer = FakeEndpointer(utterance_at=2)
    s._feed_confirm(b"\x00" * 640)
    check("frames go to the answer endpointer", s._confirm is not None)
    s._feed_confirm(b"\x00" * 640)
    await settle()
    check(
        "the utterance is transcribed and sent as the answer",
        s._confirm is None and s._link.of("confirm.answer") == [
            {"type": "confirm.answer", "confirm_id": "c1", "text": "yes"}
        ],
    )
    s._on_frame({"type": "confirm.request", "confirm_id": "c2", "text": "Okay, skipping it.",
                 "deadline_wall": 0.0, "listen": False})
    await settle()
    check("a closing line is spoken without opening the window",
          s._player.played[-1] == "Okay, skipping it." and s._confirm is None)

    s = make_spoke()
    s._on_frame({"type": "confirm.request", "confirm_id": "c3", "text": "Sure?",
                 "deadline_wall": 0.0, "listen": True})
    await settle()
    s._confirm = ("c3", 0.0)  # the deadline already passed
    s._feed_confirm(b"\x00" * 640)
    check(
        "the window timing out sends an empty answer",
        s._confirm is None and s._link.of("confirm.answer")[-1]["text"] == "",
    )
    s._on_frame({"type": "confirm.request", "confirm_id": "c4", "text": "Sure?",
                 "deadline_wall": 0.0, "listen": True})
    await settle()
    s._on_frame({"type": "confirm.cancel", "confirm_id": "c4"})
    check("confirm.cancel closes the window", s._confirm is None)


async def probe_delivery() -> None:
    print("\na delivery at idle")
    s = make_spoke()
    s._state = State.WAITING
    s._on_frame({"type": "deliver.speak", "event_id": "timer:1", "text": "Your ten minute timer is done.",
                 "meta": {"ring": True, "kind": "timer"}})
    await settle()
    check(
        "ring, then the line, then the receipt, mic drained",
        s._player.played == ["<pcm>", "Your ten minute timer is done."]
        and s._link.of("deliver.result") == [{"type": "deliver.result", "event_id": "timer:1", "ok": True}]
        and s._mic.drains == 1 and not s._delivering,
    )
    check("the HUD returns to idle", s._indicator.states[-1] == "idle")

    s = make_spoke(complete_after=1)
    s._state = State.WAITING
    s._on_frame({"type": "deliver.speak", "event_id": "ev", "text": "Nudge.", "meta": {}})
    await settle()
    check("a nudge barged in on still counts as delivered",
          s._link.of("deliver.result")[-1]["ok"] is True)
    s = make_spoke(complete_after=0)
    s._player.device_lost = True
    s._state = State.WAITING
    s._on_frame({"type": "deliver.speak", "event_id": "ev", "text": "Nudge.", "meta": {}})
    await settle()
    check("a dead device does not", s._link.of("deliver.result")[-1]["ok"] is False)


async def probe_mute_and_state() -> None:
    print("\nmute and the state reports")
    s = make_spoke()
    s._on_frame({"type": "muted", "muted": True})
    check(
        "the hub's mute touches the sentinel, stops the player, sends no echo",
        s._muted and s._mute_sentinel.exists() and s._player.stops == 1
        and s._link.of("mute") == [],
    )
    s._set_muted(False)
    check(
        "a local flip clears the sentinel and tells the hub",
        not s._mute_sentinel.exists() and s._link.of("mute") == [{"type": "mute", "muted": False}],
    )
    s._on_connect({"type": "hello", "muted": True})
    check("the hub's hello carries the switch", s._muted)

    s = make_spoke()
    s._state = State.WAITING
    s._report_voice_state()
    s._report_voice_state()
    s._enter_listening()
    s._report_voice_state()
    s._endpointer.speaking = True
    s._report_voice_state()
    s._report_voice_state()
    check(
        "voice.state goes up once per transition",
        [(f["listening"], f["speaking"]) for f in s._link.of("voice.state")]
        == [(False, False), (True, False), (True, True)],
    )


async def probe_ack_filler() -> None:
    print("\nthe ack filler")
    s = make_spoke(ack=True)
    s._on_frame({"type": "turn.begin", "turn_id": "t5", "lane": "voice"})
    await asyncio.sleep(0.05)
    s._on_frame({"type": "turn.sentence", "turn_id": "t5", "n": 1, "kind": "reply", "text": "Late."})
    await settle()
    check("a slow first sentence is preceded by the filler",
          s._player.played == ["Hmm.", "Late."])
    s = make_spoke(ack=True)
    s._on_frame({"type": "turn.begin", "turn_id": "t6", "lane": "voice"})
    s._on_frame({"type": "turn.sentence", "turn_id": "t6", "n": 1, "kind": "reply", "text": "Quick."})
    await settle()
    check("a quick first sentence cancels it unspoken", s._player.played == ["Quick."])


async def main() -> None:
    await probe_voice_turn()
    await probe_prelude()
    await probe_barge_in()
    await probe_hub_down()
    await probe_confirm()
    await probe_delivery()
    await probe_mute_and_state()
    await probe_ack_filler()
    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
