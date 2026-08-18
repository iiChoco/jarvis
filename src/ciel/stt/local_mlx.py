"""Local speech-to-text via mlx-whisper.

Same Whisper weights as faster-whisper, different runtime: MLX executes on
Apple Silicon's GPU through Metal, which is exactly the backend CTranslate2
lacks. Still entirely on-device — no API key, no network after the one-time
model download, no per-minute cost.

mlx-whisper keeps the loaded model in a module-level holder keyed by repo
name, so there is no model object to own here; the first ``transcribe`` call
loads (and on first run downloads) the weights, and every later call reuses
them. ``warm_up`` exists to pay that cost — plus Metal kernel compilation —
at startup instead of inside the user's first sentence.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from ciel.config import SAMPLE_RATE, STTConfig
from ciel.stt.hallucinations import looks_hallucinated, normalize

log = logging.getLogger(__name__)


class MlxWhisperSTT:
    """mlx-whisper behind the :class:`~ciel.stt.base.SpeechToText` protocol."""

    def __init__(self, config: STTConfig) -> None:
        self._config = config
        self._warmed = False
        self._lock = asyncio.Lock()
        # Whisper sometimes parrots its own `initial_prompt` back as the
        # transcript when handed silence or noise. Normalized once here so the
        # per-utterance check stays a set membership test.
        self._echoes = frozenset(
            e for e in {normalize(config.initial_prompt or "")} if e
        )

    async def warm_up(self) -> None:
        """Run one throwaway inference to load the model and compile kernels.

        Loading is several seconds on first run (it may download the model
        from Hugging Face); the first inference also pays a one-off cost as
        MLX compiles its Metal kernels. One dummy decode covers both.
        """
        if self._warmed:
            return

        # Import checked here, not at module top: a missing install should
        # fail at startup where build_stt can fall back, not mid-conversation.
        import mlx_whisper  # noqa: F401

        started = time.monotonic()
        log.info("loading whisper %s (mlx)...", self._config.mlx_model)
        await asyncio.to_thread(
            self._transcribe_sync, np.zeros(SAMPLE_RATE, dtype=np.float32)
        )
        self._warmed = True
        log.info("whisper ready (load + warm-up %.1fs)", time.monotonic() - started)

    async def transcribe(self, pcm: np.ndarray) -> str:
        if not self._warmed:
            await self.warm_up()

        # One decode at a time — concurrent calls would contend for the same
        # GPU and make both slower than running them back to back.
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
        import mlx_whisper

        result = mlx_whisper.transcribe(
            pcm,
            path_or_hf_repo=self._config.mlx_model,
            language=self._config.language,
            initial_prompt=self._config.initial_prompt or None,
            condition_on_previous_text=False,  # stops one bad decode cascading
            # No vad_filter here: mlx-whisper has none, and the endpointer has
            # already trimmed silence anyway. beam_size is likewise a
            # faster-whisper knob — MLX decodes greedily with temperature
            # fallback, which matches our beam_size=1 setting.
            verbose=None,
        )
        return str(result["text"]).strip()

    async def close(self) -> None:
        # The weights live in mlx_whisper's module-level cache, not on this
        # object; they are reclaimed at process exit. Nothing to release here.
        self._warmed = False


__all__ = ["MlxWhisperSTT"]
