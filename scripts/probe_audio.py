#!/usr/bin/env python
"""Dev probe for the audio layer. Not part of the package.

Three modes, because the pieces fail differently and are worth isolating:

    uv run scripts/probe_audio.py vad   # endpointing, on synthetic speech
    uv run scripts/probe_audio.py hold  # Cauchy's mid-thought judgement
    uv run scripts/probe_audio.py mic   # live capture, needs mic permission

``vad`` and ``hold`` are deterministic and need no hardware, so they're the
ones to reach for when something downstream looks wrong and you want to rule
the front half out.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from ciel.audio.input import MicStream, float_to_pcm
from ciel.audio.vad import Endpointer
from ciel.config import FRAME_BYTES, FRAME_MS, SAMPLE_RATE, load_config

SILENCE = b"\x00" * FRAME_BYTES


def _synthesize(text: str) -> bytes:
    """Render speech with macOS `say` at exactly the pipeline's format."""
    out = Path(tempfile.mkdtemp()) / "probe.wav"
    subprocess.run(
        ["say", "-o", str(out), "--data-format=LEI16@16000", text],
        check=True,
        capture_output=True,
    )
    with wave.open(str(out)) as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1
        return w.readframes(w.getnframes())


def _frames(pcm: bytes):
    for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        yield pcm[i : i + FRAME_BYTES]


def probe_vad() -> int:
    cfg = load_config()
    phrases = [
        "Hello Ciel, what is the weather like today?",
        "Remember that I prefer concise answers.",
    ]

    # Silence around and between the phrases, mimicking a real capture: dead
    # air before the user speaks, a gap between two separate utterances.
    gap = SILENCE * (1200 // FRAME_MS)
    stream = gap + (gap.join(_synthesize(p) for p in phrases)) + gap

    ep = Endpointer(cfg.audio)
    utterances: list[np.ndarray] = []
    for frame in _frames(stream):
        got = ep.push(frame)
        if got is not None:
            utterances.append(got)

    print(f"input:      {len(stream) / 2 / SAMPLE_RATE:6.2f}s of audio")
    print(f"spoken:     {len(phrases)} phrases")
    print(f"detected:   {len(utterances)} utterances")
    for i, u in enumerate(utterances, 1):
        peak = float(np.abs(u).max())
        print(f"  #{i}: {len(u) / SAMPLE_RATE:5.2f}s  peak={peak:.3f}")

    ok = True
    if len(utterances) != len(phrases):
        print(f"\nFAIL: expected {len(phrases)} utterances, got {len(utterances)}")
        ok = False
    for i, u in enumerate(utterances, 1):
        if len(u) / SAMPLE_RATE < 0.5:
            print(f"\nFAIL: utterance #{i} is implausibly short")
            ok = False
        if float(np.abs(u).max()) < 0.01:
            print(f"\nFAIL: utterance #{i} is silent — preroll/trim is misaligned")
            ok = False

    print("\nPASS: endpointer segments speech correctly" if ok else "")
    return 0 if ok else 1


def probe_hold() -> int:
    """The Cauchy judgement on transcripts Whisper actually produces.

    The held cases are cut-offs observed in practice: fillers stripped by
    Whisper, pauses after content words, fragments Whisper closed with a
    period anyway. The complete cases are the sentences that must *not* pay
    the hold's extra wait — questions ending on prepositions, particles,
    the protected "I think so.".
    """
    from ciel.pipeline import trails_off

    cfg = load_config()
    cases: list[tuple[str, bool]] = [
        # Held: the transcriber signalled trailing off outright.
        ("So I was thinking...", True),
        ("Add milk and, um…", True),
        # Held: no closing punctuation — the pause was mid-sentence, and any
        # spoken "um" that caused it was stripped from the transcript.
        ("check my", True),
        ("remind me to call", True),
        ("Can you add eggs, milk,", True),
        # Held: Whisper closed a fragment with its habitual period, but the
        # last word can't end a thought.
        ("Check my.", True),
        ("I want to.", True),
        ("Um.", True),
        ("It's red and.", True),
        ("Look at the.", True),
        # Complete: real sentences, including the shapes the dangling check
        # must not catch.
        ("I think so.", False),
        ("Turn it on.", False),
        ("What are you waiting for?", False),
        ("What's the weather like today?", False),
        ("Stop!", False),
        ("Set a timer for 10.", False),
        ("That's all, thanks.", False),
        ("", False),
    ]

    ok = True
    for text, expected in cases:
        got = trails_off(text, cfg.audio)
        verdict = "hold" if got else "done"
        if got != expected:
            ok = False
            print(f"  FAIL: {text!r} -> {verdict}, expected {'hold' if expected else 'done'}")
        else:
            print(f"  ok:   {verdict}  {text!r}")

    print(f"\nPASS: {len(cases)} transcripts judged correctly" if ok else "")
    return 0 if ok else 1


async def probe_mic() -> int:
    cfg = load_config()
    print("Opening microphone. Say something, then pause.")
    print("(macOS will prompt for microphone permission on first run.)\n")

    ep = Endpointer(cfg.audio)
    captured: list[np.ndarray] = []

    async def listen() -> None:
        async with MicStream(cfg.audio) as mic:
            async for frame in mic.frames():
                utterance = ep.push(frame)
                if utterance is not None:
                    captured.append(utterance)
                    print(f"  captured {len(utterance) / SAMPLE_RATE:.2f}s")
                    if len(captured) >= 2:
                        return

    try:
        await asyncio.wait_for(listen(), timeout=30)
    except asyncio.TimeoutError:
        print("\n(timed out after 30s)")

    if not captured:
        print("\nFAIL: captured nothing. Check microphone permission and input device.")
        return 1

    out = Path("/tmp/ciel_capture.wav")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(float_to_pcm(u) for u in captured))
    print(f"\nPASS: wrote {out} — play it back to confirm it sounds like you.")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "vad"
    if mode == "vad":
        raise SystemExit(probe_vad())
    if mode == "hold":
        raise SystemExit(probe_hold())
    if mode == "mic":
        raise SystemExit(asyncio.run(probe_mic()))
    print(f"unknown mode {mode!r}; expected 'vad', 'hold', or 'mic'")
    raise SystemExit(2)
