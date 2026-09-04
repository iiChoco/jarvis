"""Probe the unified turn skeleton — all four lanes through _run_turn.

    uv run scripts/probe_turns.py

The lane registry's labels, notes, and delivery rules are load-bearing:
``user``/``you-confirm``/``user-web`` rows are Vigil's presence evidence,
``user-remote`` is deliberately evidence of the opposite, the Chart
renders rows by these exact strings, and the system notes are the
model's only way to know where the user is. This probe drives every lane
through the one shared skeleton with fakes and pins that contract —
golden row sequences, prompt composition, reply routing, confirm-context
origins, the conversation flags, and the failure shapes. A regression
here is a lane quietly changing meaning, which is exactly what the
hub split must never do.
"""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ciel.config import BrainConfig, Config
from ciel.pipeline import Pipeline, _DiscordSink, _TextSink, _VoiceSink
from ciel.remote.discord import RemoteUnavailable
from ciel.turn import (
    _REMOTE_NOTE,
    _REMOTE_PUBLIC_NOTE,
    _WEB_MUTED_NOTE,
    _WEB_NOTE,
    TurnRequest,
    lane_spec,
)

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeBrain:
    """Yields a scripted stream of (kind, sentence); records the prompt."""

    def __init__(self, script, error=None):
        self.script = script
        self.error = error
        self.prompts: list[str] = []
        self.interrupts = 0
        self.last_turn_cost_usd = 0.0
        self.last_reconnect_s = 0.0

    async def ask(self, text):
        self.prompts.append(text)
        for item in self.script:
            yield item
        if self.error is not None:
            raise self.error

    async def interrupt(self):
        self.interrupts += 1


class FakeConfirm:
    """Records remote-context entries; the broker surface _run_turn uses."""

    def __init__(self):
        self.remotes: list[str] = []
        self.cancels: list[str] = []

    def remote(self, send, *, origin):
        self.remotes.append(origin)
        import contextlib

        return contextlib.nullcontext()

    def cancel(self, reason=""):
        self.cancels.append(reason)


class FakeIndicator:
    def __init__(self):
        self.states: list[str] = []

    def set_state(self, state):
        self.states.append(state)


class FakeWebLink:
    """The record tap's row recorder — the golden transcript."""

    def __init__(self):
        self.rows: list[tuple[str, str]] = []

    def note_row(self, speaker, text):
        self.rows.append((speaker, text))


class FakeTTS:
    sample_rate = 16000

    def stream(self, text):
        async def chunks():
            yield text.encode()

        return chunks()


class FakePlayer:
    """Plays by collecting; a scripted budget of completions."""

    def __init__(self, complete_after=None):
        self.played: list[str] = []
        self.complete_after = complete_after  # None: always complete
        self.device_lost = False

    async def play(self, stream):
        text = b""
        async for chunk in stream:
            text += chunk
        self.played.append(text.decode())
        if self.complete_after is not None and len(self.played) > self.complete_after:
            return False
        return True


class FakeRemoteLink:
    def __init__(self, fail_send=False):
        self.sent: list[tuple[str, object]] = []
        self.fail_send = fail_send

    async def send(self, text, channel=None):
        if self.fail_send:
            raise RemoteUnavailable("gateway down")
        self.sent.append((text, channel))

    def typing(self, channel=None):
        import contextlib

        return contextlib.nullcontext()


class FakeEvents:
    """Held notes: one note, consumed on take."""

    def __init__(self, notes=()):
        self._notes = list(notes)
        self.takes = 0

    def take_held(self, now):
        self.takes += 1
        notes, self._notes = self._notes, []
        return notes


class FakeNote:
    def __init__(self, summary):
        self.summary = summary


class FakeTimers:
    def __init__(self):
        self.commits = 0
        self.armed: list[float] = []

    def commit_pending(self):
        self.commits += 1

    def set_relative(self, seconds, label=""):
        self.armed.append(seconds)


class FakeChannel:
    """A Discord guild channel — makes a turn public."""

    def __init__(self, name="general"):
        self.name = name
        self.guild = object()


