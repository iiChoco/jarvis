"""Post-processing that gives the voice a place to speak from.

Piper synthesizes a voice standing nowhere: dry, close-mic'd, pasted onto
the silence. The cinematic-assistant sound — JARVIS is the reference — is
mostly not a different voice but a *treatment*: the low end rolled off the
way small installed speakers roll it off, a presence lift where consonants
live, and a handful of fast, quiet early reflections that read as "this
voice comes from somewhere in the room's architecture". This module applies
exactly that treatment to the PCM stream and nothing more — no pitch, no
formants, and no latency worth naming (the EQ is a small linear-phase FIR,
about six milliseconds of group delay).

Pure numpy on purpose. scipy happens to be in the venv today, but only as
someone else's transitive dependency, and a cosmetic effect must never be
the reason the assistant fails to import.

Streaming is the constraint that shaped the code. Chunks arrive
mid-sentence and the first one must leave as fast as it arrived — time to
first audio is what the user perceives as responsiveness — so both stages
run stateful overlap-save across chunk boundaries instead of transforming
whole utterances, and the reflection tail is flushed *after* the dry
stream ends: the room keeps ringing ~130 ms into the silence, which is
what rooms do.
"""

from __future__ import annotations

from contextlib import aclosing
from typing import AsyncIterator

import numpy as np

from ciel.tts.base import TextToSpeech

_HIGHPASS_HZ = 110.0
"""Below here the rolloff starts (~12 dB/oct). A voice with no chest boom
sounds smaller and mounted, not thinner — Piper has little energy down
there anyway, so this is a tilt, not an amputation."""

_PRESENCE_HZ = 3200.0
_PRESENCE_DB = 3.5
_PRESENCE_WIDTH_OCT = 0.65
"""A broad bell where consonants and articulation live. This is most of
the 'crisp, engineered' character; more than ~5 dB turns sibilants harsh
on laptop speakers."""

_SHELF_HZ = 10_000.0
_SHELF_DB = -1.5
"""A whisper of top-end taming — synthetic voices carry a synthetic hiss
up there that real room playback would have absorbed."""

_N_TAPS = 257
"""FIR length. Odd for linear phase; 257 at 22.05 kHz is ~12 ms of window,
enough resolution for a 110 Hz tilt while keeping group delay under 6 ms."""

_REFLECTIONS_MS = (
    (4.1, 0.16), (13.7, 0.14), (19.9, -0.12), (27.1, 0.105),
    (34.9, -0.09), (44.3, 0.078), (53.9, -0.066), (66.1, 0.055),
    (79.9, -0.046), (94.7, 0.038), (111.3, -0.031), (129.7, 0.025),
)
"""Early reflections: prime-ish spacings so no two stack into a flutter,
alternating signs so their sum doesn't comb into one metallic resonance.
Together about -12 dB against the dry voice — heard as a room, not as an
echo."""

_WET = 0.8
"""Overall reflection level. 0 is the dry voice; past ~1.5 the room stops
being architecture and starts being a bathroom."""

_OUT_GAIN = 0.9
"""Headroom before the soft limiter: the reflections add energy on top of
a voice that already peaks near full scale."""

_MAKEUP = 1.4
"""Loudness restitution, measured not guessed: the low-end tilt and the
limiter's linear region shave ~3 dB of RMS off the voice, and a treatment
that also made Ciel quieter would read as a downgrade. This brings the
treated voice back to the dry voice's measured RMS; the tanh above it
keeps the added gain from ever cracking a peak."""


