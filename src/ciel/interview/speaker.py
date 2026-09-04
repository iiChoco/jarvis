"""The interviewer's voice: piper on the hub, one WAV per sentence.

Ciel's own voice runs on the Mac, streamed chunk by chunk into a player
that can be stopped mid-word. The room has no player of its own — the
browser does — so the unit here is a whole sentence rendered to a WAV the
page can decode and queue. Same engine (``PiperTTS``, the same voice
file), a different shape at the end.

One voice, loaded once, shared by every live session; synthesis is
CPU-bound and serialized inside ``PiperTTS`` already. When piper cannot
be imported or its voice cannot be loaded, ``available`` is False and the
room tells the page to use the browser's own voice instead — a worse
interviewer, not a missing one.
"""

from __future__ import annotations

import io
import logging
import wave
from dataclasses import replace

from ciel.config import InterviewConfig, TTSConfig

log = logging.getLogger(__name__)


class Speaker:
    def __init__(self, cfg: InterviewConfig) -> None:
        self._cfg = cfg
        self._tts = None
        self.available = False
        self._failed = False

    async def warm_up(self) -> None:
        """Load the voice; a failure is one warning and browser TTS."""
        if self._tts is not None or self._failed:
            return
        try:
            from ciel.tts.piper import PiperTTS
        except ImportError as exc:
            self._failed = True
            log.warning("interview voice: piper not importable (%s) — browser voice", exc)
            return
        tts = PiperTTS(replace(TTSConfig(), engine="piper", piper_voice=self._cfg.piper_voice))
        try:
            await tts.warm_up()
        except Exception as exc:  # noqa: BLE001 - a missing voice is a fallback, not a crash
            self._failed = True
            log.warning("interview voice: piper could not load %s (%s) — browser voice",
                        self._cfg.piper_voice, exc)
            return
        self._tts = tts
        self.available = True
        log.info("interview voice: piper %s at %d Hz", self._cfg.piper_voice, tts.sample_rate)

    async def wav(self, text: str) -> bytes | None:
        """The sentence as a 16-bit mono WAV, or None when the voice is
        unavailable or the sentence produced no audio."""
        if self._tts is None:
            return None
        pcm = bytearray()
        try:
            async for chunk in self._tts.stream(text):
                pcm.extend(chunk)
        except Exception:  # noqa: BLE001 - one sentence's failure is silence, not an ended interview
            log.warning("interview voice: synthesis failed for %r", text[:60], exc_info=True)
            return None
        if not pcm:
            return None
        return pcm_to_wav(bytes(pcm), self._tts.sample_rate)


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


__all__ = ["Speaker", "pcm_to_wav"]