def make_pipeline(script, *, error=None, events=None, muted=False,
                  remote=None, ack=False):
    cfg = replace(
        Config(),
        brain=replace(
            BrainConfig(),
            ack_phrases=("Hmm.",) if ack else (),
            ack_delay_s=0.01 if ack else 0.6,
            speak_thinking=False,
            thinking_chime=False,
        ),
    )
    p = Pipeline.__new__(Pipeline)
    p._config = cfg
    p._role = "local"
    p._brain = FakeBrain(script, error)
    p._confirm = FakeConfirm()
    p._indicator = FakeIndicator()
    p._web_link = FakeWebLink()
    p._transcript = None
    p._presence = None
    p._events = events
    p._timers = FakeTimers()
    p._tts = FakeTTS()
    p._remote_link = remote
    p._muted = muted
    p._spoke = False
    p._conversed = False
    p._brain_conversed = False
    p._interrupted = False
    p._pending_text = None
    p._continue_listening = False
    p._reload_pending = False
    return p


STREAM = [("thinking", "Consider."), ("reply", "First."), ("reply", "Second.")]


def rows(p):
    return p._web_link.rows


# ── the golden row contract ──────────────────────────────────────────────────


async def probe_labels_and_rows() -> None:
    print("labels and the golden row sequence")

    p = make_pipeline(STREAM)
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    golden = [
        ("user", "hi"),
        ("ciel-thinking", "Consider."),
        ("ciel", "First."),
        ("ciel", "Second."),
    ]
    check("typed: label user, golden row order", rows(p) == golden)
    check("typed: conversation flags set", p._conversed and p._brain_conversed)
    check("typed: timers committed in the finally", p._timers.commits == 1)

    p = make_pipeline(STREAM)
    await p._run_turn(
        TurnRequest(lane="web", text="hi"), _TextSink(p, confirm_send=None)
    )
    check("web: label user-web", rows(p)[0] == ("user-web", "hi"))
    check("web: same golden tail", rows(p)[1:] == golden[1:])

    link = FakeRemoteLink()
    p = make_pipeline(STREAM, remote=link)

    async def send_here(t):
        await link.send(t, None)

    await p._run_turn(
        TurnRequest(lane="discord", text="hi"), _DiscordSink(p, send_here)
    )
    check("discord: label user-remote", rows(p)[0] == ("user-remote", "hi"))
    check(
        "discord: reply buffered into one text",
        link.sent == [("First. Second.", None)],
    )

    p = make_pipeline(STREAM)
    player = FakePlayer()
    await p._run_turn(TurnRequest(lane="voice", text="hi"), _VoiceSink(p, player))
    check("voice: label user", rows(p)[0] == ("user", "hi"))
    check("voice: replies played, thinking shown not spoken",
          player.played == ["First.", "Second."])
    check("voice: _spoke opens the follow-up window", p._spoke)


# ── prompt composition ───────────────────────────────────────────────────────


async def probe_prompts() -> None:
    print("\nprompt notes and held notes")

    p = make_pipeline(STREAM)
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    check("typed: bare prompt, no note", p._brain.prompts == ["hi"])

    p = make_pipeline(STREAM)
    await p._run_turn(TurnRequest(lane="web", text="hi"), _TextSink(p))
    check("web: the GUI note", p._brain.prompts[0] == _WEB_NOTE + "hi")

    p = make_pipeline(STREAM, muted=True)
    await p._run_turn(TurnRequest(lane="web", text="hi"), _TextSink(p))
    check("web muted: the muted variant", p._brain.prompts[0] == _WEB_MUTED_NOTE + "hi")

    link = FakeRemoteLink()
    p = make_pipeline(STREAM, remote=link)
    await p._run_turn(
        TurnRequest(lane="discord", text="hi"), _DiscordSink(p, link.send)
    )
    check("discord DM: the remote note", p._brain.prompts[0] == _REMOTE_NOTE + "hi")

    events = FakeEvents([FakeNote("Your 2pm moved.")])
    p = make_pipeline(STREAM, events=events, remote=link)
    await p._run_turn(
        TurnRequest(lane="discord", text="hi"), _DiscordSink(p, link.send)
    )
    check(
        "discord DM: held notes ride inside the note prefix",
        p._brain.prompts[0].startswith(_REMOTE_NOTE)
        and "Your 2pm moved." in p._brain.prompts[0]
        and p._brain.prompts[0].endswith("hi"),
    )

    events = FakeEvents([FakeNote("Your 2pm moved.")])
    link = FakeRemoteLink()
    p = make_pipeline(STREAM, events=events, remote=link)
    chan = FakeChannel()
    await p._run_turn(
        TurnRequest(lane="discord", text="hi", channel=chan, public=True),
        _DiscordSink(p, link.send),
    )
    check(
        "discord public: discretion note, held notes withheld",
        p._brain.prompts[0] == _REMOTE_PUBLIC_NOTE + "hi" and events.takes == 0,
    )

    events = FakeEvents([FakeNote("Your 2pm moved.")])
    p = make_pipeline(STREAM, events=events)
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    check(
        "typed: held notes consumed and prefixed",
        events.takes == 1 and "Your 2pm moved." in p._brain.prompts[0],
    )
    check(
        "held-note delivery leaves an event row",
        ("event", "held notes delivered (1)") in rows(p),
    )


