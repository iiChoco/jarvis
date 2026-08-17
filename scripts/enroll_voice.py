"""Enroll your voice for the IFF gate, or test an existing enrollment.

    uv run python scripts/enroll_voice.py          # record 4 phrases, save profile
    uv run python scripts/enroll_voice.py --test   # score yourself against it

Enrollment records a handful of natural sentences, embeds each, checks they
agree with each other (a TV in the background or a second speaker shows up as
low pairwise similarity), and saves the profile. It also prints your
self-similarity spread and a suggested `[voice] threshold` — the gate's
default of 0.5 is sensible, but your microphone and room are the numbers
that actually matter.

Like the probe scripts, this opens its own microphone — run it while Ciel
itself is stopped, or they will fight over the device.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import sys

import numpy as np

from ciel.audio.input import MicStream
from ciel.audio.speaker import SherpaEncoder, load_profile, save_profile
from ciel.audio.vad import Endpointer
from ciel.config import SAMPLE_RATE, load_config

PHRASES = [
    "Hey jarvis, what's the weather looking like tomorrow?",
    "Add a reminder to call the dentist on Thursday afternoon.",
    "Actually, cancel that — I'll deal with it next week instead.",
    "Read me the last email from the university, please.",
]


async def capture(mic: MicStream, endpointer: Endpointer) -> np.ndarray:
    """One utterance off the mic, using the production endpointing."""
    endpointer.reset()
    mic.drain()
    async for frame in mic.frames():
        utterance = endpointer.push(frame)
        if utterance is not None:
            return utterance
    raise RuntimeError("microphone stream ended")


async def enroll(config, encoder: SherpaEncoder) -> None:
    embeddings: list[np.ndarray] = []
    async with MicStream(config.audio) as mic:
        for i, phrase in enumerate(PHRASES, 1):
            print(f"\n[{i}/{len(PHRASES)}] Say something like:\n    “{phrase}”")
            utterance = await capture(mic, Endpointer(config.audio))
            seconds = len(utterance) / SAMPLE_RATE
            if seconds < 1.0:
                print(f"    only {seconds:.1f}s of speech — say a full sentence, retrying")
                utterance = await capture(mic, Endpointer(config.audio))
            embeddings.append(encoder.embed(utterance))
            print(f"    captured {seconds:.1f}s")

    pairs = [
        float(a @ b) for a, b in itertools.combinations(embeddings, 2)
    ]
    lo, hi = min(pairs), max(pairs)
    print(f"\nself-similarity across takes: {lo:.2f} to {hi:.2f}")
    if lo < 0.45:
        print(
            "warning: the takes disagree with each other — background voices "
            "or a noisy room? A clean enrollment usually sits above 0.55. "
            "Consider re-running somewhere quieter."
        )

    save_profile(config.voice.profile, embeddings)
    print(f"profile saved to {config.voice.profile}")

    # Suggest a threshold below the observed self-similarity floor, but never
    # so low that it stops meaning anything.
    suggested = max(0.35, round(lo - 0.12, 2))
    print(
        f"suggested [voice] threshold: {suggested} "
        f"(config default is {config.voice.threshold})"
    )
    print("\nNow set in ~/.ciel/config.toml:\n\n[voice]\nenabled = true\n")


async def test(config, encoder: SherpaEncoder) -> None:
    profile = load_profile(config.voice.profile)
    if profile is None:
        print("no profile enrolled yet — run without --test first")
        sys.exit(1)
    print("Say anything; ctrl-C to stop.")
    async with MicStream(config.audio) as mic:
        endpointer = Endpointer(config.audio)
        while True:
            utterance = await capture(mic, endpointer)
            embedding = encoder.embed(utterance)
            similarity = float(embedding @ profile)
            verdict = "you" if similarity >= config.voice.threshold else "NOT you"
            print(f"  similarity {similarity:.2f} → {verdict} "
                  f"(threshold {config.voice.threshold})")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true",
                        help="score live utterances against the saved profile")
    args = parser.parse_args()

    config = load_config()
    encoder = SherpaEncoder(config.voice.model)
    print("loading speaker model...")
    await encoder.warm_up()

    if args.test:
        await test(config, encoder)
    else:
        await enroll(config, encoder)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
