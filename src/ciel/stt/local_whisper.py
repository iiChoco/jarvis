"""Local speech-to-text via faster-whisper.

Runs entirely on-device: no API key, no network, no per-minute cost. The model
is CPU-only here because faster-whisper is built on CTranslate2, which has no
Metal backend — the GPU path on Apple Silicon is the mlx-whisper engine in
``local_mlx.py``. This one stays as the fallback that works everywhere.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from ciel.config import SAMPLE_RATE, STTConfig
from ciel.stt.hallucinations import looks_hallucinated, normalize

log = logging.getLogger(__name__)


class WhisperSTT:
    """faster-whisper behind the :class:`~ciel.stt.base.SpeechToText` protocol."""

    def __init__(self, config: STTConfig) -> None:
        self._config = config
        self._model = None
        self._lock = asyncio.Lock()
        # Whisper sometimes parrots its own `initial_prompt` back as the
        # transcript when handed silence or noise. Normalized once here so the
        # per-utterance check stays a set membership test.
        self._echoes = frozenset(
            e for e in {normalize(config.initial_prompt or "")} if e
        )

    async def warm_up(self) -> None:
        """Load the model and run one throwaway inference.

        Both halves matter. Loading is several seconds on first run (it may
        download the model); the first *inference* also pays a one-off cost as
        CTranslate2 allocates its workspace. Paying both at startup keeps them
        out of the user's first sentence.
        """
        if self._model is not None:
            return

        from faster_whisper import WhisperModel

        started = time.monotonic()
        log.info("loading whisper %s (%s)...", self._config.model, self._config.compute_type)
        self._model = await asyncio.to_thread(
            WhisperModel,
            self._config.model,
            device=self._config.device,
            compute_type=self._config.compute_type,
        )
        loaded = time.monotonic()

        await asyncio.to_thread(self._transcribe_sync, np.zeros(SAMPLE_RATE, dtype=np.float32))
        log.info(
            "whisper ready (load %.1fs, warm-up %.1fs)",
            loaded - started,
            time.monotonic() - loaded,
        )

    async def transcribe(self, pcm: np.ndarray) -> str:
        if self._model is None:
            await self.warm_up()

        # One decode at a time. Two concurrent calls would contend for the same
        # CPU threads and make both slower than running them back to back.
        async with self._lock:
            started = time.monotonic()
            text = await asyncio.to_thread(self._transcribe_sync, pcm)

        elapsed = time.monotonic() - started
        audio_s = len(pcm) / SAMPLE_RATE
        log.debug("transcribed %.2fs of audio in %.2fs (%.1fx)",
                  audio_s, elapsed, audio_s / elapsed if elapsed else 0)

        if looks_hallucinated(text, self._echoes):
            log.debug("discarding likely hallucination: %r", text)
            return ""
        return text

    def _transcribe_sync(self, pcm: np.ndarray) -> str:
        assert self._model is not None
        segments, _info = self._model.transcribe(
            pcm,
            language=self._config.language,
            beam_size=self._config.beam_size,
            initial_prompt=self._config.initial_prompt or None,
            # The endpointer has already trimmed silence, so Whisper's own VAD
            # would only re-litigate that decision — and it sometimes discards
            # short valid utterances entirely.
            vad_filter=False,
            condition_on_previous_text=False,  # stops one bad decode cascading
            # Two fallback temperatures, not the default six-step ladder —
            # each step is a full re-decode, and the worst case (six decodes
            # of a noisy utterance) is all spent while the user waits. See
            # the mlx engine for the long version.
            temperature=[0.0, 0.4],
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def close(self) -> None:
        self._model = None


__all__ = ["WhisperSTT"]
