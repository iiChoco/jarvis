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

The obligation also follows the user out the door. A turn that arrived over
a text lane — the Discord link, or the local web GUI — runs inside
:meth:`remote`, and a question asked there is *shown*, not spoken: the
person who asked is at their phone or behind a keyboard in a quiet room,
and a question voiced into an empty room discharges nothing. The answer
comes back over a text lane (the pipeline routes owner messages to
:meth:`answer` exactly as it routes typed lines), against a
notification-time deadline rather than a conversation-time one. Remote
answers are only ever accepted for remote-origin questions — a question
asked *aloud at home* must not be answerable by someone who never heard it
— and lane-matched besides: a Discord text answers only a Discord-origin
question, because a question shown on the loopback page was never shown to
the phone. (The page may answer either origin; every question is mirrored
to it as a ciel-confirm row.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from enum import Enum, auto
from typing import Awaitable, Callable, Iterator

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
        self._endpointer = (
            endpointer
            if endpointer is not None
            # The floor callable is rebound at bind() time; routing through
            # self keeps the answer window's endpointing noise-gated with
            # whatever estimate the pipeline currently holds.
            else Endpointer(config.audio, noise_floor=lambda: self._noise_floor())
        )
        self._lock = asyncio.Lock()
        self._cancelled = asyncio.Event()
        self._phase = _Phase.IDLE
        self._suppressed = 0
        self._utterance: asyncio.Future[np.ndarray | None] | None = None
        self._typed_answer: str | None = None
        self._asked_at: float | None = None
        self._asked_at_wall: float | None = None
        self._barge_run = 0
        self._remote_send: Callable[[str], Awaitable[None]] | None = None
        """The text lane's reply sink while a remote turn is in flight
        (see :meth:`remote`); None for every attended turn. Its presence is
        what routes :meth:`ask` through the texted choreography."""
        self._remote_origin: str | None = None
        """Which lane installed the sink — "discord" or "web" — so an
        answer is only ever accepted from a lane that showed the question
        (see :attr:`remote_origin`)."""

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

    def bind_record(self, record: Callable[[str, str], None]) -> None:
        """The hub's binding: no audio — every question here travels a
        sink — but the transcript writer, so the exchange is recorded."""
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

    @property
    def asked_at(self) -> float | None:
        """Monotonic time the current question was first put, or None if no
        question is awaiting an answer.

        The pipeline uses this to reject a typed line that predates the
        question: a request typed while the turn was still busy is the user's
        *next* turn, not a clairvoyant answer, and consuming it would both eat
        the request and let a line beginning "yes"/"no" silently decide a gated
        action nobody was answering.
        """
        return self._asked_at

    @property
    def asked_at_wall(self) -> float | None:
        """Wall-clock twin of :attr:`asked_at` — same lifecycle, same
        None-when-idle. The monotonic stamp stays the predating rule's
        clock on this machine (it can't jump backwards); the wall twin
        exists for the hub split, whose gating only ever compares wall
        stamps minted on the same host that reads them."""
        return self._asked_at_wall

    def feed(self, frame: bytes) -> None:
        """Accept one mic frame from the frame loop; meaning depends on phase."""
        if self._phase is _Phase.PROMPT:
            self._check_early_answer(frame)
        elif self._phase is _Phase.LISTEN:
            if self._utterance is None:
                # A remote question: listening to the link, not the room.
                # No future was created, so there is no one to endpoint for.
                return
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
    def remote(
        self, send: Callable[[str], Awaitable[None]], origin: str = "discord"
    ) -> "Iterator[None]":
        """Route confirmations through a remote text channel for the block.

        Engaged around a whole remote turn, the way :meth:`suppress` wraps
        an unattended one: any confirm-tier tool call inside texts its
        question via ``send`` and waits for the owner's reply instead of
        speaking to a room the user is not in. ``origin`` names the lane
        ("discord", "web", or "spoke" — the hub's voice lane, whose sink
        speaks the question through the spoke and takes ``listen=`` to
        know which lines expect an answer) so the pipeline can accept an
        answer only from a lane that actually showed the question. Not a
        counter — remote
        turns never nest (one turn slot) — but exception-safe the same
        way."""
        self._remote_send = send
        self._remote_origin = origin
        try:
            yield
        finally:
            self._remote_send = None
            self._remote_origin = None

    @property
    def remote_active(self) -> bool:
        """Whether a *remote-origin* question is awaiting an answer — the
        pipeline's cue to route text-lane messages to :meth:`answer`. False
        for a pending spoken question on purpose: a remote yes to a
        question voiced at home would approve an action its approver
        never heard."""
        return self._remote_send is not None and self.active

    @property
    def remote_origin(self) -> str | None:
        """Which lane asked the pending remote question, or None outside a
        remote turn. The pipeline routes a Discord answer only when this is
        "discord": a web-origin question is visible only on the loopback
        page, and a text from a phone that never showed it is not an
        answer. (The web page may answer *any* origin — every question is
        mirrored to it as a ciel-confirm row, so its reader has seen it.)"""
        return self._remote_origin

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
        if self._lock.locked():
            # A second question while one is pending. Deny rather than queue:
            # stacked spoken questions are unanswerable ("yes" to which?).
            return False
        if self._remote_send is not None:
            # A remote turn's question goes where the asker is. Dispatched
            # before the binding check below: the texted choreography needs
            # no player or microphone, and must survive their absence.
            return await self._ask_remote(question)
        if self._player is None or self._mic is None or self._stt is None or self._tts is None:
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

                self._asked_at = time.monotonic()
                self._asked_at_wall = time.time()
                await self._speak(question)

                # One re-prompt on an unclear answer, then deny. A gate that
                # keeps asking becomes background noise to say yes at.
                attempts = 2
                for attempt in range(attempts):
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
                        await self._announce("No answer — skipping it.")
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
                        await self._announce("Okay, skipping it.")
                        return False
                    # Unclear. Re-prompt only if another listen follows;
                    # speaking "Yes or no?" after the last attempt would ask a
                    # question nothing is left to hear the answer to.
                    if attempt < attempts - 1:
                        await self._speak("Yes or no?")
                await self._announce("I'll take that as a no.")
                return False
            finally:
                self._phase = _Phase.IDLE
                self._asked_at = None
                self._asked_at_wall = None
                self._endpointer.reset()

    async def _ask_remote(self, question: str) -> bool:
        """Text the question, await a texted (or locally typed) yes or no.

        The spoken choreography's away image, with the audio parties
        deleted: no WAIT_IDLE (nothing is playing for the asker), no
        prompt playback, no endpointer. The phase sits in LISTEN for the
        whole wait so :attr:`active` holds — which is what keeps the frame
        loop routing answers here — while :meth:`feed` stays inert
        (``_utterance`` is never created, so mic frames fall through).
        Same one-re-prompt-then-deny contract, same cancel-means-deny;
        the deadline runs on notification time (``discord.confirm_timeout_s``),
        not the spoken gate's eight seconds. A question that cannot even
        be sent resolves to deny — a gate that cannot ask must still
        resolve, and silently-no is its safe shape.
        """
        send = self._remote_send
        assert send is not None
        # The spoke origin is the spoken choreography carried over the
        # wire: the question is *spoken* by the spoke and the answer is
        # the user's voice at the machine — presence evidence, so the row
        # keeps the spoken lane's label — and the spoke reports its own
        # listening timeout as an empty answer, which is "no answer".
        spoken = self._remote_origin == "spoke"
        async with self._lock:
            self._cancelled.clear()
            self._typed_answer = None  # a cancel can leave a stale one behind
            self._phase = _Phase.LISTEN
            self._asked_at = time.monotonic()
            self._asked_at_wall = time.time()
            try:
                if not await self._send_remote(send, question, listen=True):
                    return False
                attempts = 2
                for attempt in range(attempts):
                    heard = await self._await_remote_answer()
                    if self._cancelled.is_set():
                        return False
                    if heard is None or (spoken and not heard):
                        await self._send_remote(send, "No answer — skipping it.")
                        return False
                    tag = "confirm" if spoken else "confirm, remote"
                    print(f"  you ({tag}): {heard}", flush=True)
                    if self._record is not None:
                        # Not "you-confirm" for a text lane: that row is a
                        # presence signal (the user demonstrably *here*),
                        # and a remote answer is evidence of exactly the
                        # opposite. The spoke's answer was spoken here.
                        self._record(
                            "you-confirm" if spoken else "you-confirm-remote",
                            heard,
                        )
                    verdict = yes_no(heard)
                    if verdict is True:
                        return True
                    if verdict is False:
                        await self._send_remote(send, "Okay, skipping it.")
                        return False
                    if attempt < attempts - 1:
                        if not await self._send_remote(send, "Yes or no?", listen=True):
                            return False
                await self._send_remote(send, "I'll take that as a no.")
                return False
            finally:
                self._phase = _Phase.IDLE
                self._asked_at = None
                self._asked_at_wall = None

    async def _send_remote(
        self,
        send: Callable[..., Awaitable[None]],
        text: str,
        *,
        listen: bool = False,
    ) -> bool:
        """One line over the link, echoed and recorded like every spoken
        prompt. Returns whether it plausibly reached the user — the
        caller treats a failed *question* as unanswerable (deny) and a
        failed closing line as cosmetic. ``listen`` marks a line an
        answer is expected to; only the spoke sink is told (it must open
        the microphone after speaking it), text lanes never needed to
        know."""
        tag = "confirm" if self._remote_origin == "spoke" else "confirm, remote"
        print(f"\n  ciel ({tag}): {text}", flush=True)
        if self._record is not None:
            self._record("ciel-confirm", text)
        try:
            if self._remote_origin == "spoke":
                await send(text, listen=listen)
            else:
                await send(text)
            return True
        except Exception:  # noqa: BLE001 - a dead link means an unaskable question
            log.warning("could not text the confirmation prompt", exc_info=True)
            return False

    async def _await_remote_answer(self) -> str | None:
        """Wait for a text answer, a cancellation, or the remote deadline.

        Polled like :meth:`_await_utterance`, but with nothing speech-shaped
        to extend the deadline for — a text either arrived or it didn't.
        """
        timeout = (
            self._config.hub.confirm_timeout_s
            if self._remote_origin == "spoke"
            else self._config.discord.confirm_timeout_s
        )
        deadline = time.monotonic() + timeout
        while True:
            if self._typed_answer is not None:
                answer, self._typed_answer = self._typed_answer, None
                return answer.strip()
            if self._cancelled.is_set() or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)

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

    async def _announce(self, text: str) -> None:
        """Speak a closing line after the verdict is decided, without listening.

        The phase drops to IDLE first so :meth:`answer` stops accepting input
        for the rest of this exchange: the decision is made, and a line typed
        during "Okay, skipping it." is the user's next request, not an answer to
        eat. Otherwise identical to :meth:`_speak` — echoed and recorded.
        """
        assert self._player is not None and self._tts is not None
        self._phase = _Phase.IDLE
        print(f"\n  ciel (confirm): {text}", flush=True)
        if self._record is not None:
            self._record("ciel-confirm", text)
        try:
            await self._player.play(self._tts.stream(text))
        except Exception:  # noqa: BLE001
            log.debug("could not speak the confirmation closing", exc_info=True)

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
