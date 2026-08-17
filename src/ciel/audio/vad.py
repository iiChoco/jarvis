"""Utterance endpointing — deciding when the user started and stopped talking.

webrtcvad classifies each 30 ms frame as speech or not. That per-frame verdict
is noisy on its own, so this wraps it in the two pieces of state that make it
usable:

* a **preroll** ring buffer, so the audio *before* the trigger is kept. VAD
  always fires a frame or two late, and without preroll the first phoneme is
  clipped — which is exactly the kind of error that makes Whisper drop or
  invent the opening word.
* a **hangover** counter, so a natural pause mid-sentence doesn't end the turn.
  A single silent frame means nothing; 700 ms of them means the user is done.
"""

from __future__ import annotations

import logging
from collections import deque
from enum import Enum, auto

import numpy as np
import webrtcvad

from ciel.audio.input import pcm_to_float
from ciel.config import FRAME_BYTES, FRAME_MS, SAMPLE_RATE, AudioConfig

log = logging.getLogger(__name__)


class _State(Enum):
    WAITING = auto()   # no speech yet; filling the preroll buffer
    SPEAKING = auto()  # speech detected; accumulating the utterance


class Endpointer:
    """Feeds frames in, gets complete utterances out.

    Stateful and single-turn: call :meth:`push` per frame until it returns an
    array, which is the finished utterance. It resets itself afterwards, ready
    for the next one.
    """

    def __init__(self, config: AudioConfig) -> None:
        self._config = config
        self._vad = webrtcvad.Vad(config.vad_aggressiveness)

        preroll_frames = max(1, config.preroll_ms // FRAME_MS)
        self._preroll: deque[bytes] = deque(maxlen=preroll_frames)

        self._silence_limit = max(1, config.silence_ms // FRAME_MS)
        self._min_frames = max(1, config.min_utterance_ms // FRAME_MS)
        self._max_frames = int(config.max_utterance_s * 1000 // FRAME_MS)

        self._state = _State.WAITING
        self._frames: list[bytes] = []
        self._silent_run = 0
        self._lead = 0  # preroll frames at the head of the current utterance

    @property
    def speaking(self) -> bool:
        """Whether an utterance is currently in progress."""
        return self._state is _State.SPEAKING

    def push(self, frame: bytes) -> np.ndarray | None:
        """Feed one 30 ms frame.

        Returns the complete utterance as float32 samples once the user stops
        talking, or ``None`` while still listening. Utterances shorter than
        ``min_utterance_ms`` are swallowed and ``None`` is returned — those are
        coughs and desk bumps, not speech worth transcribing.
        """
        if len(frame) != FRAME_BYTES:
            # A short frame at stream teardown is normal; anything else means
            # blocksize and FRAME_SAMPLES have drifted apart.
            return None

        is_speech = self._vad.is_speech(frame, SAMPLE_RATE)

        if self._state is _State.WAITING:
            if is_speech:
                # Preroll first, so the utterance begins slightly *before* the
                # frame that tripped the detector.
                self._lead = len(self._preroll)
                self._frames = [*self._preroll, frame]
                self._preroll.clear()
                self._state = _State.SPEAKING
                self._silent_run = 0
            else:
                self._preroll.append(frame)
            return None

        # SPEAKING
        self._frames.append(frame)
        self._silent_run = 0 if is_speech else self._silent_run + 1

        if self._silent_run >= self._silence_limit:
            return self._finish()

        if len(self._frames) >= self._max_frames:
            log.warning("utterance hit the %.0fs cap — cutting it off",
                        self._config.max_utterance_s)
            return self._finish()

        return None

    def _finish(self) -> np.ndarray | None:
        frames, self._frames = self._frames, []
        silent_run, self._silent_run = self._silent_run, 0
        self._state = _State.WAITING
        self._preroll.clear()

        # Trim the trailing silence that triggered the endpoint — only frames
        # that were actually silent, so the max-length path (which ends on
        # live speech) loses nothing. Feeding silence to Whisper adds latency
        # and invites hallucinated trailing words.
        trim = max(0, silent_run - 1)
        if trim:
            frames = frames[:-trim]

        # Too little actual speech (the preroll doesn't count) is a cough or a
        # door slam, not an utterance worth transcribing.
        if len(frames) - self._lead < self._min_frames:
            log.debug("ignoring %d-frame blip", len(frames) - self._lead)
            return None

        return pcm_to_float(b"".join(frames))

    def reset(self) -> None:
        """Drop all state. Used when a turn is abandoned mid-capture."""
        self._state = _State.WAITING
        self._frames = []
        self._silent_run = 0
        self._lead = 0
        self._preroll.clear()


__all__ = ["Endpointer"]
