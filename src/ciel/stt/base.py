"""The speech-to-text boundary.

One method. Any engine that can turn 16 kHz mono PCM into a string fits here —
mlx-whisper today, faster-whisper as the CPU fallback, a cloud API someday —
without the pipeline knowing which one it's talking to.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class SpeechToText(Protocol):
    """Transcribes captured audio."""

    async def transcribe(self, pcm: np.ndarray) -> str:
        """Turn mono 16 kHz float32 audio in [-1, 1] into text.

        Returns an empty string when the audio contains no intelligible
        speech. Callers treat empty as "nothing was said" and go back to
        waiting rather than sending a blank turn to the model.
        """
        ...

    async def warm_up(self) -> None:
        """Load models and run a throwaway inference.

        Called once at startup so the first real utterance isn't billed for
        several seconds of lazy model loading — the difference between Ciel
        feeling instant and feeling broken on the very first thing you say.
        """
        ...

    async def close(self) -> None:
        ...
