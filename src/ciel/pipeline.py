"""The orchestrator: wake, listen, transcribe, think, speak.

There is one microphone and one reader of it — this loop. Every component that
needs audio is *fed* frames rather than opening its own stream, which is what
keeps the wake detector and the endpointer from fighting over the device.

The loop is a small state machine. The states matter because the same incoming
frame means completely different things depending on where we are: before the
wake word it's noise to be scored, during an utterance it's speech to be
buffered, and while Ciel is talking it's a possible interruption.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from enum import Enum, auto

import numpy as np

from ciel.audio.input import MicStream, float_to_pcm
from ciel.audio.output import Player
from ciel.audio.vad import Endpointer
from ciel.audio.wake import WakeDetector, build_wake_detector
from ciel.brain.agent import Brain
from ciel.brain.tools import build_tool_server
from ciel.config import SAMPLE_RATE, Config
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


class Pipeline:
    """Runs Ciel until stopped."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._stt: SpeechToText = WhisperSTT(config.stt)
        self._tts: TextToSpeech = build_tts(config)
        self._wake: WakeDetector = build_wake_detector(config.wake)
        self._endpointer = Endpointer(config.audio)

        # Custom tools (memory today, whatever lands in the registry later) are
        # assembled before the brain, because the memory index they expose has
        # to be baked into the system prompt at connect time.
        self._mcp_servers, self._custom_tools, self._memory = build_tool_server(config)
        memory_index = self._memory.index_prompt() if self._memory else None
        if memory_index:
            log.info("loaded %d memories", len(self._memory.all()))

        self._indicator: Indicator = build_indicator(config.ui)
        self._brain = Brain(config, memory_index=memory_index)
        self._transcript: Transcript | None = (
            Transcript(config.transcripts.dir) if config.transcripts.enabled else None
        )
        self._state = State.WAITING
        self._interrupted = False
        self._barge_run = 0
        self._noise_floor: float | None = None
        self._followup_until: float | None = None
        self._spoke = False
        self._pending_text: str | None = None
        self._continue_listening = False

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
                await player.play(self._tts.stream("Ciel here."))
                mic.drain()
                self._announce_ready()

                # One loop, always consuming. The turn runs as a task rather
                # than being awaited here, because the moment this loop stops
                # pulling frames, barge-in becomes impossible — there is
                # nothing left listening for the user talking over Ciel.
                async for frame in mic.frames():
                    if self._state is State.WAITING:
                        # Reload only from idle: never yank the process out
                        # from under a conversation in progress.
                        if self._watcher is not None and self._watcher.changed:
                            print(f"\nsource changed ({self._watcher.changed.name}) — reloading")
                            try:
                                await player.play(self._tts.stream("Reloading."))
                            except Exception:  # noqa: BLE001
                                log.debug("could not announce the reload", exc_info=True)
                            self.reload_requested = True
                            break

                        # Only sampled here: while idle and silent, what the
                        # microphone hears is the room itself.
                        self._track_noise_floor(frame)
                        if self._wake.push(frame):
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
        """Whether the newest speech ends mid-thought.

        A trailing filler ("um") or dangling conjunction ("and") means the
        pause that ended the utterance was hesitation, not completion — worth
        a longer wait, where an ordinary pause is not.
        """
        if self._config.audio.filler_extend_ms <= 0:
            return False
        text = heard.rstrip()
        if text.endswith(("...", "…")):
            return True
        if text.endswith((".", "!", "?")):
            # The transcriber closed the sentence; trust its judgement. This
            # is what keeps "I think so." from reading as a dangling "so".
            return False
        words = text.split()
        if not words:
            return False
        last = re.sub(r"[^a-z']+", "", words[-1].lower())
        return last in self._config.audio.filler_words

    async def _respond(self, text: str, player: Player, mic: MicStream) -> None:
        print(f"\n  you: {text}")
        self._record("user", text)
        started = time.monotonic()
        first_spoken: float | None = None
        self._interrupted = False

        prev_kind: str | None = None
        async for kind, sentence in self._brain.ask(text):
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
            # Back to "thinking" between sentences: the model is still
            # generating, and leaving it on "speaking" during the gap would
            # misreport what Ciel is actually doing.
            self._indicator.set_state("thinking")

            if not completed:
                # The user talked over us. Everything still queued behind
                # this sentence is a reply to a question they've moved on
                # from, so abandon the turn rather than finishing it.
                self._interrupted = True
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
        if self._watcher is not None:
            closers.append(self._watcher.close())
        await asyncio.gather(*closers, return_exceptions=True)
        if self._transcript is not None:
            self._transcript.close()
        if self._brain.total_cost_usd:
            print(f"\nsession cost: ${self._brain.total_cost_usd:.4f}")


__all__ = ["Pipeline", "State", "build_tts"]
