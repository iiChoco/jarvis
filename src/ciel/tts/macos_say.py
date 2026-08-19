"""Text-to-speech via the macOS ``say`` command.

Chosen as the default because it needs no downloads, no model files, and no
network — Ciel talks the moment you run it. It sounds like a 2010 GPS unit, and
Piper is the upgrade path, but "works immediately" is worth a lot in a
prototype.

``say`` cannot write to a pipe (it fails with -54 on any non-seekable target),
so each call synthesizes to a temporary WAV and streams it back. Since the
brain hands us one sentence at a time, that cost overlaps with the model
generating the next sentence — only the first sentence's latency is ever
visible to the user.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import wave
from pathlib import Path
from typing import AsyncIterator

from ciel.config import TTSConfig
from ciel.tts.normalize import speakable

log = logging.getLogger(__name__)

# `say` renders at the voice's native rate regardless of what we request, so we
# pin the container format and read the true rate back out of the WAV header.
_REQUESTED_RATE = 22_050
_READ_CHUNK = 4096


class SayTTS:
    """macOS ``say`` behind the :class:`~ciel.tts.base.TextToSpeech` protocol."""

    def __init__(self, config: TTSConfig) -> None:
        self._config = config
        self._sample_rate = _REQUESTED_RATE
        self._tmpdir: Path | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def warm_up(self) -> None:
        """Confirm ``say`` exists and learn the voice's real sample rate.

        Worth doing eagerly: discovering a bad voice name mid-conversation
        means Ciel goes silent with no explanation.
        """
        if shutil.which("say") is None:
            raise RuntimeError("`say` not found — this engine is macOS only")

        self._tmpdir = Path(tempfile.mkdtemp(prefix="ciel-tts-"))
        path = await self._synthesize("Ready.")
        try:
            with wave.open(str(path)) as w:
                self._sample_rate = w.getframerate()
        finally:
            path.unlink(missing_ok=True)
        log.info("say ready (voice=%s, %d Hz)", self._config.voice, self._sample_rate)

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        # Same normalization as Piper: `say` mangles vowel-free interjections
        # differently but no better, and the two engines should not sound like
        # they are reading different scripts.
        text = speakable(text.strip())
        if not text:
            return

        path = await self._synthesize(text)
        try:
            with wave.open(str(path)) as w:
                self._sample_rate = w.getframerate()
                while True:
                    frames = w.readframes(_READ_CHUNK)
                    if not frames:
                        break
                    yield frames
                    # Yield control so a barge-in can be noticed between
                    # chunks rather than after the whole file is queued.
                    await asyncio.sleep(0)
        finally:
            path.unlink(missing_ok=True)

    async def _synthesize(self, text: str) -> Path:
        if self._tmpdir is None:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="ciel-tts-"))

        # Unique per call: sentences overlap when one is playing while the next
        # is being synthesized, and a shared path would corrupt both.
        fd, name = tempfile.mkstemp(suffix=".wav", dir=self._tmpdir)
        os.close(fd)  # we only wanted the unique name; `say` opens it itself
        path = Path(name)

        cmd = [
            "say",
            "-o", str(path),
            "--data-format=LEI16@%d" % _REQUESTED_RATE,
            "-r", str(self._config.rate),
        ]
        if self._config.voice:
            cmd += ["-v", self._config.voice]
        # `--` so text starting with a hyphen isn't parsed as a flag.
        cmd += ["--", text]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await proc.communicate()
        except BaseException:
            # Cancelled (a barge-in closing the stream mid-synthesis) or
            # errored: kill the `say` process and drop its temp file rather
            # than orphaning a subprocess and leaking the WAV until shutdown.
            # BaseException, not Exception, because CancelledError is the case
            # that matters and it is not an Exception.
            try:
                proc.kill()
                await proc.wait()
            except BaseException:  # noqa: BLE001 - already tearing down; best effort
                pass
            path.unlink(missing_ok=True)
            raise
        if proc.returncode != 0:
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"say failed ({proc.returncode}): {stderr.decode(errors='replace').strip()}"
            )
        return path

    async def close(self) -> None:
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


__all__ = ["SayTTS"]