# ── confirm routing ──────────────────────────────────────────────────────────


async def probe_confirm_contexts() -> None:
    print("\nconfirm contexts")

    p = make_pipeline(STREAM)
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    check("typed: confirmations stay voiced", p._confirm.remotes == [])

    p = make_pipeline(STREAM)
    await p._run_turn(TurnRequest(lane="web", text="hi"), _TextSink(p))
    check("web: broker enters remote mode as web", p._confirm.remotes == ["web"])

    link = FakeRemoteLink()
    p = make_pipeline(STREAM, remote=link)
    await p._run_turn(
        TurnRequest(lane="discord", text="hi"), _DiscordSink(p, link.send)
    )
    check("discord: broker enters remote mode as discord",
          p._confirm.remotes == ["discord"])

    p = make_pipeline(STREAM)
    await p._run_turn(
        TurnRequest(lane="voice", text="hi"), _VoiceSink(p, FakePlayer())
    )
    check("voice: confirmations stay voiced", p._confirm.remotes == [])


# ── escalation ───────────────────────────────────────────────────────────────


async def probe_escalation() -> None:
    print("\nescalation")
    script = [("escalation", ""), ("reply", "Answer.")]

    p = make_pipeline(script)
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    check(
        "typed: escalation leaves an event row",
        ("event", "deep thought engaged") in rows(p),
    )

    link = FakeRemoteLink()
    p = make_pipeline(script, remote=link)
    await p._run_turn(
        TurnRequest(lane="discord", text="hi"), _DiscordSink(p, link.send)
    )
    check(
        "discord: interim text covers the silence",
        link.sent[0][0].startswith("Give me a moment")
        and link.sent[1] == ("Answer.", None),
    )

    p = make_pipeline(script)
    player = FakePlayer()
    await p._run_turn(TurnRequest(lane="voice", text="hi"), _VoiceSink(p, player))
    check(
        "voice: announcement spoken before the quiet",
        player.played[0].startswith("Give me a moment")
        and player.played[1] == "Answer.",
    )
    check(
        "voice: the announcement is recorded",
        ("ciel", "Give me a moment. I want to think this through properly.")
        in rows(p),
    )


# ── voice specifics ──────────────────────────────────────────────────────────


async def probe_voice() -> None:
    print("\nthe voice sink")

    script = [("reply", "First."), ("reply", "Second."), ("reply", "Third.")]
    p = make_pipeline(script)
    player = FakePlayer(complete_after=1)  # the second play is barged in on
    await p._run_turn(TurnRequest(lane="voice", text="hi"), _VoiceSink(p, player))
    check(
        "barge-in abandons the rest of the turn",
        player.played == ["First.", "Second."],  # Third. never plays
    )
    check("the interruption is recorded", ("event", "interrupted") in rows(p))
    check("the pending confirmation is cancelled",
          p._confirm.cancels == ["interrupted"])
    check("the brain is interrupted", p._brain.interrupts == 1)

    p = make_pipeline(STREAM, ack=True)
    player = FakePlayer()
    sink = _VoiceSink(p, player)
    await p._run_turn(TurnRequest(lane="voice", text="hi"), sink)
    check("the ack filler never leaks past the turn", sink._ack_task is None)

    p = make_pipeline([("thinking", "Only reasoning.")])
    await p._run_turn(
        TurnRequest(lane="voice", text="hi"), _VoiceSink(p, FakePlayer())
    )
    check(
        "a turn with no audio opens no follow-up window",
        not p._spoke and not p._conversed,
    )

    p = make_pipeline([("thinking", "Only reasoning.")])
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    check("...but a typed thinking-only turn still counts as conversation",
          p._conversed and p._brain_conversed)


# ── local commands ───────────────────────────────────────────────────────────