def _design_eq(fs: int) -> np.ndarray:
    """The tonal tilt as one linear-phase FIR, by frequency sampling.

    Magnitude is composed on a dense grid (highpass x presence bell x top
    shelf), brought to an impulse with zero phase, centered, windowed to
    _N_TAPS, and normalized to unity at 1 kHz so the effect changes tone,
    never loudness.
    """
    n_fft = 4096
    f = np.maximum(np.fft.rfftfreq(n_fft, 1.0 / fs), 1e-6)
    mag = (f**2) / (f**2 + _HIGHPASS_HZ**2)
    db = _PRESENCE_DB * np.exp(
        -0.5 * (np.log2(f / _PRESENCE_HZ) / _PRESENCE_WIDTH_OCT) ** 2
    )
    db += _SHELF_DB / (1.0 + (_SHELF_HZ / f) ** 8)
    mag = mag * 10.0 ** (db / 20.0)

    impulse = np.fft.irfft(mag, n_fft)
    taps = np.roll(impulse, _N_TAPS // 2)[:_N_TAPS] * np.hanning(_N_TAPS)

    response = np.fft.rfft(taps, n_fft)
    at_1k = abs(response[round(1000 * n_fft / fs)])
    return (taps / at_1k).astype(np.float32)


class VoiceEffect:
    """Wraps any :class:`TextToSpeech` engine and treats its output.

    Same protocol in, same protocol out, so the pipeline and the player
    never learn the effect exists — exactly how the engines themselves are
    swapped. Filters are built per sample rate, lazily, because the true
    rate is only known once the wrapped engine has loaded its voice.
    """

    def __init__(self, engine: TextToSpeech) -> None:
        self._engine = engine
        self._fs = 0
        self._eq = np.zeros(1, dtype=np.float32)
        self._delays: np.ndarray = np.zeros(0, dtype=np.int64)
        self._gains: np.ndarray = np.zeros(0, dtype=np.float32)
        self._max_delay = 0

    @property
    def sample_rate(self) -> int:
        return self._engine.sample_rate

    async def warm_up(self) -> None:
        await self._engine.warm_up()
        self._prepare()

    async def close(self) -> None:
        await self._engine.close()

    def _prepare(self) -> None:
        fs = self._engine.sample_rate
        if fs == self._fs:
            return
        self._fs = fs
        self._eq = _design_eq(fs)
        self._delays = np.array(
            [round(ms * fs / 1000.0) for ms, _ in _REFLECTIONS_MS], dtype=np.int64
        )
        self._gains = np.array(
            [g * _WET for _, g in _REFLECTIONS_MS], dtype=np.float32
        )
        self._max_delay = int(self._delays.max())

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        self._prepare()
        eq_hist = np.zeros(len(self._eq) - 1, dtype=np.float32)
        wet_hist = np.zeros(self._max_delay, dtype=np.float32)
        leftover = b""

        async with aclosing(self._engine.stream(text)) as inner:
            async for chunk in inner:
                data = leftover + chunk
                if len(data) % 2:
                    data, leftover = data[:-1], data[-1:]
                else:
                    leftover = b""
                if not data:
                    continue
                x = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
                out, eq_hist, wet_hist = self._process(x, eq_hist, wet_hist)
                yield self._to_bytes(out)

        # The dry voice has ended; let the room ring out. Zeros pushed
        # through the same state flush the FIR and every reflection.
        tail = np.zeros(self._max_delay + len(self._eq) - 1, dtype=np.float32)
        out, _, _ = self._process(tail, eq_hist, wet_hist)
        yield self._to_bytes(out)

    def _process(
        self,
        x: np.ndarray,
        eq_hist: np.ndarray,
        wet_hist: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """One chunk through both stages, overlap-save on both histories."""
        n = len(self._eq)
        ext = np.concatenate([eq_hist, x])
        y = np.convolve(ext, self._eq)[n - 1 : n - 1 + len(x)]

        ext_y = np.concatenate([wet_hist, y])
        out = y.copy()
        for d, g in zip(self._delays, self._gains):
            out += g * ext_y[self._max_delay - d : self._max_delay - d + len(y)]

        return out, ext[-(n - 1):], ext_y[-self._max_delay:]

    @staticmethod
    def _to_bytes(out: np.ndarray) -> bytes:
        # tanh, not clip: at speech levels it is transparent (tanh(x) ~ x
        # below ~0.5) and a reflection landing on a peak folds over softly
        # instead of cracking.
        limited = np.tanh(out * _OUT_GAIN * _MAKEUP)
        return (limited * 32767.0).astype(np.int16).tobytes()


__all__ = ["VoiceEffect"]
