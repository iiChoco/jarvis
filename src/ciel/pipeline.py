"""The orchestrator: wake, listen, transcribe, think, speak.

There is one microphone and one reader of it — this loop. Every component that
needs audio is *fed* frames rather than opening its own stream, which is what
keeps the wake detector and the endpointer from fighting over the device.

The loop is a small state machine. The states matter because the same incoming
frame means completely different things depending on where we are: before the
wake word it's noise to be scored, during an utterance it's speech to be
buffered, and while Ciel is talking it's a possible interruption.

Four of this file's behaviors carry codenames: **Cauchy** (the mid-thought
hold — a transcript that doesn't read as finished means the tail hasn't
settled yet, so don't declare the sequence converged), **Discontinuity**
(barge-in — a jump that ends the current segment), **Neighborhood** (the
follow-up window — an open ball around the last turn, inside which no wake
word is required), and **Isomorphism** (the typed lane — a line on stdin is
a structure-preserving image of a spoken turn: same brain, same session,
same transcript, no audio on either side).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from enum import Enum, auto
from pathlib import Path

import numpy as np

from ciel.audio.input import MicStream, float_to_pcm
from ciel.audio.output import Player
from ciel.audio.speaker import build_speaker_gate
from ciel.audio.vad import Endpointer
from ciel.audio.wake import HotkeyWake, WakeDetector, build_wake_detector
from ciel.brain.agent import Brain
from ciel.brain.prompt import REFLECTION_PROMPT, proactive_prompt
from ciel.brain.tools import build_tool_server
from ciel.brain.tools.memory import bind_context as bind_memory_context
from ciel.config import SAMPLE_RATE, AudioConfig, Config
from ciel.confirm import VoiceConfirmBroker
from ciel.proactive.calendar import CalendarWatcher
from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.policy import Decision, InterruptionPolicy
from ciel.proactive.presence import PresenceProbe
from ciel.reload import SourceWatcher, default_roots
from ciel.commands import Command, match as match_command
from ciel.stt import SpeechToText, build_stt
from ciel.timers import Timer, announcement, spoken_clock, spoken_duration
from ciel.transcript import Transcript
from ciel.tts.base import TextToSpeech
from ciel.ui.indicator import Indicator, build_indicator

log = logging.getLogger(__name__)

_SLEEP_GAP_S = 30.0
"""A wall-clock jump between frames larger than this means the machine slept
(or the process was suspended — same consequences). Frames arrive ~33/s while
awake, so even a wedged maintenance pass never opens a gap like this. Chosen
well above any legitimate scheduling hiccup and well below the shortest nap a
lid-close produces. On macOS the monotonic clock largely pauses with the
process, so every monotonic deadline in the loop silently stretched by the
nap — the wake-up handler is where reality is reconciled."""


class State(Enum):
    WAITING = auto()    # not addressed; frames go to the wake detector
    LISTENING = auto()  # addressed; frames go to the endpointer
    BUSY = auto()       # thinking or speaking; frames watched for barge-in


def trails_off(heard: str, audio: AudioConfig) -> bool:
    """Whether a transcript ends mid-thought (the Cauchy hold's judgement).

    The endpoint firing only proves the user *paused*, not that they
    finished; the transcript is the tiebreaker, and it is read
    pessimistically:

    * a trailing ellipsis is the transcriber itself saying "trailed off".
    * ``?``/``!`` are always complete — Whisper doesn't punctuate fragments
      as questions.
    * a period is trusted unless the last word is one that can't end a
      thought (``dangling_words``): Whisper appends periods to fragments
      out of habit, and "check my." is not a closed sentence.
    * no closing punctuation at all means the transcriber didn't hear a
      finished sentence either. This branch is what catches the pause after
      an "um" — Whisper strips fillers from transcripts, so a hold that
      waits to *see* one misses exactly the pauses it exists for.
    """
    text = heard.rstrip()
    if text.endswith(("...", "…")):
        return True
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


def build_tts(config: Config) -> TextToSpeech:
    """Instantiate the configured speech engine.

    The only place either engine is named. Swapping them is a config value,
    which is the whole point of the protocol.
    """
    from ciel.tts.macos_say import SayTTS

    if config.tts.engine == "piper":
        try:
            from ciel.tts.piper import PiperTTS

            return PiperTTS(config.tts)
        except ImportError:
            # Piper is an optional extra. A missing install should cost voice
            # quality, not the whole assistant.
            log.warning("piper is not installed — falling back to `say`")

    return SayTTS(config.tts)


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

    def __init__(self, config: Config) -> None:
        self._config = config
        self._stt: SpeechToText = build_stt(config.stt)
        self._tts: TextToSpeech = build_tts(config)
        self._wake: WakeDetector = build_wake_detector(config.wake)
        self._endpointer = Endpointer(config.audio)
        self._speaker = build_speaker_gate(config.voice)

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
        ) = build_tool_server(config)
        if self._memory is not None and self._memory.all():
            log.info("loaded %d memories", len(self._memory.all()))

        self._indicator: Indicator = build_indicator(config.ui)
        # The broker exists even when the shell is disabled — it's inert until
        # someone calls ask(), and the brain only builds a ShellGuard around
        # it when config.shell.enabled says to.
        self._confirm = VoiceConfirmBroker(config)
        # Bound methods, not strings: the brain re-reads these at every
        # connect, so a rotated session's prompt carries whatever the last
        # reflection wrote.
        self._brain = Brain(
            config,
            memory_index_provider=self._memory.index_prompt if self._memory else None,
            confirmer=self._confirm.ask,
            journal=self._journal,
            projects_index_provider=self._projects.index_prompt if self._projects else None,
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

        # Vigil (see ciel.proactive): the queue is the only piece the frame
        # loop ever touches at frame rate, and only through its `pending`
        # boolean — policy, presence, and Quartz run at most once per
        # policy_poll_s, and only while something is actually waiting.
        self._events: EventQueue | None = None
        self._policy: InterruptionPolicy | None = None
        self._presence: PresenceProbe | None = None
        self._vigil_watchers: list = []
        self._next_policy_check = 0.0
        self._hold_sentinel = config.state_dir / "hold"
        """The emergency brake: while this file exists, no proactive event is
        delivered — they queue and wait. A file sentinel rather than config
        because it can be tripped from outside the process (`touch
        ~/.ciel/hold`), survives crashes and restarts, and lifting it is
        just as visible. In-flight speech and user-requested timers are
        untouched — the brake stops new starts, never running work."""
        self._hold_engaged = False  # log the engagement once, not per poll
        if config.proactive.enabled:
            self._events = EventQueue(
                config.proactive.state_file,
                config.proactive.held_max_age_h * 3600,
            )
            self._policy = InterruptionPolicy(config.proactive)
            self._presence = PresenceProbe(config.proactive)
            if config.proactive.calendar_enabled:
                self._vigil_watchers.append(
                    CalendarWatcher(config.proactive, self._events)
                )
            log.info(
                "vigil enabled (%d watcher%s)",
                len(self._vigil_watchers),
                "" if len(self._vigil_watchers) == 1 else "s",
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
        """A spoken or typed "reload" landed; the frame loop performs it from
        idle, exactly as it does for a source change."""

    async def run(self) -> None:
        await self._startup()

        player = Player(self._config.audio, self._tts.sample_rate)
        turn: asyncio.Task[None] | None = None

        try:
            async with MicStream(self._config.audio) as mic, player:
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
                        await self._announce_timers(
                            missed, player, mic, missed=True
                        )

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

                    # Checked above the state dispatch, not inside BUSY: a
                    # turn abandoned by barge-in leaves BUSY while its hook
                    # can still be pending, and a pending confirmation that
                    # stops receiving frames never resolves.
                    if self._confirm.active:
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
                        self._confirm.feed(frame)
                        continue

                    if (
                        self._timers is not None
                        and self._state is not State.BUSY
                        and not self._endpointer.speaking
                        and self._pending_text is None
                    ):
                        # A due timer speaks the moment nothing else owns the
                        # audio: not over Ciel's own turn (BUSY — it fires the
                        # instant the turn ends), not over the user
                        # mid-utterance, not into a Cauchy hold. Awaited
                        # inline, like the wake acknowledgement: announcements
                        # are seconds long, and the mic is drained after so
                        # Ciel's own voice isn't transcribed as a turn.
                        if self._timer_missed_sweep:
                            # First poll after a nap: grace-filter what came
                            # due mid-sleep instead of announcing it stale.
                            self._timer_missed_sweep = False
                            fired = self._timers.pop_missed(
                                self._config.timers.missed_grace_s
                            )
                            timers_missed = True
                        else:
                            fired = self._timers.pop_due()
                            timers_missed = False
                        if fired:
                            await self._announce_timers(
                                fired, player, mic, missed=timers_missed
                            )
                            continue

                    if (
                        self._typed
                        and self._state is not State.BUSY
                        and not self._endpointer.speaking
                        and self._pending_text is None
                    ):
                        # The keyboard takes the turn, but never *steals* one:
                        # not while a turn runs (BUSY — the line queues as the
                        # next turn), not mid-utterance (the spoken sentence
                        # finishes first), not while Cauchy holds a partial
                        # thought (the hold resolves, then the line runs).
                        if self._maintenance is not None and not self._maintenance.done():
                            # Same hastening the wake path does: the typed
                            # turn serializes on the brain's lock, and an
                            # unhurried reflection would hold it for seconds.
                            await self._brain.interrupt()
                        self._state = State.BUSY
                        self._barge_run = 0
                        _ts, line = self._typed.popleft()
                        turn = asyncio.create_task(
                            self._handle_typed_turn(line)
                        )
                        continue

                    if (
                        self._events is not None
                        and self._state is State.WAITING
                        and self._events.pending
                        and time.monotonic() >= self._next_policy_check
                    ):
                        # Vigil: a pending event gets its policy decision.
                        # Fourth in line behind confirmations, timers, and
                        # the keyboard — the user always outranks the
                        # machine — and stricter than the timer lane on
                        # purpose: WAITING only, never into a live listening
                        # window; the held-notes path covers those moments.
                        # The frame-rate cost is the boolean above; presence
                        # and policy run at most once per policy_poll_s.
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
                            continue
                        if self._hold_engaged:
                            self._hold_engaged = False
                            log.info("vigil hold lifted")
                        decision, event = self._decide_proactive()
                        if event is not None and decision is not None:
                            if decision.action == "speak":
                                if self._maintenance is not None and not self._maintenance.done():
                                    # Same hastening as the typed lane: the
                                    # proactive turn serializes on the
                                    # brain's lock.
                                    await self._brain.interrupt()
                                self._state = State.BUSY
                                self._barge_run = 0
                                turn = asyncio.create_task(
                                    self._run_proactive_turn(event, player, mic)
                                )
                            elif decision.action == "hold":
                                self._events.hold(event)
                            else:
                                self._events.drop(event, time.time())
                        continue

                    if self._state is State.WAITING:
                        # Reload only from idle: never yank the process out
                        # from under a conversation in progress. Triggered by
                        # a source change or by the user asking for one.
                        source_changed = (
                            self._watcher is not None and self._watcher.changed
                        )
                        if source_changed or self._reload_pending:
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
                            try:
                                await player.play(self._tts.stream("Reloading."))
                            except Exception:  # noqa: BLE001
                                log.debug("could not announce the reload", exc_info=True)
                            self.reload_requested = True
                            break

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

                        # Only sampled here: while idle and silent, what the
                        # microphone hears is the room itself.
                        self._track_noise_floor(frame)
                        if self._wake.push(frame):
                            if self._maintenance is not None and not self._maintenance.done():
                                # Hasten an in-flight reflection (it still
                                # consumes to its ResultMessage, so no drain
                                # debt); a rotation just finishes — either
                                # way the user's turn serializes on the
                                # brain's lock, masked by the ack.
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
                                turn = asyncio.create_task(
                                    self._handle_turn(None, player, mic, pending_text=pending)
                                )
                            else:
                                self._enter_waiting()
                            continue
                        utterance = self._endpointer.push(frame)
                        if utterance is not None:
                            self._state = State.BUSY
                            self._barge_run = 0
                            turn = asyncio.create_task(
                                self._handle_turn(utterance, player, mic)
                            )

                    elif self._state is State.BUSY:
                        self._check_barge_in(frame, player)
                        if turn is not None and turn.done():
                            turn.result()  # surface any exception from the turn
                            turn = None
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
            if turn is not None and not turn.done():
                turn.cancel()
            await self._shutdown()

    # ── one conversational turn ──────────────────────────────────────────────

    async def _handle_turn(
        self,
        utterance,
        player: Player,
        mic: MicStream,
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

            # Mechanical requests skip the brain entirely: no model latency,
            # no cost, no session traffic. The grammar only fires on whole
            # utterances it is certain about; everything else takes the
            # normal path, so a missed match costs nothing new.
            cmd = match_command(text) if self._config.commands.enabled else None
            if cmd is not None and (
                cmd.kind in ("reload", "dismiss") or self._timers is not None
            ):
                await self._run_local_command(cmd, text, player)
                return

            await self._respond(text, player, mic)
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
        self._indicator.set_state("thinking")
        self._spoke = False
        try:
            print(f"\n  you (typed): {text}")
            self._record("user", text)

            # Same brain bypass as the spoken path; the reply goes to the
            # console because typing is a request for quiet.
            cmd = match_command(text) if self._config.commands.enabled else None
            if cmd is not None and (
                cmd.kind in ("reload", "dismiss") or self._timers is not None
            ):
                if cmd.kind != "dismiss":
                    self._conversed = True
                log.info("local command (typed): %s", cmd.kind)
                reply = self._apply_local_command(cmd)
                if reply is not None:
                    print(f"  ciel: {reply}")
                    self._record("ciel", reply)
                return

            started = time.monotonic()
            # Held Vigil notes, after the local-command bypass — a "cancel
            # the timer" line shouldn't consume notes no model will see.
            text = self._with_held_notes(text)
            # Same aclosing obligation as _respond: ask() holds the brain's
            # turn lock, and an abandoned generator would deadlock the next
            # turn behind it.
            async with aclosing(self._brain.ask(text)) as stream:
                async for kind, sentence in stream:
                    if kind == "escalation":
                        print("  [deep thought engaged]")
                        self._record("event", "deep thought engaged")
                        continue
                    if kind == "thinking":
                        print(f"  ciel (thinking): {sentence}")
                        self._record("ciel-thinking", sentence)
                    else:
                        print(f"  ciel: {sentence}")
                        self._record("ciel", sentence)
                    # Typed exchanges are conversations too: Closure owes
                    # them the same reflection, rotation the same fresh
                    # session afterwards.
                    self._conversed = True
                    self._brain_conversed = True
            log.info(
                "typed turn: %.2fs total, $%.4f",
                time.monotonic() - started,
                self._brain.last_turn_cost_usd,
            )
        except Exception:
            log.exception("typed turn failed")
            self._indicator.set_state("error")
            self._record("event", "typed turn failed")
            print("  (something went wrong there — see the log)", flush=True)
        finally:
            # Same anchoring as the spoken path: a typed turn has no audio to
            # wait for, so its timers start as the turn's text lands. Guarded
            # for the same reason — a failed persist must not crash the loop.
            if self._timers is not None:
                try:
                    self._timers.commit_pending()
                except Exception:  # noqa: BLE001 - a failed commit must not crash the loop
                    log.exception("could not start a pending timer")

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
            elif isinstance(self._wake, HotkeyWake):
                # A bare Enter in hotkey mode is the wake signal, delivered
                # through the one stdin reader so nothing competes for fd 0.
                self._wake.arm()

    async def _run_local_command(
        self, cmd: Command, text: str, player: Player
    ) -> None:
        """Speak a locally-handled turn — same console and transcript shape
        as ``_respond``, just with nothing to wait for."""
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
        # _spoke is set unconditionally, exactly as _respond_stream does: an
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

    async def _respond(self, text: str, player: Player, mic: MicStream) -> None:
        print(f"\n  you: {text}")
        self._record("user", text)
        # Held Vigil notes ride into the model's view of this turn; the
        # print and record above stay the user's raw words.
        text = self._with_held_notes(text)
        started = time.monotonic()
        self._interrupted = False

        # The audible "I heard you": a short filler spoken if the brain is
        # still silent after a grace window. Launched as a task so it overlaps
        # the model's latency instead of adding to it; every later play awaits
        # it first so two voices never overlap. A first sentence that beats
        # the window cancels the filler unspoken — "Let me think about that."
        # chased instantly by the answer is theater, and it would delay the
        # real reply behind its own playback. Deliberately does not set
        # _spoke — a filler alone must not open a follow-up window.
        ack_task: asyncio.Task[None] | None = None
        ack_speaking = False
        if self._config.brain.ack_phrases:

            async def _ack_filler() -> None:
                nonlocal ack_speaking
                await asyncio.sleep(self._config.brain.ack_delay_s)
                ack_speaking = True
                await player.play(
                    self._tts.stream(random.choice(self._config.brain.ack_phrases))
                )

            ack_task = asyncio.create_task(_ack_filler())

        async def ack_done() -> None:
            nonlocal ack_task
            if ack_task is None:
                return
            if not ack_speaking:
                # Still inside the grace window (or between sleep and speech,
                # which is the same thing: nothing audible yet) — skip it.
                ack_task.cancel()
            task, ack_task = ack_task, None
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    # The cancellation was aimed at *us*, not the filler —
                    # swallowing it here would eat the turn's own teardown.
                    raise
            except Exception:  # noqa: BLE001 — a lost filler costs nothing
                log.debug("ack playback failed", exc_info=True)

        try:
            await self._respond_stream(text, player, started, ack_done)
        finally:
            # A brain that yields nothing (or raises) must not leave the
            # filler task dangling into the next turn.
            await ack_done()

    async def _respond_stream(
        self,
        text: str,
        player: Player,
        started: float,
        ack_done: Callable[[], Awaitable[None]],
    ) -> None:
        """The streaming body of a brain turn — split out so ``_respond``
        can guarantee the heard-ack task is awaited on every exit path."""
        first_spoken: float | None = None
        prev_kind: str | None = None
        # aclosing is load-bearing, not tidiness: ask() holds the brain's
        # turn lock, and a `break` below abandons the generator — without an
        # explicit close, the lock stays held until garbage collection, and
        # the next turn (or a reflection) deadlocks behind it.
        async with aclosing(self._brain.ask(text)) as stream:
            async for kind, sentence in stream:
                if self._interrupted:
                    break

                if kind == "escalation":
                    # The model just handed the question to deep-thought —
                    # from here the room goes quiet for as long as the deep
                    # pass takes. If nothing has been spoken yet this turn,
                    # name what is happening; if the model already announced
                    # it in its own words, don't say it twice.
                    print("  [deep thought engaged]")
                    self._record("event", "deep thought engaged")
                    if first_spoken is None:
                        line = "Give me a moment. I want to think this through properly."
                        print(f"  ciel: {line}")
                        self._record("ciel", line)
                        await ack_done()
                        self._indicator.set_state("speaking")
                        first_spoken = time.monotonic() - started
                        completed = await player.play(self._tts.stream(line))
                        self._spoke = True
                        self._conversed = True
                        self._brain_conversed = True
                        if not completed:
                            # Barging in on the announcement means the deep
                            # pass must die too — otherwise the subagent runs
                            # on for a minute and then answers a question the
                            # user already abandoned. A lost output device
                            # abandons it the same way, but says so honestly.
                            await self._abandon_playback(player.device_lost)
                            break
                    self._indicator.set_state("thinking")
                    continue

                # Thinking is always shown once the brain yields it (the
                # console is the record); whether it is also *spoken* is the
                # speak_thinking choice. Show-only reasoning keeps the
                # indicator on "thinking" — "reasoning" means the voice is
                # doing it aloud, and lying about that costs the pill its
                # meaning.
                spoken = kind != "thinking" or self._config.brain.speak_thinking
                if kind == "thinking":
                    print(f"  ciel (thinking): {sentence}")
                    self._record("ciel-thinking", sentence)
                    self._indicator.set_state(
                        "reasoning" if spoken else "thinking"
                    )
                else:
                    if prev_kind == "thinking" and self._config.brain.thinking_chime:
                        # The audible boundary: reasoning is over, what
                        # follows is the answer. When the reasoning was
                        # show-only this tone is doing its best work — the
                        # deliberation was silent, and this is what says the
                        # silence is about to end on purpose.
                        await ack_done()
                        await self._play_chime(player)
                    print(f"  ciel: {sentence}")
                    self._record("ciel", sentence)
                    self._indicator.set_state("speaking")
                prev_kind = kind

                if not spoken:
                    continue
                await ack_done()
                if first_spoken is None:
                    first_spoken = time.monotonic() - started
                completed = await player.play(self._tts.stream(sentence))
                self._spoke = True
                self._conversed = True
                self._brain_conversed = True
                # Back to "thinking" between sentences: the model is still
                # generating, and leaving it on "speaking" during the gap would
                # misreport what Ciel is actually doing.
                self._indicator.set_state("thinking")

                if not completed:
                    # Either the user talked over us — everything queued
                    # behind this sentence answers a question they've moved
                    # on from — or the output device died and nothing is
                    # being heard. Both abandon the turn; the recorded cause
                    # differs, and the difference matters (see the helper).
                    await self._abandon_playback(player.device_lost)
                    break

        if first_spoken is not None:
            # Reconnect is called out separately because it dominates when it
            # happens at all: a client killed by a laptop sleeping overnight
            # is rebuilt lazily on the next turn, and that cost lands inside
            # "to first speech" where it reads as a slow model. Split, a long
            # turn names its own cause.
            reconnect = self._brain.last_reconnect_s
            log.info(
                "turn: %.2fs to first speech (%.2fs reconnect, %.2fs model), "
                "%.2fs total, $%.4f",
                first_spoken,
                reconnect,
                max(0.0, first_spoken - reconnect),
                time.monotonic() - started,
                self._brain.last_turn_cost_usd,
            )

    # ── the proactive turn (Vigil) ───────────────────────────────────────────

    def _decide_proactive(self) -> tuple[Decision | None, ProactiveEvent | None]:
        """Pop the next live event and route it through the policy.

        The caller owns enacting the decision — and owns the event: whatever
        isn't spoken must be held or dropped, never silently discarded.
        """
        assert (
            self._events is not None
            and self._policy is not None
            and self._presence is not None
        )
        now = time.time()
        event = self._events.pop_next(now)
        if event is None:
            return None, None
        local = time.localtime(now)
        decision = self._policy.decide(
            event,
            presence=self._presence.state(time.monotonic()),
            spoken_today=self._events.spoken_count(
                time.strftime("%Y-%m-%d", local)
            ),
            now=now,
            local_minutes=local.tm_hour * 60 + local.tm_min,
        )
        log.info(
            "vigil: %s → %s (%s)", event.summary, decision.action, decision.reason
        )
        return decision, event

    async def _run_proactive_turn(
        self, event: ProactiveEvent, player: Player, mic: MicStream
    ) -> None:
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
        sentences: list[str] = []
        try:
            try:
                # Same shape as reflection, same hazard: the timeout must
                # interrupt the brain, or suppression and unattended mode
                # lift while the turn is still generating.
                with self._confirm.suppress(), self._brain.unattended("proactive"):
                    await asyncio.wait_for(
                        self._collect_unattended(
                            proactive_prompt(event.summary), sentences
                        ),
                        timeout=self._config.proactive.turn_timeout_s,
                    )
            except asyncio.TimeoutError:
                log.debug("proactive turn timed out — interrupting the brain")
                try:
                    await self._brain.interrupt()
                except Exception:  # noqa: BLE001 - best-effort, as in reflection
                    log.debug("interrupt after proactive timeout failed", exc_info=True)
                self._events.hold(event)
                return

            reply = " ".join(sentences).strip()
            valve = proactive_valve(reply) if reply else None
            if valve == "SKIP":
                print("  [proactive: model declined]", flush=True)
                self._record("event", "proactive: declined")
                self._events.drop(event, time.time())
                return
            if valve == "HOLD" or not reply:
                # An empty reply is a malfunction, not a judgement; holding
                # keeps the note owed to the next conversation either way.
                print("  [proactive: held for next conversation]", flush=True)
                self._record("event", "proactive: held")
                self._events.hold(event)
                return

            self._indicator.set_state("speaking")
            await self._play_ring(player)
            played_all = True
            device_lost = False
            for sentence in sentences:
                print(f"  ciel (proactive): {sentence}", flush=True)
                self._record("ciel", f"[proactive] {sentence}")
                if not await player.play(self._tts.stream(sentence)):
                    played_all = False
                    device_lost = player.device_lost
                    break
            if device_lost:
                # Nothing reached the room — the notification did not
                # happen. No budget charge, and the note is still owed:
                # hold it for the next conversation.
                self._record("event", "proactive: audio output lost")
                self._events.hold(event)
                return
            # Barge-in mid-nudge still counts as delivered — the ring and
            # first words reached the room, and the user chose to talk over
            # them; re-delivering would nag.
            self._events.mark_spoken(
                event, time.time(), time.strftime("%Y-%m-%d")
            )
            if not played_all:
                self._record("event", "proactive: interrupted")
            # Rotation is re-armed at the next _enter_waiting — the session
            # grew, so the permanent-session problem stays dead — but
            # reflection isn't owed for a one-line nudge (_brain_conversed
            # stays False), and no follow-up window opens (_spoke stays
            # False), mirroring the timer lane.
            self._conversed = True
        except Exception:  # noqa: BLE001 - an unattended turn must never crash the loop
            log.exception("proactive turn failed")
            self._record("event", "proactive turn failed")
            try:
                self._events.hold(event)
            except Exception:  # noqa: BLE001 - persistence is best-effort here
                log.debug("could not hold the failed event", exc_info=True)
        # No mic.drain() here: the BUSY-exit path in the frame loop drains
        # and re-enters WAITING for every turn task, this one included.

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
        self,
        fired: list[Timer],
        player: Player,
        mic: MicStream,
        missed: bool = False,
    ) -> None:
        """Ring, then speak each fired timer's announcement.

        The unprompted-speech path: no turn, no brain, just the ring and the
        sentence the timer was armed with. The ring comes first because an
        assistant that starts talking out of nowhere startles — a familiar
        tone buys the half-second of "that's the timer" before the words.
        """
        self._indicator.set_state("speaking")
        try:
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
            mic.drain()
            self._indicator.set_state(
                "listening" if self._state is State.LISTENING else "idle"
            )

    async def _play_ring(self, player: Player) -> None:
        """The timer ring: a rising two-note figure, played twice.

        Deliberately not the thinking chime — that tone means "answer coming",
        this one means "Ciel is about to speak although you said nothing".
        Distinct sounds keep those distinguishable by ear alone.
        """
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

    async def _play_chime(self, player: Player) -> None:
        """A soft tone marking the end of spoken reasoning.

        Same voice speaks both reasoning and answer, so the ear needs some
        other boundary. 120 ms of A5 with 20 ms fades — present, not alarming.
        """
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

    def _record(self, speaker: str, text: str) -> None:
        """Append one row to this conversation's transcript, if one is kept.

        Also Vigil's presence tap: every ``user`` row — a spoken turn, a
        typed line, a confirmation answer — is evidence the user is here.
        Tapped here, not in _enter_waiting, so Ciel's own proactive speech
        (which sets _conversed) can never count as the user being around.
        """
        if speaker == "user" and self._presence is not None:
            self._presence.note_conversation(time.monotonic())
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

    def _check_barge_in(self, frame: bytes, player: Player) -> None:
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

    async def _acknowledge(self, player: Player, mic: MicStream) -> None:
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
        self._endpointer.reset()
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
        mode = self._config.wake.mode
        if mode == "wakeword":
            print(f"\nReady. Say \"{self._config.wake.model.replace('_', ' ')}\".")
        elif mode == "always":
            print("\nReady. Just talk.")
        # hotkey prints its own prompt via reset()/start()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def _startup(self) -> None:
        started = time.monotonic()
        # Whisper, the wake model, and the Agent SDK subprocess all take
        # seconds to start; do them together so startup is bounded by the
        # slowest one, not their sum.
        startup = [
            self._warm_up_stt(),
            self._warm_up_tts(),
            self._wake.start(),
            self._indicator.start(),
            self._brain.connect(self._mcp_servers, self._custom_tools),
        ]
        if self._speaker is not None:
            startup.append(self._speaker.warm_up())
        if self._watcher is not None:
            startup.append(self._watcher.start())
        # Vigil watchers start here but never block here: start() only
        # spawns their poll tasks — a permission dialog stalls a watcher,
        # not the greeting.
        startup.extend(w.start() for w in self._vigil_watchers)
        await asyncio.gather(*startup)
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
        try:
            await self._tts.warm_up()
            return
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            log.warning("%s failed to start (%s) — falling back to `say`",
                        type(self._tts).__name__, exc)

        from ciel.tts.macos_say import SayTTS

        self._tts = SayTTS(self._config.tts)
        await self._tts.warm_up()

    async def _shutdown(self) -> None:
        closers = [
            self._stt.close(),
            self._tts.close(),
            self._wake.close(),
            self._brain.close(),
            self._indicator.close(),
        ]
        if self._speaker is not None:
            closers.append(self._speaker.close())
        if self._watcher is not None:
            closers.append(self._watcher.close())
        closers.extend(w.close() for w in self._vigil_watchers)
        await asyncio.gather(*closers, return_exceptions=True)
        if self._transcript is not None:
            self._transcript.close()
        if self._brain.total_cost_usd:
            print(f"\nsession cost: ${self._brain.total_cost_usd:.4f}")


__all__ = ["Pipeline", "State", "build_tts"]
