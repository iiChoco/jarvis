"""Measure a wake-word model's trigger behavior before trusting it.

    uv run python scripts/probe_wake_model.py /path/to/model.onnx

Streams synthesized speech through Ciel's actual WakeDetector — the same
frame loop, the same config — and reports trigger rates for: the wake
phrase across several voices, adversarial near-misses ("hey sail"), and
ordinary sentences that should never trigger. Synthesized voices are not a
substitute for the user's real voice, but a model that fails *this* isn't
worth a live test.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np

from ciel.audio.wake import build_wake_detector
from ciel.config import FRAME_BYTES, load_config

POSITIVE = "hey seal"
NEAR_MISSES = ["hey sail", "hey steel", "he steals", "hazel"]
NEGATIVE_SENTENCES = [
    "what time is the meeting tomorrow afternoon",
    "the sea level keeps rising every single year",
    "she sells seashells down by the seashore",
]
VOICES = ["Samantha", "Daniel", "Karen", "Alex", "Moira", "Tessa"]


def synth(text: str, voice: str, out: Path) -> bool:
    done = subprocess.run(
        ["say", "-v", voice, "--file-format=WAVE",
         "--data-format=LEI16@16000", "-o", str(out), text],
        capture_output=True,
    )
    return done.returncode == 0 and out.exists()


def frames_of(path: Path):
    with wave.open(str(path)) as f:
        pcm = f.readframes(f.getnframes())
    # lead-in/out silence so the detector sees a natural boundary
    silence = b"\x00" * FRAME_BYTES * 20
    pcm = silence + pcm + silence
    for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        yield pcm[i:i + FRAME_BYTES]


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    model_path = sys.argv[1]

    config = load_config()
    # Force wakeword mode: the probe exists to measure the wake *model*, but the
    # user's config might set mode to hotkey or always, and build_wake_detector
    # would then hand back a detector that ignores audio entirely — silently
    # measuring the wrong thing (AlwaysAwake fires on every clip, HotkeyWake on
    # none) instead of the model named on the command line.
    wake_config = replace(config.wake, mode="wakeword", model=model_path)
    detector = build_wake_detector(wake_config)
    await detector.start()

    async def triggers(path: Path) -> bool:
        detector.reset()
        fired = False
        for frame in frames_of(path):
            if detector.push(frame):
                fired = True
        return fired

    with tempfile.TemporaryDirectory(prefix="wake-probe-") as tmp:
        tmp = Path(tmp)

        print(f"\n{POSITIVE!r} (should trigger):")
        hits = 0
        for voice in VOICES:
            clip = tmp / f"pos-{voice}.wav"
            if not synth(f"{POSITIVE}.", voice, clip):
                continue
            fired = await triggers(clip)
            hits += fired
            print(f"  {voice:<10} {'TRIGGERED' if fired else 'missed'}")
        print(f"  -> {hits}/{len(VOICES)}")

        print("\nnear-misses (should NOT trigger):")
        false_hits = 0
        for text in NEAR_MISSES:
            for voice in VOICES[:3]:
                clip = tmp / f"near-{text.replace(' ', '_')}-{voice}.wav"
                if not synth(f"{text}.", voice, clip):
                    continue
                fired = await triggers(clip)
                false_hits += fired
                if fired:
                    print(f"  {voice}: {text!r} TRIGGERED (bad)")
        print(f"  -> {false_hits}/{len(NEAR_MISSES) * 3} false triggers")

        print("\nordinary speech (should NOT trigger):")
        speech_hits = 0
        for text in NEGATIVE_SENTENCES:
            for voice in VOICES[:2]:
                clip = tmp / f"neg-{hash(text) % 1000}-{voice}.wav"
                if not synth(text, voice, clip):
                    continue
                fired = await triggers(clip)
                speech_hits += fired
                if fired:
                    print(f"  {voice}: {text!r} TRIGGERED (bad)")
        print(f"  -> {speech_hits}/{len(NEGATIVE_SENTENCES) * 2} false triggers")

    await detector.close()


if __name__ == "__main__":
    asyncio.run(main())
