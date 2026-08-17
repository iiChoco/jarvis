"""Microphone capture.

PortAudio delivers audio on its own high-priority thread and will not wait for
us. Everything here exists to get frames off that thread and into asyncio
without ever blocking it: the callback does a bounded, non-blocking handoff and
returns immediately. If the consumer falls behind, we drop the *oldest* audio
and count it, because dropping stale audio is recoverable and stalling the
callback is not — a blocked callback produces glitches in the input stream
itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from types import TracebackType
from typing import AsyncIterator, Self

import numpy as np
import sounddevice as sd

from ciel.config import CHANNELS, FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE, AudioConfig

log = logging.getLogger(__name__)


def pcm_to_float(frame: bytes) -> np.ndarray:
    """int16 PCM bytes to float32 in [-1, 1], the form every model wants."""
    return np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0


def float_to_pcm(samples: np.ndarray) -> bytes:
    """Inverse of :func:`pcm_to_float`, with clipping to avoid wraparound."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


class MicStream:
    """An async iterator of fixed-size PCM frames from the microphone.

    Used as an async context manager::

        async with MicStream(cfg) as mic:
            async for frame in mic.frames():
                ...
    """

    def __init__(self, config: AudioConfig, max_queued_frames: int = 100) -> None:
        self._config = config
        self._queue: deque[bytes] = deque(maxlen=max_queued_frames)
        self._stream: sd.RawInputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._dropped = 0
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._closed = False

        device = _resolve_device(self._config.input_device, want_input=True)

        # blocksize is pinned to our frame size so the callback hands us
        # exactly one webrtcvad-sized frame at a time — no repacking, no
        # partial frames to buffer across callbacks.
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            device=device,
            channels=CHANNELS,
            dtype="int16",
            callback=self._on_audio,
        )
        self._stream.start()
        log.debug(
            "microphone open (device=%s, %d Hz, %d-sample frames)",
            device if device is not None else "default",
            SAMPLE_RATE,
            FRAME_SAMPLES,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        self._closed = True
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._wakeup is not None:
            self._wakeup.set()
        if self._dropped:
            log.warning("dropped %d audio frames — consumer fell behind", self._dropped)

    # ── PortAudio callback (runs on PortAudio's thread, not the loop) ────────

    def _on_audio(self, indata, frames: int, time_info, status) -> None:  # noqa: ANN001
        if status:
            # Overflows here mean the OS-level buffer wrapped. Worth knowing
            # about, never worth raising from an audio callback.
            log.debug("portaudio status: %s", status)

        if self._queue.maxlen and len(self._queue) == self._queue.maxlen:
            self._dropped += 1  # deque discards the oldest for us

        # bytes(indata) copies out of PortAudio's buffer, which it reuses the
        # moment this returns. Referencing it later would read torn audio.
        self._queue.append(bytes(indata))

        loop, wakeup = self._loop, self._wakeup
        if loop is not None and wakeup is not None and not wakeup.is_set():
            loop.call_soon_threadsafe(wakeup.set)

    # ── consumption ──────────────────────────────────────────────────────────

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield 30 ms int16 PCM frames until the stream is closed."""
        assert self._wakeup is not None, "MicStream used outside its context manager"
        while not self._closed:
            while self._queue:
                yield self._queue.popleft()
            self._wakeup.clear()
            if self._queue:  # raced with the callback between pop and clear
                continue
            await self._wakeup.wait()

    def drain(self) -> None:
        """Discard buffered audio.

        Called after Ciel finishes speaking so that its own voice — captured
        while the speakers were live — isn't waiting in the queue to be
        transcribed as if the user had said it.
        """
        self._queue.clear()


def _resolve_device(spec: int | str | None, *, want_input: bool) -> int | None:
    """Turn a device index or name fragment into a PortAudio index.

    Accepting a substring matters in practice: device indices shuffle when you
    plug in headphones, but "MacBook Pro Microphone" keeps meaning the same
    thing.
    """
    if spec is None or isinstance(spec, int):
        return spec

    needle = spec.lower()
    key = "max_input_channels" if want_input else "max_output_channels"
    for index, device in enumerate(sd.query_devices()):
        if device[key] > 0 and needle in device["name"].lower():
            return index
    raise ValueError(
        f"no {'input' if want_input else 'output'} device matching {spec!r}; "
        f"available: {[d['name'] for d in sd.query_devices() if d[key] > 0]}"
    )


__all__ = ["MicStream", "float_to_pcm", "pcm_to_float", "FRAME_BYTES"]
