"""The orchestrator: wake, listen, transcribe, think, speak.

There is one microphone and one reader of it — this loop. Every component that
needs audio is *fed* frames rather than opening its own stream, which is what
keeps the wake detector and the endpointer from fighting over the device.

The loop is a small state machine. The states matter because the same incoming
frame means completely different things depending on where we are: before the
wake word it's noise to be scored, during an utterance it's speech to be
buffered, and while Ciel is talking it's a possible interruption.

Six of this file's behaviors carry codenames: **Cauchy** (the mid-thought
hold — a transcript that doesn't read as finished means the tail hasn't
settled yet, so don't declare the sequence converged), **Discontinuity**
(barge-in — a jump that ends the current segment), **Neighborhood** (the
follow-up window — an open ball around the last turn, inside which no wake
word is required), **Isomorphism** (the typed lane — a line on stdin is
a structure-preserving image of a spoken turn: same brain, same session,
same transcript, no audio on either side), **Parallel Transport** (the
Discord lane — the same map carried along the path away from home: a DM
from the pinned owner account is a turn, the reply rides back as a text,
and confirmations travel the same road), and **Chart** (the web GUI — a
local coordinate window onto the whole conversation: a loopback page that
shows every lane live, takes typed turns of its own, and holds the mute
switch for rooms that must stay quiet).

The same class runs in two roles. ``local`` is all of the above in one
process. ``hub`` builds no audio at all: the voice lane's turns arrive
from the spoke (``ciel spoke``, the room's half) over the wire and its
sentences go back the same way — see ``_WireSink`` and ``_run_hub`` —
while every text lane, Vigil, and the maintenance slot run unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import secrets
import sys
import time
from collections import deque
from contextlib import AsyncExitStack, aclosing
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ciel.brain.agent import Brain
from ciel.brain.prompt import REFLECTION_PROMPT, proactive_prompt
from ciel.brain.tools import build_tool_server
from ciel.brain.tools.memory import bind_context as bind_memory_context
from ciel.config import SAMPLE_RATE, AudioConfig, Config
from ciel.confirm import VoiceConfirmBroker
from ciel.messages import MessagesClient, MessagesUnavailable
from ciel.remote.discord import DiscordLink, RemoteUnavailable
from ciel.remote.web import WebIndicator, WebLink
from ciel.hub.rpc import RemoteBindings
from ciel.hub.server import HubServer
from ciel.proactive.calendar import CalendarWatcher
from ciel.proactive.gcal import GoogleCalendarWatcher
from ciel.proactive.location import LocationWatcher
from ciel.proactive.oura import OuraWatcher
from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.policy import Decision, InterruptionPolicy
from ciel.proactive.presence import PresenceProbe
from ciel.proactive.sections import SectionsWatcher
from ciel.proactive.watchers import ScheduleWatcher
from ciel.proactive.work import WorkWatcher
from ciel.brain.tools.location import bind_locator
from ciel.brain.tools.watch import bind_watcher
from ciel.location import Locator
from ciel import world as W
from ciel.world import World
from ciel.endpointing import trails_off as _trails_off_words
from ciel.reload import SourceWatcher, default_roots
from ciel.schedule import Snapshot, Source, State, pick_next
from ciel.commands import Command, match as match_command
from ciel.timers import Timer, announcement, spoken_clock, spoken_duration
from ciel.transcript import Transcript
from ciel.turn import TurnRequest, TurnSink, lane_spec, prompt_note
from ciel.ui.indicator import Indicator, TeeIndicator, build_indicator

if TYPE_CHECKING:
    from ciel.interview.app import InterviewApp
    # The audio stack is imported where it is built (the local role's
    # constructor and the methods only it runs), never at module level:
    # the hub imports this module on a machine with no sound device, no
    # VAD library, and no Metal.
    from ciel.audio.input import MicStream
    from ciel.audio.output import Player
    from ciel.audio.vad import Endpointer
    from ciel.audio.wake import WakeDetector
    from ciel.stt import SpeechToText
    from ciel.tts.base import TextToSpeech

log = logging.getLogger(__name__)

_SLEEP_GAP_S = 30.0
"""A wall-clock jump between frames larger than this means the machine slept
(or the process was suspended — same consequences). Frames arrive ~33/s while
awake, so even a wedged maintenance pass never opens a gap like this. Chosen
well above any legitimate scheduling hiccup and well below the shortest nap a
lid-close produces. On macOS the monotonic clock largely pauses with the
process, so every monotonic deadline in the loop silently stretched by the
nap — the wake-up handler is where reality is reconciled."""


def trails_off(heard: str, audio: AudioConfig) -> bool:
    """Whether a transcript ends mid-thought (the Cauchy hold's judgement).

    The judgement itself lives in ``ciel.endpointing`` — the interview
    room reads browser transcripts with the same eyes — and this keeps the
    pipeline's signature, which takes the audio config for its
    ``dangling_words``.
    """
    return _trails_off_words(heard, audio.dangling_words)
    if text.endswith(("?", "!")):
        return False
    words = text.split()
    if not words:
        return False
    last = re.sub(r"[^a-z']+", "", words[-1].lower())
    if text.endswith("."):
        return last in audio.dangling_words
    return True


def proactive_valve(reply: str) -> str | None:
    """The one-way valve's de-escalations, read off an unattended reply.

    A whole reply of exactly SKIP or HOLD (punctuation and quotes tolerated,
    case ignored) is the model declining or deferring — returned as the
    token. Anything else, including SKIP buried inside a sentence, is a
    reply to deliver: the valve only ever quiets an event, so ambiguity
    resolves toward speaking what the policy already approved.
    """
    match = re.fullmatch(r"\W*(skip|hold)\W*", reply, re.IGNORECASE)
    return match.group(1).upper() if match else None


def build_tts(config: Config) -> "TextToSpeech":
    """Instantiate the configured speech engine.

    The only place an engine is *chosen* from config — swapping them is a
    config value, which is the whole point of the protocol. (``_warm_up_tts``
    names ``SayTTS`` once more, on the runtime fallback path.)
    """
    from ciel.tts.macos_say import SayTTS

    engine: TextToSpeech | None = None
    if config.tts.engine == "piper":
        try:
            from ciel.tts.piper import PiperTTS

            engine = PiperTTS(config.tts)
        except ImportError:
            # Piper is an optional extra. A missing install should cost voice
            # quality, not the whole assistant.
            log.warning("piper is not installed — falling back to `say`")
    if engine is None:
        engine = SayTTS(config.tts)
    return _apply_effect(engine, config)


def _apply_effect(engine: "TextToSpeech", config: Config) -> "TextToSpeech":
    """Wrap the engine in the configured voice treatment, if any.

    Applied at build time rather than inside either engine so the effect
    survives an engine swap — and the fallback path re-applies it for the
    same reason: the treatment belongs to the voice's *character*, not to
    whichever synthesizer happens to be producing it."""
    if config.tts.effect == "jarvis":
        from ciel.tts.effect import VoiceEffect

        return VoiceEffect(engine)
    return engine


class _TextSink:
    """Delivery for the typed and web lanes: there is nothing to deliver —
    the console print is the typed lane's delivery and the transcript tap
    is the web lane's — so the hooks only keep the conversation flags
    honest. Text exchanges are conversations too: Closure owes them the
    same reflection, rotation the same fresh session afterwards."""

    def __init__(self, pipeline: "Pipeline", confirm_send=None) -> None:
        self._p = pipeline
        self.confirm_send = confirm_send

    def gate(self) -> bool:
        return True

    async def begin(self, started: float) -> None:
        return None

    async def escalation(self) -> bool:
        return True

    async def thinking(self, sentence: str) -> bool:
        self._flags()
        return True

    async def reply(self, sentence: str) -> bool:
        self._flags()
        return True

    def _flags(self) -> None:
        self._p._conversed = True
        self._p._brain_conversed = True

    async def finish(self, started: float) -> None:
        return None

    async def cleanup(self) -> None:
        return None


class _DiscordSink(_TextSink):
    """Delivery for the Discord lane: the reply is *buffered* and sent as
    one text rather than streamed — nobody is in the room to hear
    sentences land, and ten notifications for one answer is nagging. A
    deep-thought escalation sends an interim line, because a typing
    indicator alone reads as a hang after the first thirty seconds."""

    def __init__(self, pipeline: "Pipeline", send_here) -> None:
        super().__init__(pipeline, confirm_send=send_here)
        self._send = send_here
        self._sentences: list[str] = []

    async def escalation(self) -> bool:
        try:
            await self._send(
                "Give me a moment — I want to think this through properly."
            )
        except RemoteUnavailable:
            log.debug("could not text the escalation notice")
        return True

    async def reply(self, sentence: str) -> bool:
        self._sentences.append(sentence)
        self._flags()
        return True

    async def finish(self, started: float) -> None:
        reply = " ".join(self._sentences).strip()
        if reply:
            await self._send(reply)


class _VoiceSink:
    """Delivery for the spoken lane — the audio machinery a text lane
    never needs: the heard-ack filler, the thinking chime, the indicator
    dance, the first-speech metric, and barge-in's abort.

    The ack filler is the audible "I heard you": a short phrase spoken if
    the brain is still silent after a grace window, launched as a task so
    it overlaps the model's latency instead of adding to it. Every play
    awaits ``_ack_done`` first so two voices never overlap, and a first
    sentence that beats the window cancels the filler unspoken — "Let me
    think about that." chased instantly by the answer is theater.
    Deliberately never sets ``_spoke`` by itself: a filler alone must not
    open a follow-up window.
    """

    def __init__(self, pipeline: "Pipeline", player: "Player") -> None:
        self._p = pipeline
        self._player = player
        self.confirm_send = None
        self._started = 0.0
        self._first_spoken: float | None = None
        self._prev_kind: str | None = None
        self._ack_task: asyncio.Task[None] | None = None
        self._ack_speaking = False

    def gate(self) -> bool:
        return not self._p._interrupted

    async def begin(self, started: float) -> None:
        self._started = started
        self._p._interrupted = False
        if self._p._config.brain.ack_phrases:
            self._ack_task = asyncio.create_task(self._ack_filler())

    async def _ack_filler(self) -> None:
        await asyncio.sleep(self._p._config.brain.ack_delay_s)
        self._ack_speaking = True
        await self._player.play(
            self._p._tts.stream(random.choice(self._p._config.brain.ack_phrases))
        )

    async def _ack_done(self) -> None:
        if self._ack_task is None:
            return
        if not self._ack_speaking:
            # Still inside the grace window (or between sleep and speech,
            # which is the same thing: nothing audible yet) — skip it.
            self._ack_task.cancel()
        task, self._ack_task = self._ack_task, None
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                # The cancellation was aimed at *us*, not the filler —
                # swallowing it here would eat the turn's own teardown.
                raise
        except Exception:  # noqa: BLE001 — a lost filler costs nothing
            log.debug("ack playback failed", exc_info=True)

    async def _play(self, sentence: str) -> bool:
        """One sentence through the speakers, with the shared accounting:
        the filler resolved first, the first-speech stamp, the flags, and
        the between-sentences indicator. Returns False — after abandoning
        the turn — when playback didn't finish."""
        p = self._p
        await self._ack_done()
        if self._first_spoken is None:
            self._first_spoken = time.monotonic() - self._started
        completed = await self._player.play(p._tts.stream(sentence))
        p._spoke = True
        p._conversed = True
        p._brain_conversed = True
        # Back to "thinking" between sentences: the model is still
        # generating, and leaving it on "speaking" during the gap would
        # misreport what Ciel is actually doing.
        p._indicator.set_state("thinking")
        if not completed:
            # Either the user talked over us — everything queued behind
            # this sentence answers a question they've moved on from — or
            # the output device died and nothing is being heard. Both
            # abandon the turn; the recorded cause differs, and the
            # difference matters (see _abandon_playback).
            await p._abandon_playback(self._player.device_lost)
            return False
        return True

    async def escalation(self) -> bool:
        # The model just handed the question to deep-thought — from here
        # the room goes quiet for as long as the deep pass takes. If
        # nothing has been spoken yet this turn, name what is happening;
        # if the model already announced it in its own words, don't say
        # it twice.
        p = self._p
        if self._first_spoken is None:
            line = "Give me a moment. I want to think this through properly."
            print(f"  ciel: {line}")
            p._record("ciel", line)
            p._indicator.set_state("speaking")
            if not await self._play(line):
                # Barging in on the announcement means the deep pass must
                # die too — otherwise the subagent runs on for a minute
                # and then answers a question the user already abandoned.
                return False
        p._indicator.set_state("thinking")
        return True

    async def thinking(self, sentence: str) -> bool:
        # Whether reasoning is *spoken* is the speak_thinking choice.
        # Show-only reasoning keeps the indicator on "thinking" —
        # "reasoning" means the voice is doing it aloud, and lying about
        # that costs the pill its meaning.
        spoken = self._p._config.brain.speak_thinking
        self._p._indicator.set_state("reasoning" if spoken else "thinking")
        self._prev_kind = "thinking"
        if not spoken:
            return True
        return await self._play(sentence)

    async def reply(self, sentence: str) -> bool:
        p = self._p
        if self._prev_kind == "thinking" and p._config.brain.thinking_chime:
            # The audible boundary: reasoning is over, what follows is
            # the answer. When the reasoning was show-only this tone is
            # doing its best work — the deliberation was silent, and this
            # is what says the silence is about to end on purpose.
            await self._ack_done()
            await p._play_chime(self._player)
        p._indicator.set_state("speaking")
        self._prev_kind = "reply"
        return await self._play(sentence)

    async def finish(self, started: float) -> None:
        if self._first_spoken is None:
            return
        # Reconnect is called out separately because it dominates when it
        # happens at all: a client killed by a laptop sleeping overnight
        # is rebuilt lazily on the next turn, and that cost lands inside
        # "to first speech" where it reads as a slow model. Split, a long
        # turn names its own cause.
        reconnect = self._p._brain.last_reconnect_s
        log.info(
            "turn: %.2fs to first speech (%.2fs reconnect, %.2fs model), "
            "%.2fs total, $%.4f",
            self._first_spoken,
            reconnect,
            max(0.0, self._first_spoken - reconnect),
            time.monotonic() - started,
            self._p._brain.last_turn_cost_usd,
        )

    async def play_reply(self, reply: str) -> None:
        """A locally-handled command's spoken reply. ``_spoke`` is set
        unconditionally, exactly as brain sentences do: an interrupted
        reply must still open the follow-up window so the user's barge-in
        ("...and a five minute one too") is heard without re-waking."""
        await self._player.play(self._p._tts.stream(reply))
        self._p._spoke = True

    async def cleanup(self) -> None:
        # A brain that yields nothing (or raises) must not leave the
        # filler task dangling into the next turn.
        await self._ack_done()


class _WireSink:
    """Delivery for the hub's voice lane: the spoke's speakers, one hop
    away. Sentences go down the wire as ``turn.sentence`` frames and
    the spoke reports each one played (or stopped), so generation is
    paced by real speech exactly as the in-process voice sink paces it
    by the player — and a barge-in, reported as a sentence that did not
    complete, aborts the stream the same way. The ack filler, the
    thinking chime, and the follow-up window are the spoke's own; what
    stays here is the accounting the turn skeleton reads: the flags,
    the first-speech stamp, the indicator, the abandon.

    Confirmations travel the same socket: the broker's remote sink for
    origin ``spoke`` speaks the question through the spoke and opens
    its answer window (``listen``), and the answer comes back to
    ``broker.answer`` through the server's hook.
    """

    def __init__(self, pipeline: "Pipeline", server: HubServer, turn_id: str) -> None:
        self._p = pipeline
        self._server = server
        self.turn_id = turn_id
        self.confirm_send = self._confirm_send
        self._n = 0
        self._confirms = 0
        self._started = 0.0
        self._first_spoken: float | None = None
        self._prev_kind: str | None = None
        self._ended = False

    def gate(self) -> bool:
        return not self._p._interrupted

    async def begin(self, started: float) -> None:
        self._started = started
        self._p._interrupted = False
        self._server.send_spoke({
            "type": "turn.begin", "turn_id": self.turn_id, "lane": "voice",
        })

    async def _say(self, kind: str, sentence: str) -> bool:
        p = self._p
        self._n += 1
        ok = await self._server.speak(
            self.turn_id, self._n, kind, sentence, p._config.hub.speak_timeout_s
        )
        if self._first_spoken is None:
            self._first_spoken = time.monotonic() - self._started
        p._spoke = True
        p._conversed = True
        p._brain_conversed = True
        p._indicator.set_state("thinking")
        if not ok:
            # Stopped (barge-in), or nothing came back: the spoke left,
            # or its device died. Both abandon; the record differs.
            await p._abandon_playback(not self._server.spoke_connected)
            return False
        return True

    async def escalation(self) -> bool:
        p = self._p
        if self._first_spoken is None:
            line = "Give me a moment. I want to think this through properly."
            print(f"  ciel: {line}")
            p._record("ciel", line)
            p._indicator.set_state("speaking")
            if not await self._say("reply", line):
                return False
        p._indicator.set_state("thinking")
        return True

    async def thinking(self, sentence: str) -> bool:
        spoken = self._p._config.brain.speak_thinking
        self._p._indicator.set_state("reasoning" if spoken else "thinking")
        self._prev_kind = "thinking"
        if not spoken:
            return True
        return await self._say("thinking", sentence)

    async def reply(self, sentence: str) -> bool:
        self._p._indicator.set_state("speaking")
        self._prev_kind = "reply"
        return await self._say("reply", sentence)

    async def finish(self, started: float) -> None:
        # The non-exception path includes a barge-in (the stream broke
        # after an unfinished receipt): the end says so, and the spoke
        # closes its turn accordingly.
        self._end("interrupted" if self._p._interrupted else "done")
        if self._first_spoken is None:
            return
        reconnect = self._p._brain.last_reconnect_s
        log.info(
            "turn (spoke): %.2fs to first speech (%.2fs reconnect, %.2fs model), "
            "%.2fs total, $%.4f",
            self._first_spoken,
            reconnect,
            max(0.0, self._first_spoken - reconnect),
            time.monotonic() - started,
            self._p._brain.last_turn_cost_usd,
        )

    async def play_reply(self, reply: str) -> None:
        """A locally-handled command's spoken reply, through the spoke."""
        await self._say("reply", reply)

    async def apologize(self) -> None:
        """The spoken apology after a failed turn, best-effort."""
        with contextlib.suppress(Exception):
            await self._server.speak(
                self.turn_id, self._n + 1, "reply",
                "Sorry, something went wrong there.",
                self._p._config.hub.speak_timeout_s,
            )

    async def cleanup(self) -> None:
        if not self._ended:
            self._end("interrupted" if self._p._interrupted else "failed")

    def _end(self, status: str) -> None:
        self._ended = True
        self._server.send_spoke({
            "type": "turn.end", "turn_id": self.turn_id, "status": status,
        })

    async def _confirm_send(self, text: str, *, listen: bool = False) -> None:
        self._confirms += 1
        sent = self._server.send_spoke({
            "type": "confirm.request",
            "confirm_id": f"{self.turn_id}:{self._confirms}",
            "text": text,
            "deadline_wall": time.time() + self._p._config.hub.confirm_timeout_s,
            "listen": listen,
        })
        if not sent:
            raise RuntimeError("no spoke to ask")


class IdleSchedule:
    """Arms Closure and rotation when a spoken conversation ends.

    Pure timer logic, injectable clock via the ``now`` arguments, so the
    probe can drive it without waiting ten real minutes.

    The invariant: reflection gets one *opportunity* before rotation, and
    rotation is guaranteed regardless of how reflection went. ``due()``
    tracks reflect-*attempted*, never reflect-*succeeded* — a failed or
    timed-out reflection still counts as consumed, because a schedule that
    waits for a successful reflection is a schedule that can quietly
    recreate the permanent-session problem rotation exists to end.
    """

    def __init__(self, reflect_delay_s: float | None, rotate_delay_s: float) -> None:
        self._reflect_delay = reflect_delay_s
        self._rotate_delay = rotate_delay_s
        self._reflect_at: float | None = None
        self._rotate_at: float | None = None

    def conversation_ended(self, now: float, *, brain: bool = True) -> None:
        """Re-arms the deadlines — rotation always, relative to the *latest*
        conversation, not the first.

        Reflection is armed only when the brain took part: a local-command
        exchange ("ten minute timer") never touches the session, so reflecting
        after one is a full model turn over a transcript with nothing new in
        it. A reflect deadline already pending from an earlier brain
        conversation survives such an exchange untouched — that conversation
        is still owed its one opportunity before rotation."""
        if brain:
            self._reflect_at = (
                now + self._reflect_delay if self._reflect_delay is not None else None
            )
        self._rotate_at = now + self._rotate_delay

    def due(self, now: float) -> str | None:
        if self._reflect_at is not None and now >= self._reflect_at:
            self._reflect_at = None
            return "reflect"
        if (
            self._rotate_at is not None
            and self._reflect_at is None
            and now >= self._rotate_at
        ):
            self._rotate_at = None
            return "rotate"
        return None

    def clear(self) -> None:
        """Drop both deadlines without serving them.

        For waking from a sleep that outlived the resume window: the session
        the deadlines were armed for can no longer be resumed, so a
        reflection now would open a *fresh* session and reflect over nothing,
        and the rotation it guards is moot — the resume window already
        retired the session. Same reasoning as ``rehydrate_schedule`` arming
        nothing from a stale record; this is the running-process analog of
        that startup decision.
        """
        self._reflect_at = None
        self._rotate_at = None


def rehydrate_schedule(
    schedule: IdleSchedule,
    session_file: Path,
    window_minutes: float,
    *,
    now_wall: float | None = None,
    now_mono: float | None = None,
) -> bool:
    """Re-arm the schedule from the persisted session record, on startup.

    A restart (autoreload included) constructs a fresh, unarmed schedule —
    but the SDK session it resumes still owes its Closure and its rotation.
    Without this, a reload in the 60 seconds after a conversation silently
    skips reflection, and the resumed context lives forever. Arming with the
    conversation's *actual* end time keeps the deadlines honest: a young
    session reflects on the original clock, an older one reflects
    immediately and rotates on schedule.

    A record past the resume window arms nothing — the session won't be
    resumed, and there is no context left to reflect on. Returns whether the
    schedule was armed.
    """
    try:
        raw = json.loads(session_file.read_text())
        updated_at = float(raw["updated_at"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False

    now_wall = time.time() if now_wall is None else now_wall
    now_mono = time.monotonic() if now_mono is None else now_mono
    age_s = max(0.0, now_wall - updated_at)
    if age_s > window_minutes * 60:
        return False
    # The wall-clock age mapped onto the monotonic clock the schedule runs on.
    schedule.conversation_ended(now_mono - age_s)
    return True


class Pipeline:
    """Runs Ciel until stopped."""

    _world: World | None = None
    """The world table (Phase Space, ``world.py``), or None when
    ``[world]`` is off. A class default rather than only an instance
    attribute so a probe that builds a pipeline by ``__new__`` runs with
    no table and bare prompts, as every probe did before the table."""
    _next_world_push = 0.0
    _world_seen = -1
    _agenda_task: "asyncio.Task[None] | None" = None
    """The table's poll clock and the agenda refresher — class defaults
    for the same reason as ``_world``."""

    def __init__(self, config: Config, role: str = "local") -> None:
        self._config = config
        self._role = role
        """``local`` is the single process — today's Ciel, the rollback
        at every commit. ``hub`` is the same pipeline with no microphone
        anywhere: the audio machinery is never built, the voice lane's
        turns arrive from the spoke over the wire and its sentences go
        back the same way, and the main loop is the ladder on a tick
        instead of on a frame."""
        hub = role == "hub"
        self._stt: "SpeechToText | None" = None
        self._tts: "TextToSpeech | None" = None
        self._wake: "WakeDetector | None" = None
        self._endpointer: "Endpointer | None" = None
        self._speaker = None
        if not hub:
            from ciel.audio.speaker import build_speaker_gate
            from ciel.audio.vad import Endpointer
            from ciel.audio.wake import build_wake_detector
            from ciel.stt import build_stt

            self._stt = build_stt(config.stt)
            self._tts = build_tts(config)
            self._wake = build_wake_detector(config.wake)
            # The lambda defers to the running noise-floor estimate (tracked in
            # the frame loop) so endpointing in a noisy room demands speech
            # louder than the room — see Endpointer._clears_floor.
            self._endpointer = Endpointer(
                config.audio, noise_floor=lambda: self._noise_floor
            )
            self._speaker = build_speaker_gate(config.voice)

        # The web GUI (Chart): a user lane like the typed deque and the
        # Discord link, plus a *view* — every transcript row and state
        # change is teed into it. Created before the tools on the hub,
        # whose Mac-bound tools bind the wire, and before the indicator so
        # the tee below can include it.
        self._web_link: WebLink | None
        self._remote: "RemoteBindings | None" = None
        self._interview: "InterviewApp | None" = None
        if hub:
            # The hub's socket is the spoke's door; the Chart rides along
            # whether or not config asked for it.
            if not config.web.enabled:
                log.info("hub: the wire server is on regardless of [web].enabled")
            interview = None
            if config.interview.enabled:
                # The interview room (Adjoint): a second surface on this
                # same server for other people. Built here, handed to the
                # link, and never spoken of again — it has no lane, no
                # sink, and no path to the brain below.
                from ciel.interview.app import InterviewApp

                interview = InterviewApp(config)
            self._web_link = HubServer(config.web, config.hub, interview)
            self._interview = interview
            self._remote = RemoteBindings(self._web_link, config)
        else:
            self._web_link = (
                WebLink(config.web, config.hub) if config.web.enabled else None
            )

        # The world table (Phase Space): one place for everything Ciel
        # currently holds true, fed below by every producer, opening every
        # turn, mirrored to the Chart. Built before the tools so the ring
        # tool and world_now can bind it.
        self._world = World(config.world.file) if config.world.enabled else None
        self._next_world_push = 0.0
        """The table's once-a-second poll deadline: timers and watches
        are re-observed, the file flushed, the Chart told if the version
        moved — the agent roster's cadence and reasoning."""
        self._world_seen = -1
        """The table version last broadcast."""
        self._agenda_task: asyncio.Task[None] | None = None
        self._agenda_kick = asyncio.Event()
        """The agenda refresher (``_refresh_agenda``) and its hurry-up:
        set on a spoke connect and on wake from sleep."""

        # Custom tools (memory today, whatever lands in the registry later) are
        # assembled before the brain, because the memory index they expose has
        # to be baked into the system prompt at connect time.
        (
            self._mcp_servers,
            self._custom_tools,
            self._memory,
            self._projects,
            self._journal,
            self._timers,
        ) = build_tool_server(config, self._remote, self._world)
        if self._memory is not None and self._memory.all():
            log.info("loaded %d memories", len(self._memory.all()))

        if self._web_link is not None:
            self._web_link.on_mute = self._set_muted
            self._web_link.on_restart = self._request_restart

        self._mute_sentinel = config.state_dir / "mute"
        """The quiet brake, the hold sentinel's sibling: while this file
        exists, no sound leaves the speakers and the wake word is not
        watched for. A file rather than memory because mute must survive
        the autoreloader's constant re-execs — walking into a lecture
        with Ciel muted and having a source change un-mute it is the
        exact failure this prevents — and because `touch ~/.ciel/mute`
        from a hotkey or a shell should work without the GUI open."""
        self._muted = self._mute_sentinel.exists()
        if self._web_link is not None:
            # No clients yet — this just sets what the hello frame claims.
            self._web_link.note_muted(self._muted)
        if self._world is not None:
            self._world.observe(W.MUTED, self._muted, source=self._who)
        self._next_mute_check = 0.0
        self._next_agents_push = 0.0
        """The active-agent roster's once-a-second poll deadline (the mute
        sentinel's cadence). Polling beats wiring change hooks through
        every source that can move the roster — brain escalations,
        watches, timers — and the link dedupes, so an unchanged second
        costs one list build and one compare."""
        self._player: Player | None = None
        """The run() player, held for exactly one cross-cutting need: the
        mute switch stopping a sentence already in the air."""
        self._mic: MicStream | None = None
        """The run() microphone, for the drains the enactments do."""
        self._turn: asyncio.Task[None] | None = None
        """The turn in flight — audio-owning, re-raised via result()."""

        if hub:
            # No HUD: the spoke draws its own from the state frames the
            # wire indicator broadcasts to every client.
            assert self._web_link is not None
            self._indicator: Indicator = WebIndicator(self._web_link)
        else:
            indicator = build_indicator(config.ui)
            self._indicator = (
                indicator
                if self._web_link is None
                else TeeIndicator(indicator, WebIndicator(self._web_link))
            )
        # The broker exists even when the shell is disabled — it's inert until
        # someone calls ask(), and the brain only builds a ShellGuard around
        # it when config.shell.enabled says to.
        self._confirm = VoiceConfirmBroker(config)
        if hub:
            # The spoke's frames land here: its answers on the broker, its
            # barge-ins on the abandon path, its mute switch on ours.
            server = self._web_link
            assert isinstance(server, HubServer)
            server.on_confirm_answer = self._confirm.answer
            server.on_turn_cancel = self._on_turn_cancel
            server.on_spoke_mute = lambda muted: self._set_muted(muted, from_spoke=True)
            server.on_spoke_change = self._on_spoke_change
            server.on_fact = self._on_fact
            if self._world is not None:
                # Until the spoke says hello, the honest reading is "not
                # here" — a carried-over "connected" from the last process
                # would promise a room that may have gone.
                self._world.observe(
                    W.SPOKE, {"connected": False, "node": config.spoke.client_id},
                    source="hub",
                )
        # Bound methods, not strings: the brain re-reads these at every
        # connect, so a rotated session's prompt carries whatever the last
        # reflection wrote.
        self._brain = Brain(
            config,
            memory_index_provider=self._memory.index_prompt if self._memory else None,
            confirmer=self._confirm.ask,
            journal=self._journal,
            projects_index_provider=self._projects.index_prompt if self._projects else None,
            # Read-back: a confirmed outward action files a verify event; the
            # emitter no-ops until the Vigil queue exists (checked at call
            # time, so construction order doesn't matter).
            verify_emitter=self._emit_verify,
            # The Mac tools exist when the Mac is elsewhere.
            mac_tools=self._remote is not None
            and (config.shell.enabled or config.files.enabled),
        )
        # Memory writes carry provenance: "proactive" when a Vigil turn with
        # nobody around is writing, "conversation" otherwise — reflection
        # included, since it distills a conversation the user was part of.
        # Recall renders the hedge for proactive-written facts.
        bind_memory_context(
            lambda: "proactive"
            if self._brain.unattended_context == "proactive"
            else "conversation"
        )
        self._transcript: Transcript | None = (
            Transcript(config.transcripts.dir) if config.transcripts.enabled else None
        )
        self._state = State.WAITING
        self._interrupted = False
        self._barge_run = 0
        self._noise_floor: float | None = None
        self._followup_until: float | None = None
        self._spoke = False
        self._conversed = False  # any speech since the last WAITING — outlives _spoke
        self._brain_conversed = False  # a *brain* turn since the last WAITING —
        # local commands leave it False, so Closure never reflects a
        # conversation the session file has no trace of
        self._pending_text: str | None = None
        self._continue_listening = False
        self._typed: deque[tuple[float, str]] = deque()
        """Lines from stdin awaiting their turn (Isomorphism), each stamped with
        its monotonic arrival time. A deque, not a variable: lines can arrive
        faster than turns complete, and dropping one would silently eat a
        request the user watched themselves type. The timestamp lets a pending
        confirmation tell a genuine answer from a request that predates the
        question (see the frame loop)."""
        self._stdin_task: asyncio.Task[None] | None = None

        self._schedule = IdleSchedule(
            config.reflection.idle_delay_s if config.reflection.enabled else None,
            config.brain.resume_window_minutes * 60,
        )
        # A restart mid-window (autoreload does this constantly) resumes the
        # session but would otherwise forget it owes a reflection and a
        # rotation.
        if rehydrate_schedule(
            self._schedule, config.session_file, config.brain.resume_window_minutes
        ):
            log.info("maintenance deadlines rehydrated from the resumed session")
        self._maintenance: asyncio.Task[None] | None = None
        """Closure/rotation in flight. Deliberately not the ``turn`` variable:
        turns re-raise via turn.result(); maintenance swallows its failures."""
        self._last_wall = time.time()
        """Wall clock at the last frame, for sleep detection — see
        _SLEEP_GAP_S and _on_wake_from_sleep."""
        self._timer_missed_sweep = False
        """Set on wake-from-sleep: the next timer poll runs the grace-filtered
        missed sweep instead of pop_due, so an alarm that came due mid-nap is
        announced late-but-honestly and a six-hour-stale "rice is done" is
        dropped — the startup pop_missed behavior, applied to naps."""
        self._unattended_hastened = False
        """Set by every lane that hastens the maintenance slot with
        brain.interrupt(). An interrupt ends the SDK stream *normally*, so a
        composing unattended turn would otherwise carry on with a truncated
        sentence list — and a truncated compose must never leave the
        machine as a sent text or a held "finding". _compose_unattended
        clears it on entry and checks it after collecting."""

        # Vigil (see ciel.proactive): the queue is the only piece the frame
        # loop ever touches at frame rate, and only through its `pending`
        # boolean — policy, presence, and Quartz run at most once per
        # policy_poll_s, and only while something is actually waiting.
        self._events: EventQueue | None = None
        self._policy: InterruptionPolicy | None = None
        self._presence: PresenceProbe | None = None
        self._vigil_watchers: list = []
        self._work_watcher: WorkWatcher | None = None
        """Held by name (it also rides _vigil_watchers) because the agent
        roster reads its active() list."""
        self._next_policy_check = 0.0
        self._hold_sentinel = config.state_dir / "hold"
        """The emergency brake: while this file exists, no proactive event is
        delivered — they queue and wait. A file sentinel rather than config
        because it can be tripped from outside the process (`touch
        ~/.ciel/hold`), survives crashes and restarts, and lifting it is
        just as visible. In-flight speech and user-requested timers are
        untouched — the brake stops new starts, never running work."""
        self._hold_engaged = False  # log the engagement once, not per poll
        if self._world is not None:
            self._world.observe(W.HOLD, self._hold_sentinel.exists(), source=self._who)
        self._calendar: CalendarWatcher | GoogleCalendarWatcher | None = None
        self._owner_messages: MessagesClient | None = None
        # The Discord lane (Parallel Transport): a user lane like the typed
        # deque, not a Vigil outlet — it exists whether or not the proactive
        # layer does, because texting Ciel a question needs no watching.
        self._remote_link: DiscordLink | None = (
            DiscordLink(config.discord) if config.discord.enabled else None
        )
        # Where the user is: the tool works with or without Vigil (it reads
        # on demand); the watcher below only exists with it.
        self._locator: Locator | None = (
            Locator(config.location)
            if config.location.enabled and not hub
            else None
        )
        if self._locator is not None:
            bind_locator(self._locator)
            if self._world is not None:
                self._locator.bind_world(self._world)
        if config.proactive.enabled:
            self._events = EventQueue(
                config.proactive.state_file,
                config.proactive.held_max_age_h * 3600,
            )
            self._policy = InterruptionPolicy(config.proactive)
            if hub:
                # The Mac's watchers publish from the spoke: EventKit,
                # location, the work watcher, and the presence signals all
                # arrive over the wire. Portable watchers (the brief, the
                # ring, Google's calendar, the sections) run here.
                assert self._remote is not None and isinstance(self._web_link, HubServer)
                self._presence = self._remote.presence
                self._web_link.on_event = self._on_published_event
                self._web_link.on_presence = self._on_presence
                if config.proactive.calendar_enabled:
                    if config.proactive.calendar_source == "google":
                        self._calendar = GoogleCalendarWatcher(config.proactive, self._events)
                        self._vigil_watchers.append(self._calendar)
                    else:
                        self._calendar = self._remote.calendar
                self._work_watcher = self._remote.watcher
            else:
                self._presence = PresenceProbe(config.proactive)
                if config.proactive.calendar_enabled:
                    # Same contract, different source of truth: EventKit for
                    # calendars macOS syncs, Google's API for calendars that
                    # never touch this machine.
                    self._calendar = (
                        GoogleCalendarWatcher(config.proactive, self._events)
                        if config.proactive.calendar_source == "google"
                        else CalendarWatcher(config.proactive, self._events)
                    )
                    self._vigil_watchers.append(self._calendar)
                work = WorkWatcher(config.proactive.watches_file, self._events)
                bind_watcher(work)
                self._vigil_watchers.append(work)
                self._work_watcher = work
            if config.proactive.brief_time:
                self._vigil_watchers.append(
                    ScheduleWatcher(config.proactive, self._events)
                )
            if config.oura.armed and (
                config.oura.low_readiness > 0
                or config.oura.low_sleep > 0
                or config.oura.low_activity > 0
            ):
                # The ring's verdicts — a rough morning, a still day — at
                # most once each per day. Needs an authorization and at
                # least one threshold: all zero keeps the tool and drops
                # the nudges.
                self._vigil_watchers.append(
                    OuraWatcher(config.oura, self._events, world=self._world)
                )
            if config.sections.enabled and not hub:
                # A spot opening in a watched course section — the one
                # watcher whose whole value is the race, hence its own
                # quick poll. Cookie or watch list missing, it idles
                # with a warning rather than arming nothing silently.
                # On the hub it runs on the spoke instead: its cookie is
                # minted by a browser profile on the Mac.
                self._vigil_watchers.append(
                    SectionsWatcher(
                        config.sections, self._events, mail=config.mail,
                        world=self._world,
                    )
                )
            if self._locator is not None:
                self._vigil_watchers.append(
                    LocationWatcher(config.location, self._locator, self._events)
                )
            # The away outlet: all three switches, or it doesn't exist. A
            # separate client from the tools' binding on purpose — this send
            # path is pipeline-owned and never reachable from a turn.
            if (
                config.proactive.owner_handle
                and config.messages.enabled
                and config.messages.allow_send
            ):
                # On the hub the text goes through the spoke's Messages.app;
                # when the spoke is away, _run_proactive_message falls to
                # Discord (the away ladder inverts with the Mac asleep).
                self._owner_messages = (
                    self._remote.messages if self._remote is not None
                    else MessagesClient(config.messages)
                )
            away_armed = self._owner_messages is not None or (
                self._remote_link is not None
                and self._remote_link.armed
                and config.discord.proactive
            )
            log.info(
                "vigil enabled (%d watcher%s%s)",
                len(self._vigil_watchers),
                "" if len(self._vigil_watchers) == 1 else "s",
                ", away texting armed" if away_armed else "",
            )

        self._watcher: SourceWatcher | None = (
            SourceWatcher(default_roots(config.state_dir))
            if config.dev.autoreload
            else None
        )
        self.reload_requested = False
        """Set when the source changed and the loop exited to be re-exec'd.
        The entry point reads this after run() returns."""
        self._reload_pending = False
        self._reload_waiting_since: float | None = None
        """A spoken or typed "reload" landed; the frame loop performs it from
        idle, exactly as it does for a source change."""

    def _loop_snapshot(self) -> Snapshot:
        """The arbiter's view of this instant, frozen. Deadline arithmetic
        happens here (the missed-timer sweep flag, the policy-poll clock)
        so ``pick_next`` stays a pure membership test."""
        if self._role == "hub":
            # The spoke's state machine, as last reported: a turn running
            # here is BUSY; otherwise its open listening window is what
            # keeps the quiet lanes off the slot.
            server = self._web_link
            assert isinstance(server, HubServer)
            state = (
                State.BUSY if self._state is State.BUSY
                else State.LISTENING if server.spoke_listening
                else State.WAITING
            )
            speaking = server.spoke_speaking
            voice_pending = server.voice_pending
        else:
            assert self._endpointer is not None
            state = self._state
            speaking = self._endpointer.speaking
            voice_pending = False
        return Snapshot(
            state=state,
            confirm_active=self._confirm.active,
            endpointer_speaking=speaking,
            held_thought=self._pending_text is not None,
            timers_due=self._timers is not None
            and (self._timer_missed_sweep or self._timers.any_due()),
            voice_pending=voice_pending,
            typed_pending=bool(self._typed),
            web_pending=self._web_link is not None and self._web_link.pending,
            remote_pending=self._remote_link is not None
            and self._remote_link.pending,
            vigil_ready=self._events is not None
            and self._events.pending
            and time.monotonic() >= self._next_policy_check,
        )

    async def run(self) -> None:
        if self._role == "hub":
            await self._run_hub()
            return
        from ciel.audio.input import MicStream
        from ciel.audio.output import Player

        assert self._tts is not None and self._stt is not None
        await self._startup()

        # The mute gate rides inside the player: one choke point silences
        # everything — greeting, acks, rings, sentences, apologies — with
        # no call site knowing mute exists.
        player = Player(
            self._config.audio, self._tts.sample_rate, muted=lambda: self._muted
        )
        self._player = player
        self._turn = None

        try:
            async with MicStream(self._config.audio) as mic, player:
                self._mic = mic
                self._confirm.bind(
                    player=player,
                    mic=mic,
                    stt=self._stt,
                    tts=self._tts,
                    noise_floor=lambda: self._noise_floor,
                    record=self._record,
                )
                greeting = random.choice(self._config.wake.greeting_phrases)
                await player.play(self._tts.stream(greeting))
                mic.drain()
                self._stdin_task = asyncio.create_task(self._read_stdin())
                self._announce_ready()

                if self._timers is not None:
                    # Timers that came due while Ciel was off: announced late
                    # but honestly, within the grace window. pop_missed drops
                    # anything staler with a log line.
                    missed = self._timers.pop_missed(
                        self._config.timers.missed_grace_s
                    )
                    if missed:
                        await self._announce_timers(missed, missed=True)

                # Re-stamped here, on the eve of the first frame: the value
                # from __init__ is minutes stale after model warm-up and the
                # greeting, and a cold start slower than _SLEEP_GAP_S would
                # otherwise be misread as a wake from sleep — fabricating a
                # transcript row and, past the resume window, clearing the
                # deadlines rehydrate_schedule just armed.
                self._last_wall = time.time()

                # One loop, always consuming. The turn runs as a task rather
                # than being awaited here, because the moment this loop stops
                # pulling frames, barge-in becomes impossible — there is
                # nothing left listening for the user talking over Ciel.
                async for frame in mic.frames():
                    # Sleep detection first: one vDSO clock read per frame.
                    # Everything below runs against monotonic deadlines that
                    # paused with the process, so after a nap they are all
                    # quietly wrong until the wake handler reconciles them.
                    now_wall = time.time()
                    if now_wall - self._last_wall > _SLEEP_GAP_S:
                        self._on_wake_from_sleep(now_wall - self._last_wall)
                    self._last_wall = now_wall

                    if now_wall >= self._next_mute_check:
                        # The mute sentinel is polled, not only toggled from
                        # the GUI, so `touch ~/.ciel/mute` from a hotkey or
                        # another shell lands mid-run — the hold sentinel's
                        # trick, at the same once-a-second price.
                        self._next_mute_check = now_wall + 1.0
                        on_disk = self._mute_sentinel.exists()
                        if on_disk != self._muted:
                            self._set_muted(on_disk)

                    if (
                        self._web_link is not None
                        and now_wall >= self._next_agents_push
                    ):
                        # The GUI's agent roster, refreshed at the mute
                        # sentinel's cadence. Above the confirm gate on
                        # purpose: a deep-thought pass holding a turn open
                        # is exactly when the page should say so.
                        self._next_agents_push = now_wall + 1.0
                        self._web_link.note_agents(self._active_agents())
                    if now_wall >= self._next_world_push:
                        self._next_world_push = now_wall + 1.0
                        self._world_tick()

                    # The turn-slot arbitration: one frozen snapshot, one
                    # pure pick (the ladder lives in ciel.schedule — the
                    # order and its reasons are that module's docstring).
                    # The blocks below only *enact* the pick; nothing is
                    # consumed until its block claims it.
                    source = pick_next(self._loop_snapshot())
                    if await self._enact(source, frame):
                        continue

                    if self._state is State.WAITING:
                        if await self._idle_housekeeping():
                            self.reload_requested = True
                            break

                        # Only sampled here: while idle and silent, what the
                        # microphone hears is the room itself.
                        self._track_noise_floor(frame)
                        # Muted, Ciel holds its name as well as its tongue:
                        # a false wake in a lecture hall costs exactly the
                        # attention mute was bought to avoid, and a true one
                        # could only start a conversation the speakers are
                        # barred from finishing. The floor above still
                        # tracks, so hearing is sharp the moment mute lifts.
                        if not self._muted and self._wake.push(frame):
                            if self._maintenance is not None and not self._maintenance.done():
                                # Hasten whatever holds the maintenance slot
                                # (reflection, rotation, or a composing
                                # unattended turn — the flag tells that one
                                # its output is truncated): the user's turn
                                # serializes on the brain's lock, masked by
                                # the ack.
                                self._unattended_hastened = True
                                await self._brain.interrupt()
                            await self._acknowledge(player, mic)
                            self._enter_listening()

                    elif self._state is State.LISTENING:
                        if self._followup_expired():
                            pending, self._pending_text = self._pending_text, None
                            if pending:
                                # The user trailed off ("...um") and never came
                                # back. Answer what they said rather than
                                # silently forgetting they spoke.
                                self._state = State.BUSY
                                self._barge_run = 0
                                self._turn = asyncio.create_task(
                                    self._handle_turn(None, player, mic, pending_text=pending)
                                )
                            else:
                                self._enter_waiting()
                            continue
                        utterance = self._endpointer.push(frame)
                        if utterance is not None:
                            self._state = State.BUSY
                            self._barge_run = 0
                            self._turn = asyncio.create_task(
                                self._handle_turn(utterance, player, mic)
                            )

                    elif self._state is State.BUSY:
                        self._check_barge_in(frame, player)
                        if self._turn is not None and self._turn.done():
                            self._turn.result()  # surface any exception from the turn
                            self._turn = None
                            if self._continue_listening:
                                # The utterance ended mid-thought; go straight
                                # back to listening. No drain — nothing was
                                # played, and the queue holds whatever was said
                                # while transcription ran.
                                self._continue_listening = False
                                self._enter_continuation()
                            else:
                                mic.drain()
                                if self._spoke and self._config.audio.followup_ms > 0:
                                    self._enter_followup()
                                else:
                                    self._enter_waiting()
        finally:
            # Resolve any pending confirmation to deny *before* cancelling the
            # turn: the Agent SDK subprocess is awaiting that hook's decision,
            # and leaving it unresolved would hang brain.close() in shutdown.
            self._confirm.cancel("shutting down")
            self._confirm.unbind()
            if self._stdin_task is not None and not self._stdin_task.done():
                self._stdin_task.cancel()
            if self._maintenance is not None and not self._maintenance.done():
                self._maintenance.cancel()
            if self._turn is not None and not self._turn.done():
                self._turn.cancel()
            self._player = None
            self._mic = None
            await self._shutdown()

    async def _enact(self, source: Source, frame: bytes | None) -> bool:
        """Enact one arbitration of the ladder. True when the pick claimed
        this round — the caller skips its state machine — False for NONE.
        ``frame`` is the mic frame a pending confirmation is fed; the hub
        has none, its confirmations listen through the spoke."""

        # Enacted above the state dispatch, not inside BUSY: a
        # turn abandoned by barge-in leaves BUSY while its hook
        # can still be pending, and a pending confirmation that
        # stops receiving frames never resolves.
        if source is Source.CONFIRM:
            # A typed line while a question is pending is an
            # answer — but only if it arrived *after* the question
            # was put. A line typed earlier (while the turn was
            # still busy) is the user's next request, and consuming
            # it would eat that request and let a stray "yes"/"no"
            # in it silently decide the gated action. Predating
            # lines stay queued and become the next turn.
            asked = self._confirm.asked_at
            if self._typed and asked is not None:
                ts, line = self._typed[0]
                if ts >= asked and self._confirm.answer(line):
                    self._typed.popleft()
            if self._web_link is not None and asked is not None:
                # A GUI message answers like a typed line, not
                # like a Discord text: the chart binds loopback,
                # so its user is at the machine and heard — or
                # saw, as a ciel-confirm row — the question.
                # Same predating rule as the keyboard.
                item = self._web_link.peek()
                if (
                    item is not None
                    and item[0] >= asked
                    and self._confirm.answer(item[1])
                ):
                    self._web_link.pop()
            if (
                self._remote_link is not None
                and self._confirm.remote_active
                and self._confirm.remote_origin == "discord"
                and asked is not None
            ):
                # An owner text answers a *Discord-origin*
                # question, under the keyboard's predating rule.
                # Spoken-origin questions never accept remote
                # answers (see remote_active), and web-origin
                # ones don't either (see remote_origin): a yes
                # to a question the answerer never saw is not
                # an answer.
                item = self._remote_link.peek()
                if (
                    item is not None
                    and item[0] >= asked
                    and self._confirm.answer(item[1])
                ):
                    self._remote_link.pop()
            if frame is not None:
                self._confirm.feed(frame)
            return True

        if source is Source.TIMERS:
            # A due timer speaks the moment nothing else owns the
            # audio (the arbiter's quiet rule). Awaited inline,
            # like the wake acknowledgement: announcements are
            # seconds long, and the mic is drained after so
            # Ciel's own voice isn't transcribed as a turn.
            # First poll after a nap grace-filters what came due
            # mid-sleep instead of announcing it stale.
            sweep, self._timer_missed_sweep = self._timer_missed_sweep, False
            fired = (
                self._timers.pop_missed(self._config.timers.missed_grace_s)
                if sweep
                else self._timers.pop_due()
            )
            if fired:
                await self._announce_timers(fired, missed=sweep)
            return True

        if source is Source.VOICE:
            # The spoke already heard the room: the utterance arrives
            # whole, and takes the slot as the state machine's own
            # turns do in the single process — first among the lanes.
            server = self._web_link
            assert isinstance(server, HubServer)
            if self._maintenance is not None and not self._maintenance.done():
                self._unattended_hastened = True
                await self._brain.interrupt()
            self._state = State.BUSY
            self._barge_run = 0
            item = server.pop_voice()
            assert item is not None  # pending was just checked
            self._turn = asyncio.create_task(self._handle_voice_wire_turn(item[1]))
            return True

        if source is Source.TYPED:
            # The keyboard takes the turn, but never *steals* one
            # (the arbiter's quiet rule: not over a running turn,
            # not mid-utterance, not over a held thought).
            if self._maintenance is not None and not self._maintenance.done():
                # Same hastening the wake path does: the typed
                # turn serializes on the brain's lock, and an
                # unhurried reflection would hold it for seconds.
                self._unattended_hastened = True
                await self._brain.interrupt()
            self._state = State.BUSY
            self._barge_run = 0
            _ts, line = self._typed.popleft()
            self._turn = asyncio.create_task(
                self._handle_typed_turn(line)
            )
            return True

        if source is Source.WEB:
            # The GUI claims the turn slot under the keyboard's
            # rules, not the phone's: a chart is the user *at*
            # the machine, so it may take a turn out of a
            # follow-up window. Queued bursts coalesce into one
            # turn, the texting-cadence reasoning.
            if self._maintenance is not None and not self._maintenance.done():
                # Same hastening as the typed lane: the web turn
                # serializes on the brain's lock.
                self._unattended_hastened = True
                await self._brain.interrupt()
            self._state = State.BUSY
            self._barge_run = 0
            batch = self._web_link.pop_batch()
            assert batch is not None  # pending was just checked
            line, _channel = batch
            self._turn = asyncio.create_task(self._handle_web_turn(line))
            return True

        if source is Source.REMOTE:
            # The Discord lane claims the turn slot — a user
            # lane, so it outranks Vigil, but only from WAITING
            # (the arbiter's rule: the room outranks the phone).
            # Everything queued right now coalesces into one
            # turn (people text in bursts; three turns for one
            # thought answers the greeting with a paragraph).
            if self._maintenance is not None and not self._maintenance.done():
                # Same hastening as the typed lane: the remote
                # turn serializes on the brain's lock.
                self._unattended_hastened = True
                await self._brain.interrupt()
            self._state = State.BUSY
            self._barge_run = 0
            # Coalesces the head run of same-channel messages;
            # a DM and a server mention never fuse into one
            # turn — different audiences, different replies.
            batch = self._remote_link.pop_batch()
            assert batch is not None  # pending was just checked
            line, channel = batch
            self._turn = asyncio.create_task(
                self._handle_remote_turn(line, channel)
            )
            return True

        if source is Source.VIGIL:
            # Vigil: a pending event gets its policy decision.
            # Last in line — the user always outranks the
            # machine — and stricter than the timer lane on
            # purpose: WAITING only, never into a live listening
            # window; the held-notes path covers those moments.
            # The frame-rate cost is the snapshot's booleans;
            # presence and policy run at most once per
            # policy_poll_s.
            self._next_policy_check = (
                time.monotonic()
                + self._config.proactive.policy_poll_s
            )
            if self._hold_sentinel.exists():
                # The emergency brake (see __init__): events wait,
                # nothing is dropped, and the engagement is logged
                # once rather than every poll.
                if not self._hold_engaged:
                    self._hold_engaged = True
                    log.info(
                        "vigil holding — remove %s to resume",
                        self._hold_sentinel,
                    )
                    if self._world is not None:
                        self._world.observe(W.HOLD, True, source=self._who)
                return True
            if self._hold_engaged:
                self._hold_engaged = False
                log.info("vigil hold lifted")
                if self._world is not None:
                    self._world.observe(W.HOLD, False, source=self._who)
            # The event is *peeked*, not popped: only an enacted
            # decision claims it (begin), so a busy turn slot
            # simply leaves it queued — no pop-and-push churn
            # rewriting the state file every poll, no event
            # rotating to the back for being unlucky.
            decision, event = self._decide_proactive()
            if event is not None and decision is not None:
                if decision.action == "speak" and self._muted:
                    # The nudge earned a voice the room must not
                    # hear. Held instead of spoken-silently or
                    # dropped: it rides into the next turn on
                    # whichever lane that arrives — the
                    # held-notes contract.
                    self._events.begin(event)
                    self._events.hold(event)
                elif decision.action == "speak":
                    if self._maintenance is not None and not self._maintenance.done():
                        # Same hastening as the typed lane: the
                        # proactive turn serializes on the
                        # brain's lock.
                        self._unattended_hastened = True
                        await self._brain.interrupt()
                    self._events.begin(event)
                    self._state = State.BUSY
                    self._barge_run = 0
                    self._turn = asyncio.create_task(
                        self._run_proactive_turn(event)
                    )
                elif decision.action in ("message", "note"):
                    # No audio involved, so these run in the
                    # maintenance slot — never as the
                    # audio-owning `turn`. Slot busy: the event
                    # stays peeked-not-claimed, retried next
                    # poll.
                    if self._maintenance is None or self._maintenance.done():
                        self._events.begin(event)
                        runner = (
                            self._run_proactive_message(event)
                            if decision.action == "message"
                            else self._run_proactive_note(event)
                        )
                        self._maintenance = asyncio.create_task(runner)
                elif decision.action == "hold":
                    self._events.begin(event)
                    self._events.hold(event)
                else:
                    self._events.begin(event)
                    self._events.drop(event, time.time())
            return True
        return False

    async def _idle_housekeeping(self) -> bool:
        """The idle slot's two duties — a pending reload, then maintenance
        (Closure, rotation). Returns True when the loop should exit to be
        re-exec'd. Shared by the frame loop and the hub's tick."""
        # Reload only from idle: never yank the process out
        # from under a conversation in progress. Triggered by
        # a source change or by the user asking for one.
        source_changed = (
            self._watcher is not None and self._watcher.changed
        )
        if source_changed or self._reload_pending:
            if self._interview is not None and self._interview.live_count:
                # The interview room is a surface for other people: a
                # reload waits for their interviews to end, and past a
                # long ceiling ends them gently (debrief and all) rather
                # than vanishing mid-question.
                if self._reload_waiting_since is None:
                    self._reload_waiting_since = time.monotonic()
                    log.info(
                        "reload waits on %d live interview(s)", self._interview.live_count
                    )
                waited = time.monotonic() - self._reload_waiting_since
                if waited < self._config.interview.reload_max_wait_s:
                    return False
                log.warning("reload waited %.0fs — pausing live interviews", waited)
                await self._interview.pause_all()
            self._reload_waiting_since = None
            if source_changed:
                print(f"\nsource changed ({self._watcher.changed.name}) — reloading")
            else:
                print("\nreload requested — reloading")
            # Let maintenance land first: re-exec'ing while
            # rotate() is mid-connect leaves a half-built SDK
            # subprocess behind. Bounded — a hung task is
            # cancelled rather than blocking the reload.
            if self._maintenance is not None and not self._maintenance.done():
                try:
                    await asyncio.wait_for(self._maintenance, timeout=10.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._maintenance.cancel()
            if self._player is not None and self._tts is not None:
                try:
                    await self._player.play(self._tts.stream("Reloading."))
                except Exception:  # noqa: BLE001
                    log.debug("could not announce the reload", exc_info=True)
            # Those awaits kept the loop serving sockets: a
            # turn that landed meanwhile is already
            # receipted (the web lane acks at arrival), so
            # re-exec'ing now would void a delivered line.
            # Serve it instead; the flags stay set and the
            # reload re-fires from the next idle frame.
            if (
                self._typed
                or (
                    self._web_link is not None
                    and self._web_link.pending
                )
                or (
                    self._remote_link is not None
                    and self._remote_link.pending
                )
            ):
                return False
            return True

        # Idle is when maintenance runs — Closure shortly
        # after a conversation ends, rotation when the thread
        # has been silent past the resume window. Same
        # only-from-idle reasoning as the reload above.
        if self._maintenance is not None and self._maintenance.done():
            self._maintenance = None  # _run_* swallow their errors
        if self._maintenance is None:
            due = self._schedule.due(time.monotonic())
            if due == "reflect":
                self._maintenance = asyncio.create_task(
                    self._run_reflection()
                )
            elif due == "rotate":
                self._maintenance = asyncio.create_task(
                    self._run_rotation()
                )
        return False

    async def _run_hub(self) -> None:
        """The hub's main loop: the ladder on a tick, no microphone anywhere.

        Wakes on the server's stir (a frame arrived, a client came or
        went) or every tenth of a second for the clocks — timers, the
        policy poll, the maintenance deadlines — then does exactly what
        the frame loop does between frames: a snapshot, a pick, one
        enactment. The spoke's state machine is in the snapshot by
        report; its turns arrive whole as voice says.
        """
        server = self._web_link
        assert isinstance(server, HubServer)
        await self._startup()
        self._confirm.bind_record(self._record)
        self._stdin_task = asyncio.create_task(self._read_stdin())
        self._announce_ready()
        if self._timers is not None:
            missed = self._timers.pop_missed(self._config.timers.missed_grace_s)
            if missed:
                await self._announce_timers(missed, missed=True)
        self._last_wall = time.time()
        try:
            while True:
                try:
                    await asyncio.wait_for(server.stir.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                server.stir.clear()

                now_wall = time.time()
                if now_wall - self._last_wall > _SLEEP_GAP_S:
                    self._on_wake_from_sleep(now_wall - self._last_wall)
                self._last_wall = now_wall
                if now_wall >= self._next_agents_push:
                    self._next_agents_push = now_wall + 1.0
                    server.note_agents(self._active_agents())
                    if self._timers is not None:
                        # The spoke's mirror: the whole set, once a second,
                        # deduped at the server — the alarm clock must not
                        # depend on this machine being reachable.
                        server.note_timers([
                            {"id": t.id, "kind": t.kind, "due_at": t.due_at,
                             "label": t.label, "duration_s": t.duration_s,
                             "pending": t.pending}
                            for t in self._timers.active()
                        ])
                if now_wall >= self._next_world_push:
                    self._next_world_push = now_wall + 1.0
                    self._world_tick()

                source = pick_next(self._loop_snapshot())
                if await self._enact(source, None):
                    continue

                if self._state is State.BUSY:
                    if self._turn is not None and self._turn.done():
                        self._turn.result()
                        self._turn = None
                        self._enter_waiting()
                elif self._state is State.WAITING:
                    if await self._idle_housekeeping():
                        self.reload_requested = True
                        log.info("hub loop ending for a reload")
                        break
        finally:
            log.info("hub loop ended (reload=%s)", self.reload_requested)
            self._confirm.cancel("shutting down")
            self._confirm.unbind()
            if self._stdin_task is not None and not self._stdin_task.done():
                self._stdin_task.cancel()
            if self._maintenance is not None and not self._maintenance.done():
                self._maintenance.cancel()
            if self._turn is not None and not self._turn.done():
                self._turn.cancel()
            await self._shutdown()

    async def _handle_voice_wire_turn(self, text: str) -> None:
        """One spoken turn that arrived from the spoke — the voice lane
        with the room one hop away. The guard ``_handle_turn`` keeps
        around the STT prelude lives here instead: a failed turn is
        logged, marked, and apologized for through the spoke."""
        server = self._web_link
        assert isinstance(server, HubServer)
        sink = _WireSink(self, server, secrets.token_urlsafe(6))
        try:
            await self._run_turn(
                TurnRequest(lane="voice", text=text, arrival_wall=time.time()),
                sink,
            )
        except Exception:
            log.exception("turn failed")
            self._indicator.set_state("error")
            self._record("event", "turn failed")
            await sink.apologize()

    def _on_turn_cancel(self, turn_id: str, reason: str) -> None:
        """The spoke barged in between sentences (during a sentence, the
        unfinished receipt already abandons the turn)."""
        if self._state is not State.BUSY or self._interrupted:
            return
        log.debug("spoke cancelled turn %s (%s)", turn_id, reason or "no reason")
        asyncio.get_running_loop().create_task(self._abandon_playback(False))

    def _on_published_event(self, raw: dict[str, Any]) -> bool:
        """A Mac watcher's event, arrived over the wire — into the queue,
        whose dedupe absorbs a resend. Malformed ones are refused loudly;
        the publisher is code, not a user."""
        if self._events is None:
            return False
        event = ProactiveEvent(
            id=str(raw["id"]),
            source=str(raw["source"]),
            importance=int(raw["importance"]),
            created_at=float(raw["created_at"]),
            expires_at=float(raw["expires_at"]) if raw.get("expires_at") is not None else None,
            summary=str(raw["summary"]),
            dedupe_key=str(raw["dedupe_key"]),
            payload={str(k): str(v) for k, v in (raw.get("payload") or {}).items()},
            deliver_after=float(raw["deliver_after"]) if raw.get("deliver_after") is not None else None,
        )
        if self._events.push(event):
            log.info("event published from the Mac: %s", event.summary)
            return True
        return False

    def _on_presence(self, frame: dict[str, Any]) -> None:
        """The spoke's heartbeat: the presence view and the watch roster."""
        if self._remote is None:
            return
        self._remote.presence.observe(frame, time.monotonic())
        watches = frame.get("watches")
        if isinstance(watches, list):
            self._remote.watcher.observe(watches)
        if self._world is not None:
            self._observe_presence(
                source=f"spoke:{frame.get('node') or self._config.spoke.client_id}",
                ttl_s=self._config.hub.presence_stale_s,
            )

    def _on_fact(self, frame: dict[str, Any]) -> None:
        """One world fact from the spoke — its place, the sections'
        spots — into the table, source-stamped with the node."""
        if self._world is None:
            return
        node = str(frame.get("node") or self._config.spoke.client_id)
        source = str(frame.get("source") or node)
        stamped = source if source == node else f"{source}@{node}"
        if not self._world.absorb(str(frame["name"]), frame, source=stamped):
            log.debug("fact %r from the spoke refused", frame.get("name"))

    def _on_spoke_change(self, connected: bool) -> None:
        """The spoke took or left the seat. Leaving mid-question denies it
        (the barge-in rule: nobody is going to answer), and the wait a
        sink has open resolves on its own (the server fails it)."""
        notice = "spoke connected" if connected else "spoke disconnected"
        print(f"\n  [{notice}]", flush=True)
        self._record("event", notice)
        if not connected:
            self._confirm.cancel("spoke gone")
        if self._world is not None:
            self._world.observe(
                W.SPOKE, {"connected": connected, "node": self._config.spoke.client_id},
                source="hub",
            )
        if connected and self._agenda_task is not None:
            self._agenda_kick.set()

    # ── one conversational turn ──────────────────────────────────────────────

    async def _handle_turn(
        self,
        utterance,
        player: "Player",
        mic: "MicStream",
        pending_text: str | None = None,
    ) -> None:
        # Transcription takes about a second, which is long enough that the
        # indicator should already say "thinking" rather than sit on
        # "listening" after the user has clearly stopped.
        self._indicator.set_state("thinking")
        # A turn that never actually speaks — nothing intelligible, or an
        # error before the first sentence — must not open a follow-up window.
        self._spoke = False

        # Everything fallible stays inside this guard: the turn runs as a
        # task, and an exception that escapes it is re-raised by
        # ``turn.result()`` in the frame loop — taking down the whole
        # assistant instead of just this turn.
        try:
            if utterance is not None and self._speaker is not None:
                # Identity first, transcription second: a stranger's sentence
                # costs one ~30 ms embedding and never reaches Whisper, the
                # brain, or a follow-up window.
                recognized, similarity = await self._speaker.check(utterance)
                if not recognized:
                    reason = (
                        "too short to verify outside a conversation"
                        if math.isnan(similarity)
                        else f"unrecognized voice ({similarity:.2f})"
                    )
                    log.info("%s", reason)
                    print(f"\n  [{reason} — ignored]", flush=True)
                    self._record("event", reason)
                    if self._pending_text is None:
                        return
                    # A stranger spoke during a continuation window, but the
                    # user's held thought is still owed an answer — the same
                    # courtesy the followup-expiry and noise-blip paths give.
                    # Drop the stranger's audio and answer what the user had
                    # already said by falling through with no utterance.
                    utterance = None

            if utterance is not None:
                heard = await self._stt.transcribe(utterance)
                pending = self._pending_text
                if not heard and not pending:
                    log.debug("nothing intelligible in %.2fs of audio", len(utterance) / SAMPLE_RATE)
                    return

                # A dismissal is checked against what was *just said*, before
                # the merge below — "set a timer for... um" followed by
                # "never mind" is the user cancelling the held thought, and
                # merging would bury the dismissal inside a nonsense request.
                if heard and self._config.commands.enabled:
                    cmd = match_command(heard)
                    if cmd is not None and cmd.kind == "dismiss":
                        await self._run_local_command(cmd, heard, player)
                        return

                if heard:
                    text = f"{pending} {heard}".strip() if pending else heard
                    if self._trails_off(heard):
                        # The endpoint fired on hesitation, not completion.
                        # Hold the turn and go back to listening rather than
                        # answering half a thought.
                        self._pending_text = text
                        self._continue_listening = True
                        print(f"\n  you (…): {text}", flush=True)
                        return
                else:
                    # A noise blip ended the continuation window; the held
                    # thought is all there is, so answer that.
                    text = pending
                self._pending_text = None
            else:
                # Nothing more was said, or a stranger interrupted the
                # continuation window. Either way, answer the held thought:
                # pending_text carries it when the window lapsed, and
                # self._pending_text when a rejected utterance dropped us here.
                text = pending_text if pending_text is not None else self._pending_text
                self._pending_text = None
            assert text is not None

            await self._run_turn(
                TurnRequest(lane="voice", text=text, arrival_wall=time.time()),
                _VoiceSink(self, player),
            )
        except Exception:
            self._pending_text = None
            log.exception("turn failed")
            self._indicator.set_state("error")
            self._record("event", "turn failed")
            try:
                await player.play(self._tts.stream("Sorry, something went wrong there."))
            except Exception:
                # The apology is best-effort; a TTS that is itself broken must
                # not escalate a failed turn into a dead assistant.
                log.debug("could not speak the failure notice", exc_info=True)
        finally:
            # A timer armed during this turn starts counting here, as the
            # reply's audio ends — so the "starting now" the user just heard
            # is when the count actually starts, not the tool call seconds
            # earlier. In the finally so a turn that errors after arming
            # still starts what it promised. Guarded: this is disk I/O in a
            # finally, and an OSError escaping here is re-raised by turn.result()
            # in the frame loop, taking the whole assistant down over a timer
            # that merely failed to persist.
            if self._timers is not None:
                try:
                    self._timers.commit_pending()
                except Exception:  # noqa: BLE001 - a failed commit must not crash the loop
                    log.exception("could not start a pending timer")
            # Ciel's own voice was captured while the speakers were live;
            # drained so it isn't transcribed as the user's next turn. Not on
            # the continuation path though: nothing was played there, and the
            # queue holds anything said while transcription was running.
            if not self._continue_listening:
                mic.drain()

    def _trails_off(self, heard: str) -> bool:
        if self._config.audio.filler_extend_ms <= 0:
            return False
        return trails_off(heard, self._config.audio)

    async def _handle_typed_turn(self, text: str) -> None:
        """One turn that arrived through the keyboard (Isomorphism).

        Same brain, same session, same transcript as a spoken turn — the
        differences are all at the edges. Inbound: no STT and no speaker
        gate; a keystroke needs neither transcription nor identity proof
        against the TV. Outbound: no TTS — typing instead of talking is a
        request for quiet, so the reply streams to the console only.

        ``_spoke`` stays False on purpose: the follow-up window exists to
        waive the wake word, and the keyboard never needed one — every line
        is already a turn. A confirm-tier tool mid-turn still voices its
        question (the broker owns that choreography), but a typed yes or no
        answers it just as well.
        """
        await self._run_turn(
            TurnRequest(lane="typed", text=text, arrival_wall=time.time()),
            _TextSink(self),
        )

    async def _handle_remote_turn(self, text: str, channel=None) -> None:
        """One turn that arrived over the Discord link (Parallel Transport).

        The typed lane's remote image: same brain, same session, same
        transcript. The differences are where the user is. The reply is
        *buffered* and sent as one text rather than streamed — nobody is
        in the room to hear sentences land, and ten notifications for one
        answer is nagging. Confirmations go over the link too
        (``confirm.remote``): the person who must say yes is wherever
        their phone is. And the transcript rows carry a remote speaker
        label, because ``user`` rows are presence evidence and a remote
        turn is evidence of exactly the opposite — Vigil must keep
        treating the room as empty, or it starts speaking nudges to
        nobody instead of texting them.

        ``channel`` is where the reply belongs — the owner's DM (None
        falls back to it), or the server channel an @mention came from.
        A guild channel makes the turn *public*: the system note switches
        to the discretion variant, and the held Vigil notes stay held —
        they are the owner's private catch-up, not channel content.
        """
        assert self._remote_link is not None

        async def send_here(reply_text: str) -> None:
            assert self._remote_link is not None
            await self._remote_link.send(reply_text, channel)

        await self._run_turn(
            TurnRequest(
                lane="discord",
                text=text,
                channel=channel,
                public=getattr(channel, "guild", None) is not None,
                arrival_wall=time.time(),
            ),
            _DiscordSink(self, send_here),
        )

    async def _handle_web_turn(self, text: str) -> None:
        """One turn that arrived through the GUI (Chart).

        The typed lane with the room's quietness made explicit: same
        brain, same session, same transcript, no audio on either side.
        Nothing here sends the reply — the transcript tap already streams
        every row into the page, sentence by sentence, so delivery is a
        side effect of recording. Confirmations route over the link
        (``confirm.remote``): a voiced question would ask the room, and
        the whole reason this lane is in use is that the room must not
        hear one — the question lands on the page, and a GUI yes or no
        (or a typed one) answers it.

        ``_spoke`` stays False for the typed lane's reason: every chart
        message is already a turn; there is no wake word to waive.
        """
        assert self._web_link is not None
        await self._run_turn(
            TurnRequest(lane="web", text=text, arrival_wall=time.time()),
            _TextSink(self, confirm_send=self._web_link.send),
        )

    async def _run_turn(self, req: TurnRequest, sink: TurnSink) -> None:
        """One user turn, whatever lane it rode in on — the single copy of
        the skeleton the four lanes used to hand-wire separately.

        The shape: record the user's raw words, try the local-command
        bypass, glue the lane's system note and the held Vigil notes onto
        the prompt (prefix, not transcript — the record never lies about
        what was said), stream the brain through the lane's sink, then
        let the sink deliver whatever it buffered. The sink is the
        delivery half (audio, one text, or nothing because the transcript
        tap already feeds the page); everything here is lane-independent,
        which is what lets the hub drive this same method with a sink
        that writes wire frames.

        Voice-lane exceptions re-raise to ``_handle_turn``, whose guard
        also covers the STT prelude and owns the spoken apology; text
        lanes handle their own here, shaped per lane.
        """
        spec = lane_spec(req)
        self._indicator.set_state("thinking")
        # A turn that never actually speaks must not open a follow-up
        # window; the voice sink sets it back the moment audio plays.
        self._spoke = False
        try:
            print(f"\n  {spec.console_tag}: {req.text}")
            self._record(spec.label, req.text)

            # Mechanical requests skip the brain entirely: no model
            # latency, no cost, no session traffic. The grammar only fires
            # on whole utterances it is certain about; everything else
            # takes the normal path, so a missed match costs nothing new.
            cmd = match_command(req.text) if self._config.commands.enabled else None
            if cmd is not None and (
                cmd.kind in ("reload", "dismiss") or self._timers is not None
            ):
                await self._run_lane_command(cmd, req, spec, sink)
                return

            started = time.monotonic()
            # Held Vigil notes, after the local-command bypass — a "cancel
            # the timer" line shouldn't consume notes no model will see —
            # and never into a public channel: notes are the owner's
            # private catch-up, not channel content.
            # The lane's note says where the reply lands; the world block
            # says what is true. Note first (it frames the whole turn),
            # then the readings, then the held notes, then the words.
            note = prompt_note(req, muted=self._muted)
            prompt = note + self._world_block() + (
                self._with_held_notes(req.text) if spec.held_notes else req.text
            )
            await sink.begin(started)
            async with AsyncExitStack() as stack:
                confirm_origin = spec.confirm_origin
                if confirm_origin is None and self._role == "hub":
                    # The hub's own voice lane: the question is spoken
                    # by the spoke and answered in the room, through
                    # the same remote choreography.
                    confirm_origin = "spoke"
                if confirm_origin is not None:
                    # Confirmations route over the lane: the person who
                    # must say yes is wherever their reply lands.
                    stack.enter_context(
                        self._confirm.remote(
                            sink.confirm_send, origin=confirm_origin
                        )
                    )
                if req.lane == "discord":
                    assert self._remote_link is not None
                    await stack.enter_async_context(
                        self._remote_link.typing(req.channel)
                    )
                # aclosing is load-bearing, not tidiness: ask() holds the
                # brain's turn lock, and a break below abandons the
                # generator — without an explicit close, the lock stays
                # held until garbage collection, and the next turn (or a
                # reflection) deadlocks behind it.
                stream = await stack.enter_async_context(
                    aclosing(self._brain.ask(prompt))
                )
                async for kind, sentence in stream:
                    if not sink.gate():
                        break
                    if kind == "escalation":
                        print("  [deep thought engaged]")
                        self._record("event", "deep thought engaged")
                        if not await sink.escalation():
                            break
                        continue
                    if kind == "thinking":
                        print(f"  ciel (thinking): {sentence}")
                        self._record("ciel-thinking", sentence)
                        if not await sink.thinking(sentence):
                            break
                    else:
                        print(f"  ciel: {sentence}")
                        self._record("ciel", sentence)
                        if not await sink.reply(sentence):
                            break
            await sink.finish(started)
            if spec.log_name == "remote":
                log.info(
                    "remote turn (%s): %.2fs total, $%.4f",
                    spec.origin,
                    time.monotonic() - started,
                    self._brain.last_turn_cost_usd,
                )
            elif spec.log_name is not None:
                log.info(
                    "%s turn: %.2fs total, $%.4f",
                    spec.log_name,
                    time.monotonic() - started,
                    self._brain.last_turn_cost_usd,
                )
        except RemoteUnavailable as exc:
            if req.lane != "discord":
                raise
            # The turn ran; only delivery failed. Logged, not retried — the
            # link reconnects on its own, and the user will ask again the
            # moment they notice silence. The transcript already holds the
            # undelivered reply.
            log.warning("remote reply undeliverable (%s)", exc)
            self._record("event", "remote reply undeliverable")
        except Exception:
            if req.lane == "voice":
                raise
            log.exception("%s turn failed", spec.log_name)
            self._indicator.set_state("error")
            if req.lane == "typed":
                self._record("event", "typed turn failed")
                print("  (something went wrong there — see the log)", flush=True)
            elif req.lane == "web":
                # An event row, so the page itself shows what happened —
                # the web lane's version of the spoken apology.
                self._record("event", "web turn failed — see the log")
            else:
                self._record("event", "remote turn failed")
                try:
                    await sink.confirm_send(
                        "Sorry — something went wrong with that one."
                    )
                except Exception:  # noqa: BLE001 - best-effort, like the spoken apology
                    log.debug("could not text the failure notice", exc_info=True)
        finally:
            # Release the sink first (the voice lane's ack filler), then
            # anchor timers: a timer armed during this turn starts counting
            # as the turn's delivery ends, so the "starting now" the user
            # just heard is when the count actually starts. Guarded — a
            # failed persist must not crash the loop.
            await sink.cleanup()
            if self._timers is not None:
                try:
                    self._timers.commit_pending()
                except Exception:  # noqa: BLE001 - a failed commit must not crash the loop
                    log.exception("could not start a pending timer")

    async def _run_lane_command(
        self, cmd: Command, req: TurnRequest, spec, sink: TurnSink
    ) -> None:
        """The local-command bypass, shaped per lane at the edges only:
        where the reply lands, and whether a quiet "reload" earns an
        acknowledgement — its usual answer, the spoken "Reloading.",
        happens in a room the user may not be in or hearing."""
        if cmd.kind != "dismiss":
            # A dismissal alone is not a conversation: an accidental wake
            # ended with "never mind" should not arm reflection and
            # rotation over an exchange with nothing in it.
            self._conversed = True
        if req.lane == "voice":
            log.info("local command: %s", cmd.kind)
        else:
            log.info("local command (%s): %s", spec.log_name, cmd.kind)
        reply = self._apply_local_command(cmd)
        if reply is None and cmd.kind == "reload" and spec.reload_ack is not None:
            reply = spec.reload_ack
        if reply is None:
            return  # reload and dismiss stay quiet on purpose
        print(f"  ciel: {reply}")
        self._record("ciel", reply)
        if req.lane == "voice":
            self._indicator.set_state("speaking")
            assert isinstance(sink, (_VoiceSink, _WireSink))
            await sink.play_reply(reply)
        elif req.lane == "discord":
            await sink.confirm_send(reply)

    def _active_agents(self) -> list[dict]:
        """The roster of everything working on the user's behalf right now,
        in the web protocol's agent shape (see remote/web.py).

        Three sources today: the deep-thought pass in flight (the brain
        stamps it), background watches (the watch tool's registrations),
        and timers. The Vigil watchers themselves are deliberately absent —
        they are standing infrastructure, on from startup to shutdown, and
        a roster that always says "calendar watcher" stops meaning
        anything. This lists *work with an end*, most urgent first: the
        pass someone is sitting in silence for, then watches, then timers.
        """
        agents: list[dict] = []
        since = self._brain.deep_thought_since
        if since is not None:
            agents.append({
                "id": "deep-thought",
                "kind": "deep",
                "label": "Deep thought",
                "detail": "reasoning through the current question",
                "since": since,
                "until": None,
            })
        if self._work_watcher is not None:
            for w in self._work_watcher.active():
                agents.append({
                    "id": w.id,
                    "kind": "watch",
                    # The label is the completion sentence ("The export has
                    # finished.") — still the best human name for the watch,
                    # so it rides as-is; the detail says what is literally
                    # being polled.
                    "label": w.label or f"Watching {w.target}",
                    "detail": ("file " if w.kind == "file" else "process ")
                    + w.target,
                    "since": w.created_at,
                    "until": w.expires_at,
                })
        if self._timers is not None:
            for t in self._timers.active():
                agents.append({
                    "id": t.id,
                    "kind": t.kind,  # "timer" | "alarm", the protocol's names
                    "label": t.label
                    or (
                        "Alarm"
                        if t.kind == "alarm"
                        else f"{spoken_duration(t.duration_s)} timer"
                    ),
                    # A pending timer's due_at still anchors to the tool
                    # call; the countdown re-syncs when commit_pending
                    # re-stamps it at end of speech.
                    "detail": "starts when the turn ends" if t.pending else None,
                    "since": None,
                    "until": t.due_at,
                })
        return agents

    def _set_muted(self, muted: bool, *, from_spoke: bool = False) -> None:
        """Move the mute switch — from the GUI, the sentinel poll, or (on
        the hub) the spoke's report. The hub touches no sentinel and
        stops no player: those are the spoke's, and the ``muted``
        broadcast below is how it learns a Chart flipped the switch.

        Muted, the machine holds its tongue and its name: no sound leaves
        the speakers (the Player's gate) and the wake word is not watched
        for (the frame loop's gate). The typed and web lanes keep working
        — that is what they are for — and Vigil's speak decisions become
        held notes. The sentinel file carries the state across the
        autoreloader's restarts and accepts toggles from outside the
        process; see its docstring in ``__init__``.
        """
        if muted == self._muted:
            return
        self._muted = muted
        if self._role != "hub":
            try:
                if muted:
                    self._mute_sentinel.touch()
                else:
                    self._mute_sentinel.unlink(missing_ok=True)
            except OSError:
                log.debug("could not persist mute state", exc_info=True)
        if muted and self._player is not None:
            # Whatever is mid-sentence stops now: the switch was flipped
            # because the room needs silence, not silence-after-this-line.
            self._player.stop()
        notice = (
            "muted — Ciel will stay silent and not listen for its name"
            if muted
            else "unmuted"
        )
        print(f"\n  [{notice}]", flush=True)
        # An event row: the transcript keeps why the assistant went quiet,
        # and the tap shows the flip on every open chart.
        self._record("event", notice)
        if self._web_link is not None:
            self._web_link.note_muted(muted)
        if self._world is not None:
            self._world.observe(W.MUTED, muted, source="spoke" if from_spoke else self._who)

    def _request_restart(self) -> None:
        """The GUI's restart button — a typed "reload" without the words.

        Queues the same clean re-exec, which the frame loop performs from
        idle: never yank the process out from under a conversation. The
        event row is the acknowledgement, on every open chart — the page
        that asked may be watching a conversation still in flight.
        """
        if self._reload_pending:
            # Still acknowledged: a typed "reload" sets the flag with no
            # row, and silence here would be indistinguishable from a
            # dropped frame to the page that just clicked.
            self._record("event", "restart already pending — Ciel restarts when idle")
            return
        self._reload_pending = True
        print("\n  [restart requested from the chart]", flush=True)
        self._record("event", "restart requested — Ciel restarts when idle")

    async def _read_stdin(self) -> None:
        """Feed typed lines into the turn queue — the tier-one chatbox.

        The terminal Ciel runs in doubles as the chat surface: every
        non-empty line becomes a turn, or an answer when a confirmation is
        pending. Consumption happens on the frame loop, not here, so the
        one-place-decides-state rule holds for the keyboard exactly as it
        does for the microphone.

        This is the *only* reader of stdin. In hotkey mode a bare Enter arms
        the wake through here rather than through a second reader in
        HotkeyWake — two readers of one descriptor race per line, and the
        asyncio side flips the descriptor non-blocking underneath a thread
        blocked in readline, silently breaking Enter-to-talk.

        Degrades to nothing, silently, when stdin isn't readable — launchd,
        a closed terminal, probes piping their own stdin. Typed input is a
        convenience like the indicator: it must never be able to take the
        voice loop down with it.
        """
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        try:
            await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
            )
        except (OSError, ValueError):
            log.debug("stdin not readable — typed input disabled", exc_info=True)
            return
        while True:
            raw = await reader.readline()
            if not raw:
                # EOF. The terminal went away or piped input ran dry; voice
                # carries on without a chatbox.
                log.debug("stdin closed — typed input disabled")
                return
            line = raw.decode(errors="replace").strip()
            if line:
                self._typed.append((time.monotonic(), line))
            elif self._wake is not None and hasattr(self._wake, "arm"):
                # A bare Enter in hotkey mode is the wake signal, delivered
                # through the one stdin reader so nothing competes for fd 0.
                self._wake.arm()

    async def _run_local_command(
        self, cmd: Command, text: str, player: "Player"
    ) -> None:
        """Speak a locally-handled turn — the pre-merge dismissal's path:
        same console and transcript shape as ``_run_turn``, just with
        nothing to wait for."""
        print(f"\n  you: {text}")
        self._record("user", text)
        if cmd.kind != "dismiss":
            # A dismissal alone is not a conversation: an accidental wake
            # ended with "never mind" should not arm reflection and rotation
            # over an exchange with nothing in it.
            self._conversed = True
        log.info("local command: %s", cmd.kind)
        reply = self._apply_local_command(cmd)
        if reply is None:
            return  # reload and dismiss stay quiet on purpose
        print(f"  ciel: {reply}")
        self._record("ciel", reply)
        self._indicator.set_state("speaking")
        # _spoke is set unconditionally, exactly as the voice sink does: an
        # interrupted reply must still open the follow-up window so the user's
        # barge-in ("...and a five minute one too") is heard without re-waking.
        # Setting it to the play's completion instead would drop that speech
        # and force a fresh wake — the one behavior this is meant to avoid.
        await player.play(self._tts.stream(reply))
        self._spoke = True

    def _apply_local_command(self, cmd: Command) -> str | None:
        """Execute a local command and return what to say (None: stay quiet).

        Side effects live here, shared by the spoken and typed paths;
        delivery — TTS or console — belongs to the callers. Timer commands
        assume the service exists because the dispatch sites guard on it.
        """
        if cmd.kind == "reload":
            self._reload_pending = True
            return None

        if cmd.kind == "dismiss":
            # Drop whatever this exchange was building — a held partial
            # thought included — and go quietly back to waiting. No spoken
            # reply: the answer to "never mind" is the absence of one, and
            # the indicator going idle is the acknowledgement.
            self._pending_text = None
            self._continue_listening = False
            print("  [dismissed]")
            return None

        assert self._timers is not None
        if cmd.kind == "set_timer":
            self._timers.set_relative(cmd.seconds)
            # Pending until the turn's finally commits it, so "starting now"
            # is anchored to the end of this very sentence.
            return f"{spoken_duration(cmd.seconds)} timer — starting now."

        if cmd.kind == "cancel_timer":
            active = self._timers.active()
            if not active:
                return "No timers are running."
            cancelled = self._timers.cancel("")
            if cancelled is None:
                return (
                    f"You have {len(active)} timers running — "
                    "tell me which one to cancel."
                )
            if cancelled.kind == "alarm":
                return f"Cancelled the {spoken_clock(cancelled.due_at)} alarm."
            return f"Cancelled the {spoken_duration(cancelled.duration_s)} timer."

        active = self._timers.active()  # list_timers
        if not active:
            return "No timers are running."
        now = time.time()
        parts = []
        for t in active:
            if t.kind == "alarm":
                parts.append(f"an alarm at {spoken_clock(t.due_at)}")
            else:
                left = spoken_duration(max(1.0, t.due_at - now), plural=True)
                parts.append(f"your {spoken_duration(t.duration_s)} timer with {left} left")
        listing = "; ".join(parts)
        return f"{listing[0].upper()}{listing[1:]}."

    # ── the proactive turn (Vigil) ───────────────────────────────────────────

    def _decide_proactive(self) -> tuple[Decision | None, ProactiveEvent | None]:
        """Peek the next live event and route it through the policy.

        The event is not yet claimed — the caller claims it with begin()
        exactly when it enacts the decision, and from then on owns it:
        every path out must mark, hold, or drop it, never silently discard.
        """
        assert (
            self._events is not None
            and self._policy is not None
            and self._presence is not None
        )
        now = time.time()
        event = self._events.peek_next(now)
        if event is None:
            return None, None
        local = time.localtime(now)
        today = time.strftime("%Y-%m-%d", local)
        decision = self._policy.decide(
            event,
            presence=self._presence.state(time.monotonic()),
            spoken_today=self._events.spoken_count(today),
            messaged_today=self._events.messaged_count(today),
            can_message=self._owner_messages is not None
            or self._remote_messaging(),
            now=now,
            local_minutes=local.tm_hour * 60 + local.tm_min,
        )
        log.info(
            "vigil: %s → %s (%s)", event.summary, decision.action, decision.reason
        )
        return decision, event

    async def _run_proactive_turn(self, event: ProactiveEvent) -> None:
        """One unattended turn over an event the policy approved for speech.

        The brain composes under the Witness rule (suppress + unattended:
        confirm-tier denies silently, everything world-facing denies with a
        reason) and the reply is *buffered*, not streamed — nobody is
        waiting, and the whole reply is what the one-way valve reads: SKIP
        drops the event, HOLD de-escalates it to a held note, anything else
        is rung and spoken. The budget is charged only after audio actually
        played. Failures hold the event rather than losing it — the next
        conversation still hears the note — and never escape: like the
        typed lane, an exception here must not reach turn.result().
        """
        assert self._events is not None
        print(f"\n  [proactive: {event.summary}]", flush=True)
        self._record("event", f"proactive: {event.summary}")
        self._indicator.set_state("thinking")
        reply = await self._compose_unattended(event, outlet="speak")
        if reply is None:
            return  # already routed (declined, held, timed out, hastened)
        try:
            self._indicator.set_state("speaking")
            print(f"  ciel (proactive): {reply}", flush=True)
            self._record("ciel", f"[proactive] {reply}")
            if self._role == "hub":
                # Rung and spoken by the spoke at its idle; its receipt
                # is the "reached the room" fact the budget hangs on.
                server = self._web_link
                assert isinstance(server, HubServer)
                completed = await server.deliver(
                    event.id, reply, {"ring": True, "source": event.source},
                    self._config.hub.speak_timeout_s,
                )
                device_lost = not completed
            else:
                player = self._player
                assert player is not None and self._tts is not None
                await self._play_ring(player)
                completed = await player.play(self._tts.stream(reply))
                device_lost = player.device_lost
            if not completed and device_lost:
                # Nothing reached the room — the notification did not
                # happen. No budget charge, and the note is still owed.
                self._record("event", "proactive: audio output lost")
                self._events.hold(event)
                return
            # Barge-in mid-nudge still counts as delivered — the ring and
            # first words reached the room, and the user chose to talk over
            # them; re-delivering would nag.
            self._events.mark_spoken(
                event, time.time(), time.strftime("%Y-%m-%d")
            )
            if not completed:
                self._record("event", "proactive: interrupted")
            if event.source == "brief":
                # The brief's material included the held notes (peeked at
                # compose time); a delivered brief is their delivery.
                self._events.take_held(time.time())
            # Rotation is re-armed at the next _enter_waiting — the session
            # grew, so the permanent-session problem stays dead — but
            # reflection isn't owed for a one-line nudge (_brain_conversed
            # stays False), and no follow-up window opens (_spoke stays
            # False), mirroring the timer lane.
            self._conversed = True
        except asyncio.CancelledError:
            # Shutdown mid-playback: the claim must not die with the task.
            self._events.hold(event)
            raise
        except Exception:  # noqa: BLE001 - an unattended turn must never crash the loop
            log.exception("proactive turn failed")
            self._record("event", "proactive turn failed")
            try:
                self._events.hold(event)
            except Exception:  # noqa: BLE001 - persistence is best-effort here
                log.debug("could not hold the failed event", exc_info=True)
        # No mic.drain() here: the BUSY-exit path in the frame loop drains
        # and re-enters WAITING for every turn task, this one included.

    async def _compose_unattended(
        self,
        event: ProactiveEvent,
        *,
        outlet: str,
        fallback: ProactiveEvent | None = None,
    ) -> str | None:
        """One unattended composition under the Witness rule — the single
        copy of the choreography every outlet shares.

        Returns the deliverable reply, or None once the event has already
        been routed: SKIP drops it; HOLD, an empty reply, a timeout, a
        hastening interrupt, or any composition failure holds it. Three
        invariants live here so they can never again drift apart between
        outlets (the review caught all three duplicated, two of them
        wrong):

        * The timeout interrupts the brain *inside* the suppression block —
          confirm suppression and unattended mode must not lift while the
          turn still generates, or a dying turn's tool call speaks a
          question into an empty room and its memory writes lose their
          provenance.
        * A hastening interrupt (a user lane taking the brain lock) ends
          the stream normally with a truncated sentence list — checked via
          _unattended_hastened, because a half-composed thought must never
          leave the machine as a sent text, a spoken fragment, or a held
          "finding".
        * Cancellation (reload drain, shutdown) holds the event before
          re-raising: the claim taken by begin() must not die with the
          task.

        ``fallback`` is what gets held on the non-delivery paths when the
        event's own summary is not fit to show a user (the verify
        instruction); it defaults to the event itself.
        """
        assert self._events is not None
        held = fallback if fallback is not None else event
        sentences: list[str] = []
        self._unattended_hastened = False
        try:
            extra = await self._proactive_extra(event)
            block = self._world_block().strip()
            if block:
                # The readings, before the event's own material: an
                # unattended turn has even less to go on than a spoken
                # one, and "is anyone around" is exactly its question.
                extra = block if extra is None else f"{block}\n\n{extra}"
            timed_out = False
            with self._confirm.suppress(), self._brain.unattended("proactive"):
                try:
                    await asyncio.wait_for(
                        self._collect_unattended(
                            proactive_prompt(
                                event.summary, outlet=outlet, extra=extra
                            ),
                            sentences,
                        ),
                        timeout=self._config.proactive.turn_timeout_s,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    log.debug("unattended %s turn timed out — interrupting", outlet)
                    try:
                        await self._brain.interrupt()
                    except Exception:  # noqa: BLE001 - best-effort, as in reflection
                        log.debug("interrupt after timeout failed", exc_info=True)
            if timed_out:
                self._events.hold(held)
                return None
            if self._unattended_hastened:
                log.info("unattended %s turn hastened aside — holding", outlet)
                self._events.hold(held)
                return None
        except asyncio.CancelledError:
            self._events.hold(held)
            raise
        except Exception:  # noqa: BLE001 - composition failure must not crash the caller
            log.exception("unattended %s composition failed", outlet)
            try:
                self._events.hold(held)
            except Exception:  # noqa: BLE001 - persistence is best-effort here
                log.debug("could not hold the failed event", exc_info=True)
            return None

        reply = " ".join(sentences).strip()
        valve = proactive_valve(reply) if reply else None
        if valve == "SKIP":
            print("  [proactive: model declined]", flush=True)
            self._record("event", f"proactive ({outlet}): declined")
            self._events.drop(event, time.time())
            return None
        if valve == "HOLD" or not reply:
            # An empty reply is a malfunction, not a judgement; holding
            # keeps the note owed to the next conversation either way.
            print("  [proactive: held for next conversation]", flush=True)
            self._record("event", f"proactive ({outlet}): held")
            self._events.hold(held)
            return None
        return reply

    async def _proactive_extra(self, event: ProactiveEvent) -> str | None:
        """Event-specific prompt material. The brief gets today's agenda and
        the overnight held notes — *peeked*, never consumed here: the notes
        are only taken once the brief actually delivers (spoken or sent).
        A SKIP, a timeout, or a dead speaker must leave them held, or the
        brief's failure silently burns everything it was carrying.
        """
        if event.source != "brief" or self._events is None:
            return None
        parts: list[str] = []
        if self._calendar is not None:
            agenda = await self._calendar.agenda_today()
            if agenda:
                parts.append(
                    "Today's calendar:\n"
                    + "\n".join(f"- {line}" for line in agenda)
                )
        notes = self._events.peek_held(time.time())
        if notes:
            parts.append(
                "Held while you were away:\n"
                + "\n".join(f"- {note.summary}" for note in notes)
            )
        if not parts:
            parts.append(
                "The calendar shows nothing for the rest of today and "
                "nothing was held overnight."
            )
        return "\n\n".join(parts)

    def _remote_messaging(self) -> bool:
        """Whether the Discord link can carry an away text *right now* —
        opted in via ``discord.proactive`` and actually connected, so a
        dropped gateway reads as no outlet rather than a text into the
        void. Consulted at decision time on purpose: armed-in-config is a
        startup claim, deliverable-now is a per-event fact."""
        return (
            self._remote_link is not None
            and self._config.discord.proactive
            and self._remote_link.can_send
        )

    async def _run_proactive_message(self, event: ProactiveEvent) -> None:
        """One unattended turn whose outlet is a text to the owner.

        Runs in the maintenance slot: no audio, failures swallowed, hastened
        by user turns. The Witness rule still governs the turn itself —
        ``mcp__ciel__send_message`` stays denied inside it — because the send
        below is pipeline-owned: the model composes words; deterministic
        code decided a message happens, and config decided to whom. The
        recipient is pinned either way — the iMessage handle or the Discord
        owner id — with iMessage keeping priority when both are armed. The
        texting budget is charged only after the send succeeds.
        """
        assert self._events is not None and (
            self._owner_messages is not None or self._remote_link is not None
        )
        print(f"\n  [proactive → text: {event.summary}]", flush=True)
        self._record("event", f"proactive (text): {event.summary}")
        reply = await self._compose_unattended(event, outlet="message")
        if reply is None:
            return  # already routed (declined, held, timed out, hastened)
        try:
            imessage_reachable = self._owner_messages is not None and (
                self._role != "hub"
                or (isinstance(self._web_link, HubServer) and self._web_link.spoke_connected)
            )
            if imessage_reachable:
                assert self._owner_messages is not None
                await self._owner_messages.send(
                    self._config.proactive.owner_handle, reply
                )
            elif self._remote_link is not None and self._remote_link.can_send:
                # The Mac is asleep (no spoke), so the phone's iMessage
                # can't be reached through it: Discord carries the text.
                await self._remote_link.send(reply)
            elif self._owner_messages is not None:
                await self._owner_messages.send(
                    self._config.proactive.owner_handle, reply
                )
            else:  # unreachable behind the assert; kept for the type story
                raise MessagesUnavailable("no away outlet armed")
            print(f"  ciel (texted): {reply}", flush=True)
            self._record("ciel", f"[proactive text] {reply}")
            self._events.mark_messaged(
                event, time.time(), time.strftime("%Y-%m-%d")
            )
            if event.source == "brief":
                self._events.take_held(time.time())
        except asyncio.CancelledError:
            # Reload drain or shutdown mid-send: the claim must not die
            # with the task. The send may or may not have gone out — held
            # means at-least-once, which beats silently never.
            self._events.hold(event)
            raise
        except (MessagesUnavailable, RemoteUnavailable) as exc:
            # The send path refused (switch off, handle malformed, osascript
            # failure, a Discord link that dropped between decision and
            # send). The event is still owed: held for the next
            # conversation, and the reason is in the log, not a guess.
            log.warning("away text failed (%s) — holding the event", exc)
            self._record("event", "proactive (text): send failed")
            try:
                self._events.hold(event)
            except Exception:  # noqa: BLE001 - persistence is best-effort here
                log.debug("could not hold the failed event", exc_info=True)
        except Exception:  # noqa: BLE001 - maintenance-slot task must not crash the loop
            log.exception("proactive message turn failed")
            try:
                self._events.hold(event)
            except Exception:  # noqa: BLE001 - persistence is best-effort here
                log.debug("could not hold the failed event", exc_info=True)

    def _emit_verify(self, tool: str, args: dict) -> None:
        """File a read-back check after a confirmed outward action.

        Called by the recorder's PostToolUse for confirm-gated tools — which
        under the Witness rule only ever run attended. The check itself runs
        as a silent unattended turn (the policy routes source="verify" to
        "note"), and its finding rides a held note: "done" becomes something
        observed rather than something a tool's return code implied. Expiry
        is short because the check's value decays with distance from the
        action.
        """
        if self._events is None:
            return
        if self._events.count_source("verify") >= 3:
            # A backlog cap, not a budget: checks are cheap but each one is
            # a brain turn and a future held note, and a burst of approved
            # actions must not turn the next conversation's opening into a
            # verification report. The journal still holds every record.
            log.info("read-back backlog full — skipping the check for %s", tool)
            return
        from ciel.brain.toolguard import describe_call

        described = describe_call(tool, args).removesuffix(" — okay?")
        now = time.time()
        self._events.push(ProactiveEvent(
            id=self._events.next_id(),
            source="verify",
            importance=1,
            created_at=now,
            expires_at=now + 1800,
            summary=(
                f"Read-back check. The user just approved this action: "
                f"{described}. Using read-only tools (recent_actions holds "
                "the journal record), verify whether it actually took "
                "effect, and report what you observed — not what the tool "
                "claimed."
            ),
            dedupe_key=f"verify:{tool}:{int(now * 1000)}",
            # The human-shaped description, for the note turn's fallback —
            # the summary above is a directive and must never be read to
            # the user as a held note.
            payload={"action": described},
        ))
        log.info("read-back check filed for %s", tool)

    async def _run_proactive_note(self, event: ProactiveEvent) -> None:
        """One silent unattended turn whose finding becomes a held note.

        The verification half of the loop: observe with read-only tools,
        write down what is actually true, tell the user at the start of
        their next conversation. SKIP means "checked, nothing worth
        relaying" and drops the event; a failure degrades to holding the
        original instruction, so the next attended conversation can do the
        check itself with full tools.
        """
        assert self._events is not None
        print(f"\n  [proactive check: {event.summary[:80]}…]", flush=True)
        self._record("event", "proactive: read-back check")
        # What rides into the next conversation on failure: never the raw
        # instruction — an event summary is promised to be a spoken-English
        # line, and _with_held_notes would read the directive to the user
        # verbatim. The fallback says honestly that the check didn't happen.
        fallback = dc_replace(
            event,
            summary=(
                "I meant to double-check something you approved earlier — "
                f"{event.payload.get('action', 'a recent action')} — but "
                "could not; worth confirming it went through."
            ),
            expires_at=None,
        )
        reply = await self._compose_unattended(
            event, outlet="note", fallback=fallback
        )
        if reply is None:
            return  # already routed (declined, held with the fallback, ...)
        try:
            # The finding replaces the instruction: what rides into the next
            # conversation is what was observed, stamped now.
            self._events.hold(dc_replace(
                event,
                summary=reply,
                created_at=time.time(),
                expires_at=None,
            ))
            print(f"  [checked: {reply}]", flush=True)
            self._record("event", f"proactive check: {reply}")
        except Exception:  # noqa: BLE001 - maintenance-slot task must not crash the loop
            log.exception("could not hold the finding")

    async def _collect_unattended(
        self, prompt: str, sentences: list[str]
    ) -> None:
        """Drain one brain turn into a buffer instead of the speakers.

        Mutates ``sentences`` rather than returning, so a timeout that
        cancels this mid-stream still leaves the caller holding whatever
        arrived. Same aclosing obligation as every other consumer.
        """
        async with aclosing(self._brain.ask(prompt)) as stream:
            async for kind, sentence in stream:
                if kind == "escalation":
                    # The Witness guard denies the spawn; nothing to await.
                    print("  [proactive: escalation denied — unattended]", flush=True)
                    continue
                if kind == "thinking":
                    print(f"  ciel (thinking): {sentence}", flush=True)
                    self._record("ciel-thinking", sentence)
                    continue
                sentences.append(sentence)

    def _with_held_notes(self, text: str) -> str:
        """Prefix a user turn with Vigil's held notes, consuming them.

        The transcript records the user's raw words plus an ``event`` row —
        the prefix goes only to the model, so the record never lies about
        what was said. No budget charge: the user initiated this turn.
        """
        if self._events is None:
            return text
        notes = self._events.take_held(time.time())
        if not notes:
            return text
        print(f"  [delivering {len(notes)} held note(s)]", flush=True)
        self._record("event", f"held notes delivered ({len(notes)})")
        listing = " ".join(note.summary for note in notes)
        return (
            "(System note — things that came up while you were away: "
            f"{listing} Work the relevant ones into your reply naturally; "
            "if one is stale, drop it silently.)\n\n"
            f"{text}"
        )

    async def _abandon_playback(self, device_lost: bool) -> None:
        """Abandon the rest of a turn whose sentence didn't finish playing.

        Two causes share the mechanics — stop generating, resolve any pending
        confirmation to deny, mark the turn interrupted — but not the record:
        barge-in means the user wants something else; a dead output device
        means nothing was heard. Logging the second as "(interrupted)" sends
        whoever reads the transcript to barge-in tuning when the real problem
        is a cable (the hardening review's issue B — two call sites had
        exactly that misreport).
        """
        self._interrupted = True
        if device_lost:
            self._confirm.cancel("output device lost")
            await self._brain.interrupt()
            print("  (audio output lost — abandoning the turn)")
            self._record("event", "audio output lost")
        else:
            self._confirm.cancel("interrupted")
            await self._brain.interrupt()
            print("  (interrupted)")
            self._record("event", "interrupted")

    async def _announce_timers(
        self, fired: list["Timer"], missed: bool = False
    ) -> None:
        """Ring, then speak each fired timer's announcement.

        The unprompted-speech path: no turn, no brain, just the ring and the
        sentence the timer was armed with. The ring comes first because an
        assistant that starts talking out of nowhere startles — a familiar
        tone buys the half-second of "that's the timer" before the words.
        On the hub the spoke does the ringing and the speaking, one
        delivery per timer; a timer with no spoke to ring is one event
        row, not a lost loop.
        """
        self._indicator.set_state("speaking")
        try:
            if self._role == "hub":
                server = self._web_link
                assert isinstance(server, HubServer)
                for timer in fired:
                    text = announcement(timer)
                    if missed:
                        text = f"While I was off, a {timer.kind} went off. {text}"
                    print(f"\n  ciel ({timer.kind}): {text}", flush=True)
                    self._record("ciel", f"[{timer.kind}] {text}")
                    ok = await server.deliver(
                        f"timer:{timer.id}", text,
                        {"ring": True, "kind": timer.kind},
                        self._config.hub.speak_timeout_s,
                    )
                    if not ok:
                        self._record(
                            "event", f"{timer.kind} announcement did not reach the room"
                        )
                return
            player, mic = self._player, self._mic
            assert player is not None and mic is not None and self._tts is not None
            await self._play_ring(player)
            for timer in fired:
                text = announcement(timer)
                if missed:
                    text = f"While I was off, a {timer.kind} went off. {text}"
                print(f"\n  ciel ({timer.kind}): {text}", flush=True)
                self._record("ciel", f"[{timer.kind}] {text}")
                await player.play(self._tts.stream(text))
        except Exception:  # noqa: BLE001 — a failed ring must not kill the loop
            log.exception("timer announcement failed")
        finally:
            # Ciel's own voice was captured while the speakers were live.
            if self._mic is not None:
                self._mic.drain()
            self._indicator.set_state(
                "listening" if self._state is State.LISTENING else "idle"
            )

    async def _play_ring(self, player: "Player") -> None:
        """The timer ring: a rising two-note figure, played twice.

        Deliberately not the thinking chime — that tone means "answer coming",
        this one means "Ciel is about to speak although you said nothing".
        Distinct sounds keep those distinguishable by ear alone.
        """
        from ciel.audio.input import float_to_pcm

        rate = self._tts.sample_rate

        def note(freq: float, dur: float) -> np.ndarray:
            t = np.arange(int(rate * dur), dtype=np.float32) / rate
            fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.015)
            return 0.2 * np.sin(2 * np.pi * freq * t) * fade

        gap = np.zeros(int(rate * 0.08), dtype=np.float32)
        figure = np.concatenate([note(660.0, 0.13), note(880.0, 0.18)])
        data = float_to_pcm(
            np.concatenate([figure, gap, figure]).astype(np.float32)
        )

        async def chunks():
            yield data

        try:
            await player.play(chunks())
        except Exception:  # noqa: BLE001 - a missing ring is not a failure
            log.debug("could not play the timer ring", exc_info=True)

    async def _play_chime(self, player: "Player") -> None:
        """A soft tone marking the end of spoken reasoning.

        Same voice speaks both reasoning and answer, so the ear needs some
        other boundary. 120 ms of A5 with 20 ms fades — present, not alarming.
        """
        from ciel.audio.input import float_to_pcm

        rate = self._tts.sample_rate
        t = np.arange(int(rate * 0.12), dtype=np.float32) / rate
        fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.02)
        tone = (0.12 * np.sin(2 * np.pi * 880.0 * t) * fade).astype(np.float32)
        data = float_to_pcm(tone)

        async def chunks():
            yield data

        try:
            await player.play(chunks())
        except Exception:  # noqa: BLE001 - a missing chime is not a failure
            log.debug("could not play the thinking chime", exc_info=True)

    # ── the world table (Phase Space) ────────────────────────────────────────

    @property
    def _who(self) -> str:
        """This process, as a fact's source: the hub, or the Mac itself."""
        return "hub" if self._role == "hub" else "mac"

    def _world_tick(self) -> None:
        """The once-a-second duty: re-observe what this process owns
        (timers, watches), mirror the file, and hand the Chart the
        snapshot when the version moved. Polled, like the agent roster,
        because the producers on threads must not touch the sockets."""
        world = self._world
        if world is None:
            return
        who = self._who
        if self._timers is not None:
            world.observe(W.TIMERS, [
                {"kind": t.kind, "label": t.label, "due_at": t.due_at,
                 "duration_s": t.duration_s, "pending": t.pending}
                for t in self._timers.active()
            ], source=who)
        if self._work_watcher is not None:
            world.observe(W.WATCHES, [
                {"label": w.label, "kind": w.kind, "target": w.target,
                 "expires_at": w.expires_at}
                for w in self._work_watcher.active()
            ], source=who)
        world.flush()
        version = world.version
        if version != self._world_seen:
            self._world_seen = version
            if self._web_link is not None:
                self._web_link.note_world(world.snapshot())

    def _observe_presence(self, *, source: str, ttl_s: float | None = None) -> None:
        """Fold the presence view into the table. On the hub this runs
        on every heartbeat; locally, once per turn (the Quartz reads are
        cheap, but the probe's rule is never at frame rate)."""
        if self._world is None or self._presence is None:
            return
        try:
            state = self._presence.state(time.monotonic())
        except Exception:  # noqa: BLE001 - a failed read leaves the last one standing
            log.debug("presence read for the world failed", exc_info=True)
            return
        idle = state.seconds_since_input
        self._world.observe(W.PRESENCE, {
            "present": state.present,
            "locked": state.screen_locked,
            "idle_s": round(idle, 1) if math.isfinite(idle) else None,
            "since_conversation_s": (
                round(state.seconds_since_conversation, 1)
                if state.seconds_since_conversation is not None else None
            ),
        }, source=source, ttl_s=ttl_s)

    def _world_block(self) -> str:
        """The turn's opening block, or "" when the table is off or kept
        out of the prompt. Locally the presence reading is taken here,
        so it is never older than the turn it opens."""
        if self._world is None or not self._config.world.in_prompt:
            return ""
        if self._role != "hub":
            self._observe_presence(source="mac")
        return self._world.render() + "\n\n"

    async def _refresh_agenda(self) -> None:
        """Today's remaining calendar into the table, every
        ``agenda_refresh_s`` and when kicked (a spoke connect, a wake
        from sleep). On the hub with the Mac's calendar the read is an
        RPC, skipped while the seat is empty rather than warned about
        every quarter hour."""
        assert self._world is not None and self._calendar is not None
        from ciel.proactive.watchers import poll_loop

        async def tick() -> None:
            if (
                self._remote is not None
                and self._calendar is self._remote.calendar
                and not self._web_link.spoke_connected  # type: ignore[union-attr]
            ):
                return
            lines = await self._calendar.agenda_today()  # type: ignore[union-attr]
            self._world.observe(  # type: ignore[union-attr]
                W.AGENDA,
                {"day": time.strftime("%Y-%m-%d"), "lines": [str(x) for x in lines]},
                source="calendar",
            )

        await poll_loop(self._agenda_kick, self._config.world.agenda_refresh_s, tick)

    def _record(self, speaker: str, text: str) -> None:
        """Append one row to this conversation's transcript, if one is kept.

        Also Vigil's presence tap: every ``user`` row (a spoken turn, a
        typed line), every ``you-confirm`` row (the broker's speaker for
        confirmation answers), and every ``user-web`` row (the GUI binds
        loopback, so its user is at this machine — unlike ``user-remote``,
        which is evidence of the opposite) is evidence the user is here.
        Tapped here, not in _enter_waiting, so Ciel's own proactive speech
        (which sets _conversed) can never count as the user being around.

        And the Chart's feed: every row, whatever its lane, is teed to any
        open GUI — which is how web replies reach their reader at all.
        """
        if (
            speaker in ("user", "you-confirm", "user-web")
            and self._presence is not None
        ):
            self._presence.note_conversation(time.monotonic())
        if self._web_link is not None:
            self._web_link.note_row(speaker, text)
        if self._transcript is not None:
            self._transcript.record(speaker, text)

    @staticmethod
    def _rms(frame: bytes) -> float:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(samples * samples)))

    def _track_noise_floor(self, frame: bytes) -> None:
        """Maintain a running estimate of ambient loudness.

        Updated only while Ciel is idle and not speaking, so the floor reflects
        the room rather than Ciel's own voice. Asymmetric on purpose: it rises
        slowly and falls quickly, so a passing noise doesn't desensitize
        barge-in for the next minute.
        """
        level = self._rms(frame)
        if self._noise_floor is None:
            self._noise_floor = level
            return
        alpha = 0.02 if level > self._noise_floor else 0.15
        self._noise_floor = (1 - alpha) * self._noise_floor + alpha * level

    def _check_barge_in(self, frame: bytes, player: "Player") -> None:
        """Stop playback if the user talks over Ciel.

        Runs on the main frame loop, so it stays cheap — RMS against an
        adaptive threshold, sustained across several frames. No VAD model:
        with no echo cancellation, Ciel's own voice through the speakers *is*
        speech and a VAD says so confidently. Relative loudness is the only
        signal that still separates the two, and only partially — see the
        config note on why this is off by default on speakers.
        """
        if not self._config.audio.barge_in or not player.is_playing:
            self._barge_run = 0
            return

        floor = self._noise_floor if self._noise_floor is not None else 0.0
        threshold = max(
            floor * self._config.audio.barge_in_ratio,
            self._config.audio.barge_in_floor,
        )

        if self._rms(frame) < threshold:
            self._barge_run = 0
            return

        self._barge_run += 1
        if self._barge_run >= self._config.audio.barge_in_frames:
            log.debug("barge-in (floor %.3f, threshold %.3f)", floor, threshold)
            self._barge_run = 0
            player.stop()

    async def _acknowledge(self, player: "Player", mic: "MicStream") -> None:
        """Speak a short acknowledgement that the wake was heard.

        The audible counterpart of the indicator turning green. Kept to a
        syllable or two on purpose: the microphone is drained afterwards so
        Ciel's own voice isn't transcribed as the user's utterance, which means
        anything said *during* the acknowledgement is lost — a short one keeps
        that window tiny for someone who wakes and speaks in one breath.
        """
        wake = self._config.wake
        if not wake.ack or wake.mode == "always" or not wake.ack_phrases:
            return
        try:
            await player.play(self._tts.stream(random.choice(wake.ack_phrases)))
        except Exception:  # noqa: BLE001 - a lost ack must not cost the turn
            log.debug("could not speak the wake acknowledgement", exc_info=True)
        mic.drain()

    # ── state transitions ────────────────────────────────────────────────────

    def _enter_listening(self) -> None:
        self._state = State.LISTENING
        self._endpointer.reset()
        # Even a wake-word start gets a deadline: a false wake with no speech
        # behind it must fall back to WAITING rather than latch here forever.
        timeout_ms = self._config.audio.wake_timeout_ms
        self._followup_until = (
            time.monotonic() + timeout_ms / 1000 if timeout_ms > 0 else None
        )
        self._indicator.set_state("listening")
        print("\n  [listening]", flush=True)

    def _enter_continuation(self) -> None:
        """Resume listening mid-turn, holding the partial utterance.

        Like a follow-up window but with the filler deadline, and the held
        text survives: whatever comes next is appended to it.
        """
        self._state = State.LISTENING
        self._endpointer.reset()
        self._followup_until = (
            time.monotonic() + self._config.audio.filler_extend_ms / 1000
        )
        self._indicator.set_state("listening")

    def _enter_followup(self) -> None:
        """Listen for a reply without requiring the wake word again."""
        self._state = State.LISTENING
        self._endpointer.reset()
        self._followup_until = (
            time.monotonic() + self._config.audio.followup_ms / 1000
        )
        self._indicator.set_state("listening")
        print("\n  [listening]", flush=True)

    def _followup_expired(self) -> bool:
        """Whether an unused follow-up window has run out.

        Once the endpointer has speech in hand the window stops mattering: the
        user started in time, and a long sentence must not be cut off at the
        deadline.
        """
        if self._followup_until is None or self._endpointer.speaking:
            return False
        return time.monotonic() >= self._followup_until

    def _enter_waiting(self) -> None:
        self._state = State.WAITING
        if self._endpointer is not None:
            self._endpointer.reset()
        if self._wake is not None:
            self._wake.reset()
        self._followup_until = None
        self._pending_text = None  # a held thought never outlives listening
        self._indicator.set_state("idle")
        if self._conversed:
            # A real conversation just ended: arm Closure and the rotation
            # clock. _conversed (not _spoke, which resets per turn and can be
            # False after a trailing noise blip) is consumed here so a later
            # false wake — which also funnels through this method — can't
            # re-arm and reflect the same conversation twice. Reflection is
            # armed only when the brain participated: a timer set by the
            # command grammar costs zero model turns end to end, and a
            # reflection over it would quietly break that.
            self._schedule.conversation_ended(
                time.monotonic(), brain=self._brain_conversed
            )
            self._conversed = False
            self._brain_conversed = False

    def _on_wake_from_sleep(self, gap_s: float) -> None:
        """Reconcile the loop with a wall clock that jumped (the machine slept).

        Three deadlines drifted while the monotonic clock was paused:

        * Watchers were mid-``asyncio.sleep`` and would finish their poll
          interval before rescanning — minutes of lag at exactly the moment a
          "your nine o'clock is in eight minutes" matters. Kicked awake.
        * Timers that came due mid-nap would announce however stale they are;
          the next poll runs the startup missed-sweep instead (grace-filtered,
          "While I was off…" flavored).
        * If the nap outlived the resume window, the armed reflect/rotate
          deadlines point at a session that can no longer be resumed —
          serving them would reflect a fresh session over nothing. Cleared,
          mirroring what rehydrate_schedule decides at startup.
        """
        log.info("wall clock jumped %.0f s — woke from sleep; catching up", gap_s)
        self._record("event", f"woke from a {gap_s / 60:.0f} minute sleep")
        if self._timers is not None:
            self._timer_missed_sweep = True
        for watcher in self._vigil_watchers:
            kick = getattr(watcher, "kick", None)
            if kick is not None:
                kick()
        if self._agenda_task is not None:
            self._agenda_kick.set()
        if gap_s > self._config.brain.resume_window_minutes * 60:
            self._schedule.clear()

    async def _run_reflection(self) -> None:
        """One silent Closure turn; failures are logged shrugs, never crashes."""
        print("\n  [reflecting]", flush=True)
        self._record("event", "reflection")
        try:
            # Suppressed confirmations: nobody is listening, so a stray
            # confirm-tier tool call inside the reflection denies silently
            # instead of voicing a question into an empty room. Unattended
            # mode alongside it: reflection is bound by the Witness rule,
            # so its "no messages, no searches" is enforced by the guard,
            # not merely requested by the prompt.
            with self._confirm.suppress(), self._brain.unattended("reflection"):
                await asyncio.wait_for(
                    self._brain.reflect(REFLECTION_PROMPT),
                    timeout=self._config.reflection.timeout_s,
                )
        except asyncio.TimeoutError:
            # wait_for cancelled reflect(), but the SDK is still generating into
            # a turn nobody will read. Interrupt so it stops promptly and the
            # next user turn's drain is short — otherwise it runs to completion
            # (still billing, still holding the turn's tail) with confirm
            # suppression already lifted, and its leftover stream corrupts the
            # next turn until drained. Mirrors the wake/typed hastening paths.
            log.debug("reflection timed out — interrupting the brain")
            try:
                await self._brain.interrupt()
            except Exception:  # noqa: BLE001 - best-effort; the drain still recovers
                log.debug("interrupt after reflection timeout failed", exc_info=True)
        except Exception:  # noqa: BLE001 - reflection is best-effort
            log.debug("reflection failed", exc_info=True)

    async def _run_rotation(self) -> None:
        """Retire the idle session; the next conversation starts clean."""
        try:
            await self._brain.rotate()
            # A rotated session is a new conversation, so its transcript is a
            # new file — the one-file-per-conversation contract. Rotated after
            # the session so the "session rotated" marker below lands as the
            # last row of the conversation it closes, not the first of the next.
            self._record("event", "session rotated")
            if self._transcript is not None:
                self._transcript.rotate()
            print("\n  [fresh session]", flush=True)
        except Exception:  # noqa: BLE001 - ask() reconnects on demand anyway
            log.exception("rotation failed")

    def _announce_ready(self) -> None:
        if self._role == "hub":
            assert self._web_link is not None
            print(f"\nHub ready. Wire: {self._web_link.url}/ws — run `ciel spoke` for the room.")
            return
        mode = self._config.wake.mode
        if mode == "wakeword":
            # The model may be a pretrained NAME ("hey_jarvis") or a PATH to
            # a custom model ("~/.ciel/models/hey_ciel.onnx") — the phrase is
            # the stem either way, not the directory it lives in.
            phrase = Path(self._config.wake.model).stem.replace("_", " ")
            print(f'\nReady. Say "{phrase}".')
        elif mode == "always":
            print("\nReady. Just talk.")
        # hotkey prints its own prompt via reset()/start()
        if self._web_link is not None and self._web_link.serving:
            print(f"GUI: {self._web_link.url}")
        if self._muted:
            # A silenced greeting must not read as a broken speaker.
            print("  [muted — Ciel will stay silent and not listen for its name]")

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def _startup(self) -> None:
        started = time.monotonic()
        # Whisper, the wake model, and the Agent SDK subprocess all take
        # seconds to start; do them together so startup is bounded by the
        # slowest one, not their sum.
        startup = [
            self._indicator.start(),
            self._brain.connect(self._mcp_servers, self._custom_tools),
        ]
        if self._role != "hub":
            assert self._wake is not None
            startup += [
                self._warm_up_stt(),
                self._warm_up_tts(),
                self._wake.start(),
            ]
        if self._speaker is not None:
            startup.append(self._speaker.warm_up())
        if self._watcher is not None:
            startup.append(self._watcher.start())
        if self._remote_link is not None:
            # Spawns the gateway task and returns; a bad token or missing
            # dependency is one warning, never a blocked greeting.
            startup.append(self._remote_link.start())
        if self._web_link is not None:
            # Binds loopback and returns; a taken port or missing
            # dependency is one warning, never a blocked greeting.
            startup.append(self._web_link.start())
        # Vigil watchers start here but never block here: start() only
        # spawns their poll tasks — a permission dialog stalls a watcher,
        # not the greeting.
        startup.extend(w.start() for w in self._vigil_watchers)
        await asyncio.gather(*startup)
        if (
            self._world is not None
            and self._calendar is not None
            and self._config.world.agenda_refresh_s > 0
        ):
            self._agenda_task = asyncio.create_task(self._refresh_agenda())
        log.info("ready in %.1fs", time.monotonic() - started)

    async def _warm_up_stt(self) -> None:
        """Load the transcriber, falling back to faster-whisper if it won't.

        ``build_stt`` already falls back when mlx-whisper is *absent*; this
        covers present-but-broken — an ImportError or model failure that only
        surfaces in warm_up's lazy import (the hardening follow-up's note).
        Same reasoning as the TTS fallback below: losing GPU transcription
        speed is acceptable, losing the ears is not, and faster-whisper is a
        hard dependency that works everywhere. Runs before the confirm broker
        binds its engines, so the replacement is what gets bound.
        """
        from ciel.stt.local_whisper import WhisperSTT

        assert self._stt is not None
        try:
            await self._stt.warm_up()
            return
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            if isinstance(self._stt, WhisperSTT):
                raise  # already the engine of last resort; nothing to fall to
            log.warning("%s failed to start (%s) — falling back to faster-whisper",
                        type(self._stt).__name__, exc)

        self._stt = WhisperSTT(self._config.stt)
        await self._stt.warm_up()

    async def _warm_up_tts(self) -> None:
        """Load the speech engine, falling back to ``say`` if it won't start.

        A voice download can fail on a bad network. Losing voice *quality*
        because of that is acceptable; losing the ability to speak at all is
        not, and `say` is always present on macOS.
        """
        assert self._tts is not None
        try:
            await self._tts.warm_up()
            return
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            log.warning("%s failed to start (%s) — falling back to `say`",
                        type(self._tts).__name__, exc)

        from ciel.tts.macos_say import SayTTS

        self._tts = _apply_effect(SayTTS(self._config.tts), self._config)
        await self._tts.warm_up()

    async def _shutdown(self) -> None:
        closers = [
            self._brain.close(),
            self._indicator.close(),
        ]
        for component in (self._stt, self._tts, self._wake):
            if component is not None:
                closers.append(component.close())
        if self._speaker is not None:
            closers.append(self._speaker.close())
        if self._watcher is not None:
            closers.append(self._watcher.close())
        if self._remote_link is not None:
            closers.append(self._remote_link.close())
        if self._web_link is not None:
            closers.append(self._web_link.close())
        closers.extend(w.close() for w in self._vigil_watchers)
        if self._agenda_task is not None and not self._agenda_task.done():
            self._agenda_task.cancel()
            closers.append(asyncio.gather(self._agenda_task, return_exceptions=True))
        await asyncio.gather(*closers, return_exceptions=True)
        if self._world is not None:
            self._world.flush()
        if self._transcript is not None:
            self._transcript.close()
        if self._brain.total_cost_usd:
            print(f"\nsession cost: ${self._brain.total_cost_usd:.4f}")


__all__ = ["Pipeline", "State", "build_tts"]
