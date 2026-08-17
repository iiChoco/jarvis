"""Text-to-speech via Piper — the quality upgrade over macOS ``say``.

Piper is a local neural TTS: it runs on-device under onnxruntime, costs
nothing per word, and sounds markedly more human than ``say``. The price is a
one-time voice-model download (~60 MB) and a few seconds of load at startup.

Unlike ``say``, Piper synthesizes *incrementally* — it yields audio chunks as
it produces them rather than after finishing the whole utterance — so the time
to first audio is substantially lower even though total synthesis time is
similar. That difference is what the user actually perceives as responsiveness.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator

from ciel.config import TTSConfig
from ciel.tts.normalize import speakable

log = logging.getLogger(__name__)

DEFAULT_VOICE_DIR = Path.home() / ".ciel" / "piper"


class PiperTTS:
    """Piper behind the :class:`~ciel.tts.base.TextToSpeech` protocol."""

    def __init__(self, config: TTSConfig) -> None:
        self._config = config
        self._voice: Any = None
        self._sample_rate = 22_050
        self._dir = config.piper_voice_dir or DEFAULT_VOICE_DIR
        self._syn_config: Any = None
        # Synthesis is CPU-bound and runs off the event loop; one at a time so
        # two sentences can't contend for the same cores and make both slower.
        self._lock = asyncio.Lock()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def warm_up(self) -> None:
        if self._voice is not None:
            return

        from piper import PiperVoice

        name = self._config.piper_voice
        model = self._dir / f"{name}.onnx"

        if not model.exists():
            log.info("downloading piper voice %s (~60 MB, one time)...", name)
            from piper.download_voices import download_voice

            self._dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(download_voice, name, self._dir)

        started = time.monotonic()
        self._voice = await asyncio.to_thread(PiperVoice.load, str(model))
        self._syn_config = self._build_syn_config()

        # The true rate comes from the voice config, not from us — Piper voices
        # ship at different rates and the player has to match or everything is
        # pitched wrong.
        rate = getattr(getattr(self._voice, "config", None), "sample_rate", None)
        if rate:
            self._sample_rate = int(rate)

        # First synthesis pays a one-off onnxruntime warm-up. Spend it here
        # rather than inside the user's first sentence.
        await asyncio.to_thread(self._synthesize_sync, "Ready.")
        log.info(
            "piper ready (%s, %d Hz, %.1fs)",
            name,
            self._sample_rate,
            time.monotonic() - started,
        )

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        # Normalized here rather than upstream: this is the last point before
        # the words become sound, so nothing else has to know that espeak
        # spells "hmm" out loud.
        text = speakable(text.strip())
        if not text:
            return
        if self._voice is None:
            await self.warm_up()

        async with self._lock:
            # Piper's generator is synchronous and CPU-bound, so it runs in a
            # worker thread — but each chunk is handed over the moment it is
            # produced, which is what keeps time-to-first-audio at first-chunk
            # cost rather than full-sentence cost. `abandoned` is checked
            # between chunks so an interrupted sentence stops burning CPU (and
            # holding this lock) partway through.
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[object] = asyncio.Queue()
            abandoned = threading.Event()
            done = object()

            def produce() -> None:
                try:
                    assert self._voice is not None
                    for chunk in self._speak(text):
                        if abandoned.is_set():
                            break
                        if chunk.sample_rate and chunk.sample_rate != self._sample_rate:
                            self._sample_rate = int(chunk.sample_rate)
                        loop.call_soon_threadsafe(
                            queue.put_nowait, chunk.audio_int16_bytes
                        )
                except Exception as exc:  # noqa: BLE001 - surfaced to the consumer
                    loop.call_soon_threadsafe(queue.put_nowait, exc)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, done)

            worker = asyncio.create_task(asyncio.to_thread(produce))
            try:
                while True:
                    item = await queue.get()
                    if item is done:
                        break
                    if isinstance(item, Exception):
                        raise item
                    yield item  # type: ignore[misc]
            finally:
                abandoned.set()
                await worker

    def _build_syn_config(self) -> Any:
        """Piper's per-synthesis knobs, or ``None`` on an older Piper.

        ``SynthesisConfig`` arrived in a later Piper than the one this was
        first written against, and speaking rate is a nicety — a version
        without it should still talk, just at the voice's native pace.
        """
        speed = self._config.piper_speed
        if not speed or speed == 1.0:
            return None
        try:
            from piper import SynthesisConfig
        except ImportError:
            log.warning(
                "this piper build has no SynthesisConfig — piper_speed ignored"
            )
            return None
        # Inverted: length_scale stretches each phoneme, so a smaller scale is
        # a faster voice.
        return SynthesisConfig(length_scale=1.0 / speed)

    def _speak(self, text: str):
        """Synthesize, passing the rate config only when we have one."""
        assert self._voice is not None
        if self._syn_config is None:
            return self._voice.synthesize(text)
        return self._voice.synthesize(text, syn_config=self._syn_config)

    def _synthesize_sync(self, text: str) -> list[bytes]:
        assert self._voice is not None
        chunks: list[bytes] = []
        for chunk in self._speak(text):
            if chunk.sample_rate and chunk.sample_rate != self._sample_rate:
                self._sample_rate = int(chunk.sample_rate)
            chunks.append(chunk.audio_int16_bytes)
        return chunks

    async def close(self) -> None:
        self._voice = None


__all__ = ["PiperTTS", "DEFAULT_VOICE_DIR"]
