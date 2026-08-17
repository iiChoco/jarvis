"""The orchestrator: wake, listen, transcribe, think, speak.

There is one microphone and one reader of it — this loop. Every component that
needs audio is *fed* frames rather than opening its own stream, which is what
keeps the wake detector and the endpointer from fighting over the device.

The loop is a small state machine. The states matter because the same incoming
frame means completely different things depending on where we are: before the
wake word it's noise to be scored, during an utterance it's speech to be
buffered, and while Ciel is talking it's a possible interruption.

Three of this file's behaviors carry codenames: **Cauchy** (the mid-thought
hold — a transcript that doesn't read as finished means the tail hasn't
settled yet, so don't declare the sequence converged), **Discontinuity**
(barge-in — a jump that ends the
current segment), and **Neighborhood** (the follow-up window — an open ball
around the last turn, inside which no wake word is required).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from contextlib import aclosing
from enum import Enum, auto
from pathlib import Path

import numpy as np

from ciel.audio.input import MicStream, float_to_pcm
from ciel.audio.output import Player
from ciel.audio.speaker import build_speaker_gate
from ciel.audio.vad import Endpointer
from ciel.audio.wake import WakeDetector, build_wake_detector
from ciel.brain.agent import Brain
from ciel.brain.prompt import REFLECTION_PROMPT
from ciel.brain.tools import build_tool_server
from ciel.config import SAMPLE_RATE, AudioConfig, Config
from ciel.confirm import VoiceConfirmBroker
from ciel.reload import SourceWatcher, default_roots
from ciel.stt.base import SpeechToText
from ciel.stt.local_whisper import WhisperSTT
from ciel.transcript import Transcript
from ciel.tts.base import TextToSpeech
from ciel.ui.indicator import Indicator, build_indicator

log = logging.getLogger(__name__)


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

    def conversation_ended(self, now: float) -> None:
        """Re-arms both deadlines — rotation is always relative to the
        *latest* conversation, not the first."""
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
        self._stt: SpeechToText = WhisperSTT(config.stt)
        self._tts: TextToSpeech = build_tts(config)
        self._wake: WakeDetector = build_wake_detector(config.wake)
        self._endpointer = Endpointer(config.audio)
        self._speaker = build_speaker_gate(config.voice)

        # Custom tools (memory today, whatever lands in the registry later) are
        # assembled before the brain, because the memory index they expose has
        # to be baked into the system prompt at connect time.
        self._mcp_servers, self._custom_tools, self._memory, self._projects, self._journal = (
            build_tool_server(config)
        )
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
        self._pending_text: str | None = None
        self._continue_listening = False

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

        self._watcher: SourceWatcher | None = (
            SourceWatcher(default_roots(config.state_dir))
            if config.dev.autoreload
            else None
        )
        self.reload_requested = False
        """Set when the source changed and the loop exited to be re-exec'd.
        The entry point reads this after run() returns."""

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
                await player.play(self._tts.stream("Ciel here."))
                mic.drain()
                self._announce_ready()

                # One loop, always consuming. The turn runs as a task rather
                # than being awaited here, because the moment this loop stops
                # pulling frames, barge-in becomes impossible — there is
                # nothing left listening for the user talking over Ciel.
                async for frame in mic.frames():
                    # Checked above the state dispatch, not inside BUSY: a
                    # turn abandoned by barge-in leaves BUSY while its hook
                    # can still be pending, and a pending confirmation that
                    # stops receiving frames never resolves.
                    if self._confirm.active:
                        self._confirm.feed(frame)
                        continue

                    if self._state is State.WAITING:
                        # Reload only from idle: never yank the process out
                        # from under a conversation in progress.
                        if self._watcher is not None and self._watcher.changed:
                            print(f"\nsource changed ({self._watcher.changed.name}) — reloading")
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
                    return

            if utterance is not None:
                heard = await self._stt.transcribe(utterance)
                pending = self._pending_text
                if not heard and not pending:
                    log.debug("nothing intelligible in %.2fs of audio", len(utterance) / SAMPLE_RATE)
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
                # The continuation window lapsed with nothing more said.
                text = pending_text
            assert text is not None

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

    async def _respond(self, text: str, player: Player, mic: MicStream) -> None:
        print(f"\n  you: {text}")
        self._record("user", text)
        started = time.monotonic()
        first_spoken: float | None = None
        self._interrupted = False

        prev_kind: str | None = None
        # aclosing is load-bearing, not tidiness: ask() holds the brain's
        # turn lock, and a `break` below abandons the generator — without an
        # explicit close, the lock stays held until garbage collection, and
        # the next turn (or a reflection) deadlocks behind it.
        async with aclosing(self._brain.ask(text)) as stream:
            async for kind, sentence in stream:
                if self._interrupted:
                    break

                if first_spoken is None:
                    first_spoken = time.monotonic() - started

                if kind == "thinking":
                    print(f"  ciel (thinking): {sentence}")
                    self._record("ciel-thinking", sentence)
                    self._indicator.set_state("reasoning")
                else:
                    if prev_kind == "thinking" and self._config.brain.thinking_chime:
                        # The audible boundary: reasoning is over, what follows
                        # is the answer.
                        await self._play_chime(player)
                    print(f"  ciel: {sentence}")
                    self._record("ciel", sentence)
                    self._indicator.set_state("speaking")
                prev_kind = kind

                completed = await player.play(self._tts.stream(sentence))
                self._spoke = True
                self._conversed = True
                # Back to "thinking" between sentences: the model is still
                # generating, and leaving it on "speaking" during the gap would
                # misreport what Ciel is actually doing.
                self._indicator.set_state("thinking")

                if not completed:
                    # The user talked over us. Everything still queued behind
                    # this sentence is a reply to a question they've moved on
                    # from, so abandon the turn rather than finishing it. A
                    # confirmation racing in from this dying turn's hook is
                    # resolved to deny for the same reason.
                    self._interrupted = True
                    self._confirm.cancel("interrupted")
                    await self._brain.interrupt()
                    print("  (interrupted)")
                    self._record("event", "interrupted")
                    break

        if first_spoken is not None:
            log.info(
                "turn: %.2fs to first speech, %.2fs total, $%.4f",
                first_spoken,
                time.monotonic() - started,
                self._brain.last_turn_cost_usd,
            )

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
        """Append one row to this conversation's transcript, if one is kept."""
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
            # re-arm and reflect the same conversation twice.
            self._schedule.conversation_ended(time.monotonic())
            self._conversed = False

    async def _run_reflection(self) -> None:
        """One silent Closure turn; failures are logged shrugs, never crashes."""
        print("\n  [reflecting]", flush=True)
        self._record("event", "reflection")
        try:
            # Suppressed confirmations: nobody is listening, so a stray
            # confirm-tier tool call inside the reflection denies silently
            # instead of voicing a question into an empty room.
            with self._confirm.suppress():
                await asyncio.wait_for(
                    self._brain.reflect(REFLECTION_PROMPT),
                    timeout=self._config.reflection.timeout_s,
                )
        except Exception:  # noqa: BLE001 - reflection is best-effort
            log.debug("reflection failed", exc_info=True)

    async def _run_rotation(self) -> None:
        """Retire the idle session; the next conversation starts clean."""
        try:
            await self._brain.rotate()
            print("\n  [fresh session]", flush=True)
            self._record("event", "session rotated")
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
            self._stt.warm_up(),
            self._warm_up_tts(),
            self._wake.start(),
            self._indicator.start(),
            self._brain.connect(self._mcp_servers, self._custom_tools),
        ]
        if self._speaker is not None:
            startup.append(self._speaker.warm_up())
        if self._watcher is not None:
            startup.append(self._watcher.start())
        await asyncio.gather(*startup)
        log.info("ready in %.1fs", time.monotonic() - started)

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
        await asyncio.gather(*closers, return_exceptions=True)
        if self._transcript is not None:
            self._transcript.close()
        if self._brain.total_cost_usd:
            print(f"\nsession cost: ${self._brain.total_cost_usd:.4f}")


__all__ = ["Pipeline", "State", "build_tts"]
