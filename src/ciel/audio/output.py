"""Speaker playback.

The design constraint here is barge-in: when the user talks over Ciel, audio
has to stop *now*, not at the end of the current sentence. So playback consumes
small chunks and checks a stop flag between each one, which bounds the worst
case stop latency to a single chunk (~50 ms) rather than a whole utterance.

The output stream is opened once and held, because opening a PortAudio stream
costs milliseconds we'd otherwise pay before every single sentence.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import aclosing
from types import TracebackType
from typing import AsyncIterator, Self

import sounddevice as sd

from ciel.audio.input import _resolve_device
from ciel.config import CHANNELS, AudioConfig

log = logging.getLogger(__name__)

# Bounds barge-in stop latency. Small enough to feel instant, large enough that
# we aren't doing syscalls constantly.
CHUNK_MS = 50


class Player:
    """Plays PCM through the speakers, interruptibly."""

    def __init__(self, config: AudioConfig, sample_rate: int) -> None:
        self._config = config
        self._sample_rate = sample_rate
        self._stream: sd.RawOutputStream | None = None
        self._stop = threading.Event()
        self._playing = threading.Event()
        # Serializes play() so two overlapping calls — the turn's own sentence
        # and, say, a confirmation prompt that fires between sentences — can't
        # interleave PCM into one stream or clear each other's stop flag. stop()
        # stays outside the lock: barge-in must be able to interrupt the play
        # that holds it.
        self._play_lock = asyncio.Lock()
        self._chunk_bytes = (sample_rate * CHUNK_MS // 1000) * 2

    @property
    def is_playing(self) -> bool:
        return self._playing.is_set()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def open(self) -> None:
        if self._stream is not None:
            return
        device = _resolve_device(self._config.output_device, want_input=False)
        self._stream = sd.RawOutputStream(
            samplerate=self._sample_rate,
            device=device,
            channels=CHANNELS,
            dtype="int16",
        )
        self._stream.start()
        log.debug("speaker open (device=%s, %d Hz)",
                  device if device is not None else "default", self._sample_rate)

    async def close(self) -> None:
        self.stop()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def stop(self) -> None:
        """Interrupt playback immediately.

        Safe to call from anywhere, including while nothing is playing. Uses a
        threading.Event rather than an asyncio one so the barge-in detector can
        trip it without needing to be on the event loop.
        """
        self._stop.set()

    async def play(self, chunks: AsyncIterator[bytes]) -> bool:
        """Play a stream of int16 PCM chunks.

        Returns ``True`` if playback ran to completion, ``False`` if it was
        interrupted *or the output device failed*. The caller uses that to
        decide whether to abandon the rest of the turn — an interrupted
        sentence means the user wants something else, so queued sentences
        behind it are stale; a device failure means nothing was heard, and
        reporting completion would leave Ciel believing it spoke into a mute
        device.
        """
        async with self._play_lock:
            if self._stream is None:
                await self.open()
            assert self._stream is not None

            self._stop.clear()
            self._playing.set()
            buffer = bytearray()
            completed = True
            device_lost = False

            try:
                # aclosing so a barge-in `break` tears the generator down now,
                # not whenever GC gets to it — a Piper stream wraps a
                # subprocess, and a leaked one lingers until finalization.
                async with aclosing(chunks) as stream:
                    async for chunk in stream:
                        if self._stop.is_set():
                            completed = False
                            break
                        buffer.extend(chunk)
                        while len(buffer) >= self._chunk_bytes:
                            if self._stop.is_set():
                                completed = False
                                break
                            piece = bytes(buffer[: self._chunk_bytes])
                            del buffer[: self._chunk_bytes]
                            await asyncio.to_thread(self._write, piece)
                        if not completed:
                            break

                    if completed and buffer:
                        await asyncio.to_thread(self._write, bytes(buffer))
            except sd.PortAudioError:
                # The device failed mid-playback (unplugged, a sample-rate
                # change). Report the sentence unspoken and drop the dead
                # stream so the next play reopens rather than writing into a
                # broken handle forever. Warning, not debug: a mute assistant
                # that thinks it is talking is exactly the kind of failure that
                # must not hide in debug logs.
                log.warning("output device failed during playback", exc_info=True)
                completed = False
                device_lost = True
                try:
                    self._stream.close()
                except Exception:  # noqa: BLE001 - already broken; best effort
                    pass
                self._stream = None
            finally:
                self._playing.clear()
                if not completed and not device_lost and self._stream is not None:
                    # Drop whatever PortAudio has already buffered, otherwise the
                    # tail of the interrupted sentence keeps playing after we've
                    # stopped feeding it.
                    try:
                        self._stream.abort()
                        self._stream.start()
                    except Exception:  # pragma: no cover - device teardown races
                        log.debug("could not abort output stream", exc_info=True)

            return completed

    def _write(self, data: bytes) -> None:
        # Deliberately lets sd.PortAudioError propagate: play() turns it into a
        # not-completed result and reopens the stream. Swallowing it here (as
        # before) made every write to a lost device look like a success, so
        # Ciel reported speaking while the room heard nothing.
        assert self._stream is not None
        self._stream.write(data)


__all__ = ["Player", "CHUNK_MS"]
