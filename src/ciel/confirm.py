"""Spoken yes-or-no confirmation, bridging a brain hook to the mic loop.

Codename: **Proof Obligation** — a side-effectful step is not permitted until
its obligation has been discharged out loud.

The tool guards — the shell gate, the connector gate, whatever fronts an
outward-acting tool next — need to ask the user a question and hear the
answer, but they run inside PreToolUse hooks — brain-side, mid-turn — while
the pipeline's frame loop is the only reader of the microphone. This broker
is the bridge:
the hook awaits :meth:`ask`, and the frame loop routes frames to :meth:`feed`
whenever :attr:`active` is set, so the one-mic-reader rule holds.

The choreography in :meth:`ask` is fussy because three parties share the
speaker and microphone:

* The turn's own sentence may still be playing when the hook fires. Wait for
  it — stopping a play ``_respond`` owns makes that play return ``False``,
  which ``_respond`` reads as barge-in and answers with ``brain.interrupt()``
  while the hook is still pending.
* The prompt this broker speaks is heard by the open microphone. Drain before
  listening, or Ciel confirms its own question.
* The user may answer early, walk away, or barge in on the turn itself. Every
  such path resolves the pending ask to *deny* — an unresolved hook wedges the
  Agent SDK subprocess, and a wedged subprocess is a dead assistant.

The answer window mirrors the follow-up window's contract: the deadline covers
*starting* to speak, and once the endpointer has speech in hand a slow
"hmm... yes, go ahead" is never cut off at the clock.

A typed line is as good as a spoken one: the pipeline routes keyboard input to
:meth:`answer` while a question is pending, so a turn that arrived through the
chatbox can be confirmed without switching to voice. Same classifier, same
one-re-prompt-then-deny contract — the modality changes, the obligation
doesn't.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from enum import Enum, auto
from typing import Callable, Iterator

import numpy as np

from ciel.audio.input import MicStream
from ciel.audio.output import Player
from ciel.audio.vad import Endpointer
from ciel.config import Config
from ciel.stt.base import SpeechToText
from ciel.tts.base import TextToSpeech

log = logging.getLogger(__name__)

# First tokens that read as an answer. Kept small and unambiguous: "sure thing
# boss" matches on "sure"; anything outside both sets earns one re-prompt and
# then a deny, because a gate that guesses is not a gate.
_YES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "affirmative",
    "go", "do", "please", "confirm", "confirmed", "approve", "approved",
})
_NO = frozenset({
    "no", "nope", "nah", "don't", "dont", "stop", "cancel", "negative",
    "never", "skip", "abort", "deny", "denied",
})


def yes_no(text: str) -> bool | None:
    """Read an answer out of a transcript: True, False, or None if unclear."""
    words = [w.strip(".,!?'\"").lower() for w in text.split()]
    words = [w for w in words if w]
    if not words:
        return None
    first = words[0]
    # "never mind" and "go ahead" both hinge on the first word; the sets are
    # chosen so the first hit is decisive ("no, wait, yes" is a None the
    # re-prompt untangles better than a guess would).
    if first in _YES and not any(w in _NO for w in words):
        return True
    if first in _NO and not any(w in _YES for w in words):
        return False
    return None


class _Phase(Enum):
    IDLE = auto()       # no confirmation in flight; the frame loop ignores us
    WAIT_IDLE = auto()  # waiting out the turn's own sentence; frames dropped
    PROMPT = auto()     # speaking the question; frames watched for early answers
    LISTEN = auto()     # awaiting the answer; frames go to the endpointer


class VoiceConfirmBroker:
    """Asks the user a spoken yes/no question and returns their answer."""

    def __init__(self, config: Config, endpointer: Endpointer | None = None) -> None:
        self._config = config
        # Its own endpointer, never the pipeline's — that one's state belongs
        # to LISTENING. Injectable so tests can script utterances instead of
        # synthesizing webrtcvad-passing PCM.
        self._endpointer = endpointer if endpointer is not None else Endpointer(config.audio)
        self._lock = asyncio.Lock()
        self._cancelled = asyncio.Event()
        self._phase = _Phase.IDLE
        self._suppressed = 0
        self._utterance: asyncio.Future[np.ndarray | None] | None = None
        self._typed_answer: str | None = None
        self._barge_run = 0

        self._player: Player | None = None
        self._mic: MicStream | None = None
        self._stt: SpeechToText | None = None
        self._tts: TextToSpeech | None = None
        self._noise_floor: Callable[[], float | None] = lambda: None
        self._record: Callable[[str, str], None] | None = None

    # ── pipeline-side wiring ─────────────────────────────────────────────────

    def bind(
        self,
        *,
        player: Player,
        mic: MicStream,
        stt: SpeechToText,
        tts: TextToSpeech,
        noise_floor: Callable[[], float | None],
        record: Callable[[str, str], None] | None = None,
    ) -> None:
        """Attach the audio machinery, which only exists inside ``run()``.

        ``record`` is the pipeline's transcript writer, so the confirmation
        exchange lands in the conversation record like every other spoken
        sentence — a gate whose questions are missing from the transcript
        reads later as actions that ran unasked.
        """
        self._player = player
        self._mic = mic
        self._stt = stt
        self._tts = tts
        self._noise_floor = noise_floor
        self._record = record

    def unbind(self) -> None:
        self._player = None
        self._mic = None
        self._stt = None
        self._tts = None
        self._noise_floor = lambda: None
        self._record = None

    @property
    def active(self) -> bool:
        """Whether the frame loop should be routing frames to :meth:`feed`."""
        return self._phase is not _Phase.IDLE

    def feed(self, frame: bytes) -> None:
        """Accept one mic frame from the frame loop; meaning depends on phase."""
        if self._phase is _Phase.PROMPT:
            self._check_early_answer(frame)
        elif self._phase is _Phase.LISTEN:
            utterance = self._endpointer.push(frame)
            if (
                utterance is not None
                and self._utterance is not None
                and not self._utterance.done()
            ):
                self._utterance.set_result(utterance)
        # WAIT_IDLE: dropped. The playing sentence belongs to _respond;
        # interrupting it here would read as barge-in and abandon the turn.

    def answer(self, text: str) -> bool:
        """Accept a typed answer to the pending question.

        Returns whether the line was consumed — False outside PROMPT/LISTEN,
        so the pipeline keeps unconsumed lines queued as ordinary turns
        instead of losing them. WAIT_IDLE is deliberately excluded: the
        question hasn't been put yet, and a line typed before it appears is
        far more likely the user's next request than a clairvoyant answer.

        Typing over the prompt is the keyboard's barge-in, and unlike the
        acoustic kind it needs no echo cancellation to be trusted — a
        keystroke is never Ciel's own voice. Stopping the prompt here is
        broker-owned, same as :meth:`_check_early_answer`.
        """
        if self._phase not in (_Phase.PROMPT, _Phase.LISTEN):
            return False
        self._typed_answer = text
        if self._phase is _Phase.PROMPT and self._player is not None:
            self._player.stop()
        return True

    @contextmanager
    def suppress(self) -> "Iterator[None]":
        """Deny every confirmation inside the block, silently and instantly.

        For turns with nobody listening — the reflection pass runs at idle,
        and a stray confirm-tier tool call must not voice a question into an
        empty room. A depth counter rather than a flag, so nested use and
        exceptions can never leave suppression stuck on.
        """
        self._suppressed += 1
        try:
            yield
        finally:
            self._suppressed -= 1

    def cancel(self, reason: str = "") -> None:
        """Resolve any pending ask to deny, immediately.

        Called on real barge-in and at shutdown — both paths where nobody is
        going to answer, and where a hook left awaiting would hang the SDK.
        """
        if self.active:
            log.debug("confirmation cancelled%s", f" ({reason})" if reason else "")
        self._cancelled.set()
        if self._utterance is not None and not self._utterance.done():
            self._utterance.set_result(None)

    # ── hook-side entry point ────────────────────────────────────────────────

    async def ask(self, question: str) -> bool:
        """Speak the question, listen for yes or no. Anything else is no.

        The question arrives fully formed — "Run: git push — okay?", "Send an
        email to Sam — okay?" — because only the guard that built it knows
        what the action is. This broker owns the choreography, not the words.
        """
        if self._suppressed:
            log.info("confirmation suppressed (nobody listening): %s", question)
            return False
        if self._player is None or self._mic is None or self._stt is None or self._tts is None:
            return False
        if self._lock.locked():
            # A second question while one is pending. Deny rather than queue:
            # stacked spoken questions are unanswerable ("yes" to which?).
            return False

        async with self._lock:
            self._cancelled.clear()
            self._typed_answer = None  # a cancel can leave a stale one behind
            self._phase = _Phase.WAIT_IDLE
            try:
                while self._player.is_playing:
                    if self._cancelled.is_set():
                        return False
                    await asyncio.sleep(0.05)
                if self._cancelled.is_set():
                    return False

                await self._speak(question)

                # One re-prompt on an unclear answer, then deny. A gate that
                # keeps asking becomes background noise to say yes at.
                for _ in range(2):
                    if self._cancelled.is_set():
                        return False
                    self._mic.drain()  # our own prompt is in the queue
                    self._endpointer.reset()
                    self._phase = _Phase.LISTEN
                    utterance = await self._await_utterance()
                    typed, self._typed_answer = self._typed_answer, None
                    if self._cancelled.is_set():
                        return False
                    if typed is not None:
                        heard = typed.strip()
                        print(f"  you (confirm, typed): {heard}", flush=True)
                    elif utterance is None:
                        await self._speak("No answer — skipping it.")
                        return False
                    else:
                        heard = (await self._stt.transcribe(utterance)).strip()
                        print(f"  you (confirm): {heard}", flush=True)
                    if self._record is not None:
                        self._record("you-confirm", heard)
                    verdict = yes_no(heard)
                    if verdict is True:
                        return True
                    if verdict is False:
                        await self._speak("Okay, skipping it.")
                        return False
                    await self._speak("Yes or no?")
                return False
            finally:
                self._phase = _Phase.IDLE
                self._endpointer.reset()

    # ── internals ────────────────────────────────────────────────────────────

    async def _speak(self, text: str) -> None:
        """Play a prompt, tolerating an early answer or a dead TTS.

        An early answer (barge-in during PROMPT) stops this play — that stop is
        broker-owned and simply means "get to the listening part", never a turn
        abandonment. A TTS failure falls through to listening too: a gate that
        cannot ask must still resolve, and silence then deny is the safe shape.
        """
        assert self._player is not None and self._tts is not None
        self._phase = _Phase.PROMPT
        self._barge_run = 0
        # Echoed like every other spoken sentence: the console and transcript
        # are the record of what was said, and a gate that asks invisibly
        # reads later as an action that ran unasked.
        print(f"\n  ciel (confirm): {text}", flush=True)
        if self._record is not None:
            self._record("ciel-confirm", text)
        try:
            await self._player.play(self._tts.stream(text))
        except Exception:  # noqa: BLE001
            log.debug("could not speak the confirmation prompt", exc_info=True)

    def _check_early_answer(self, frame: bytes) -> None:
        """Let the user answer over the prompt, when barge-in is trusted.

        The same RMS-versus-floor test the pipeline uses, and the same
        reasoning about why it's gated on ``audio.barge_in``: without echo
        cancellation the prompt's own voice qualifies. Stopping here only ends
        the *prompt*; the swallowed first syllable is the honest cost, covered
        by the re-prompt.
        """
        audio = self._config.audio
        if not audio.barge_in or self._player is None or not self._player.is_playing:
            self._barge_run = 0
            return
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples)))
        floor = self._noise_floor() or 0.0
        if rms < max(floor * audio.barge_in_ratio, audio.barge_in_floor):
            self._barge_run = 0
            return
        self._barge_run += 1
        if self._barge_run >= audio.barge_in_frames:
            self._barge_run = 0
            self._player.stop()

    async def _await_utterance(self) -> np.ndarray | None:
        """Wait for one utterance, a timeout, or a cancellation.

        Polled at frame cadence rather than ``wait_for`` because the deadline
        has the follow-up window's shape: it only applies until speech starts.
        """
        deadline = time.monotonic() + self._config.shell.confirm_timeout_ms / 1000
        loop = asyncio.get_running_loop()
        self._utterance = loop.create_future()
        try:
            while True:
                if self._utterance.done():
                    return self._utterance.result()
                if self._typed_answer is not None or self._cancelled.is_set():
                    return None
                if time.monotonic() >= deadline and not self._endpointer.speaking:
                    return None
                await asyncio.sleep(0.03)
        finally:
            self._utterance = None


__all__ = ["VoiceConfirmBroker", "yes_no"]