async def probe_local_commands() -> None:
    print("\nthe local-command bypass")

    p = make_pipeline([])
    await p._run_turn(TurnRequest(lane="typed", text="reload"), _TextSink(p))
    check("typed reload: pending, silent", p._reload_pending
          and rows(p) == [("user", "reload")])

    link = FakeRemoteLink()
    p = make_pipeline([], remote=link)
    await p._run_turn(
        TurnRequest(lane="discord", text="reload"), _DiscordSink(p, link.send)
    )
    check("discord reload: acknowledged over the lane",
          link.sent == [("Reloading.", None)])

    p = make_pipeline([])
    await p._run_turn(TurnRequest(lane="web", text="reload"), _TextSink(p))
    check("web reload: acknowledged as a row",
          ("ciel", "Reloading.") in rows(p))

    p = make_pipeline([])
    player = FakePlayer()
    await p._run_turn(
        TurnRequest(lane="voice", text="ten minute timer"),
        _VoiceSink(p, player),
    )
    check(
        "voice timer command: spoken, no brain",
        len(player.played) == 1
        and player.played[0].endswith("timer — starting now.")
        and p._timers.armed == [600.0]
        and p._brain.prompts == [],
    )
    check("voice local command opens the follow-up window", p._spoke)

    p = make_pipeline([])
    await p._run_turn(TurnRequest(lane="typed", text="never mind"), _TextSink(p))
    check("a dismissal is not a conversation", not p._conversed)


# ── failure shapes ───────────────────────────────────────────────────────────


async def probe_failures() -> None:
    print("\nfailure shapes")

    p = make_pipeline(STREAM, error=RuntimeError("boom"))
    await p._run_turn(TurnRequest(lane="typed", text="hi"), _TextSink(p))
    check("typed failure: event row, loop survives",
          ("event", "typed turn failed") in rows(p))
    check("typed failure: timers still committed", p._timers.commits == 1)

    p = make_pipeline(STREAM, error=RuntimeError("boom"))
    await p._run_turn(TurnRequest(lane="web", text="hi"), _TextSink(p))
    check("web failure: the page shows what happened",
          ("event", "web turn failed — see the log") in rows(p))

    link = FakeRemoteLink()
    p = make_pipeline(STREAM, error=RuntimeError("boom"), remote=link)
    await p._run_turn(
        TurnRequest(lane="discord", text="hi"), _DiscordSink(p, link.send)
    )
    check("discord failure: apology texted",
          link.sent[-1][0].startswith("Sorry"))

    link = FakeRemoteLink(fail_send=True)
    p = make_pipeline(STREAM, remote=link)
    await p._run_turn(
        TurnRequest(lane="discord", text="hi"), _DiscordSink(p, link.send)
    )
    check("discord undeliverable: logged as an event, not a crash",
          ("event", "remote reply undeliverable") in rows(p))

    p = make_pipeline(STREAM, error=RuntimeError("boom"))
    raised = False
    try:
        await p._run_turn(
            TurnRequest(lane="voice", text="hi"), _VoiceSink(p, FakePlayer())
        )
    except RuntimeError:
        raised = True
    check("voice failure re-raises to the spoken apology's guard", raised)


# ── the registry itself ──────────────────────────────────────────────────────


def probe_registry() -> None:
    print("\nthe registry, byte for byte")
    check("voice", lane_spec(TurnRequest(lane="voice", text="")).label == "user")
    check("typed", lane_spec(TurnRequest(lane="typed", text="")).label == "user")
    check("web", lane_spec(TurnRequest(lane="web", text="")).label == "user-web")
    spec = lane_spec(TurnRequest(lane="discord", text=""))
    check("discord", spec.label == "user-remote" and spec.origin == "discord")
    spec = lane_spec(
        TurnRequest(lane="discord", text="", channel=FakeChannel("dev"), public=True)
    )
    check(
        "discord public: channel-named origin, notes withheld",
        spec.origin == "discord #dev" and not spec.held_notes,
    )
    check(
        "console tags",
        lane_spec(TurnRequest(lane="voice", text="")).console_tag == "you"
        and lane_spec(TurnRequest(lane="typed", text="")).console_tag
        == "you (typed)",
    )


async def main() -> int:
    probe_registry()
    await probe_labels_and_rows()
    await probe_prompts()
    await probe_confirm_contexts()
    await probe_escalation()
    await probe_voice()
    await probe_local_commands()
    await probe_failures()
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
