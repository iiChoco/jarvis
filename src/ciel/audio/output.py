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
import time
from contextlib import aclosing
from types import TracebackType
from typing import AsyncIterator, Callable, Self

import sounddevice as sd

from ciel.audio.input import _resolve_device
from ciel.config import CHANNELS, AudioConfig

log = logging.getLogger(__name__)

# Bounds barge-in stop latency. Small enough to feel instant, large enough that
# we aren't doing syscalls constantly.
CHUNK_MS = 50


class Player:
    """Plays PCM through the speakers, interruptibly."""

    def __init__(
        self,
        config: AudioConfig,
        sample_rate: int,
        muted: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config
        self._sample_rate = sample_rate
        self._muted = muted
        """Consulted at the top of every play(). This is the mute switch's
        one choke point on purpose: greeting, wake ack, timer ring,
        sentences, apologies — every sound in the system funnels through
        play(), so gating here silences all of it without any call site
        knowing mute exists. A callable, not a flag, because the state
        lives in the pipeline and changes while this object lives."""
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
        self._stop_at: float | None = None
        """When stop() last fired (monotonic). A stop aims at the current
        play *and* any play already queued on the lock when it landed — the
        queued call would otherwise clear the flag and speak into a moment
        the user just interrupted. Each play records its own request time and
        compares; a play requested after the stop proceeds normally."""
        self._device_lost = False

    @property
    def is_playing(self) -> bool:
        return self._playing.is_set()

    @property
    def device_lost(self) -> bool:
        """Whether the most recent play() failed because the output device
        died, as opposed to being stopped by barge-in. Callers that abandon a
        turn on ``play() -> False`` read this to report the true cause — an
        unplugged speaker is not the user changing their mind, and logging it
        as an interruption sends whoever reads the transcript to the wrong
        place. Valid until the next play() begins (plays are serialized)."""
        return self._device_lost

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
        trip it without needing to be on the event loop. Also stamps the stop
        time, so a play() already queued on the lock when this fires is covered
        too instead of clearing the flag and speaking anyway.
        """
        self._stop_at = time.monotonic()
        self._stop.set()

    async def play(self, chunks: AsyncIterator[bytes]) -> bool:
        """Play a stream of int16 PCM chunks.

        Returns ``True`` if playback ran to completion, ``False`` if it was
        stopped or the output device failed — ``device_lost`` distinguishes
        the two. The caller uses the result to decide whether to abandon the
        rest of the turn: an interrupted sentence means the user wants
        something else, so queued sentences behind it are stale; a device
        failure means nothing was heard, and reporting completion would leave
        Ciel believing it spoke into a mute device.

        While muted, the stream is closed unconsumed and the play reports
        *completion*: the caller's turn must proceed exactly as if the
        sentence had been spoken — the text already went to the console,
        the transcript, and the GUI, which are where a muted room reads
        it. Reporting False instead would make every muted sentence look
        like a barge-in and abandon the turn mid-answer.
        """
        if self._muted is not None and self._muted():
            # aclosing for the same reason as below: a Piper stream wraps
            # a subprocess, and silence must not leak one.
            async with aclosing(chunks):
                pass
            return True
        requested_at = time.monotonic()
        async with self._play_lock:
            self._device_lost = False
            if self._stop_at is not None and self._stop_at >= requested_at:
                # A stop landed while this play was waiting on the lock. The
                # stop was aimed at everything in flight at that moment, this
                # play included — speaking now would talk over whatever the
                # user interrupted for.
                return False
            if self._stream is None:
                await self.open()
            assert self._stream is not None

            self._stop.clear()
            self._playing.set()
            buffer = bytearray()
            completed = True

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
                self._device_lost = True
                try:
                    self._stream.close()
                except Exception:  # noqa: BLE001 - already broken; best effort
                    pass
                self._stream = None
            finally:
                self._playing.clear()
                if not completed and not self._device_lost and self._stream is not None:
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
