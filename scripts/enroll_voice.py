"""Enroll, grow, and tune the IFF voice profile.

    uv run python scripts/enroll_voice.py           # fresh enrollment (8 varied takes)
    uv run python scripts/enroll_voice.py --add     # append 3 more takes
    uv run python scripts/enroll_voice.py --adopt   # review rejected clips, adopt yours
    uv run python scripts/enroll_voice.py --test    # score yourself live

Consistency comes from *variety*, not repetition: the gate scores against
your best-matching take, so the profile should contain your morning voice,
your fast voice, your across-the-room voice — not eight copies of one careful
reading. Fresh enrollment walks through varied styles; ``--add`` grows the
profile whenever you find a condition it dislikes; ``--adopt`` is the real
training loop — every rejection Ciel makes is kept as a clip, and the ones
you confirm as yourself join the profile, teaching it exactly the conditions
it failed in.

Like the probe scripts, this opens its own microphone — run it while Ciel
itself is stopped, or they will fight over the device.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from ciel.audio.input import MicStream
from ciel.audio.speaker import (
    SherpaEncoder,
    add_to_profile,
    load_profile,
    save_profile,
)
from ciel.audio.vad import Endpointer
from ciel.config import SAMPLE_RATE, load_config

# (instruction, example, minimum seconds) — the variety is the point; see the
# module docstring. The short takes matter as much as the long ones: scoring
# is best-match, and a one-second command can only match well against a
# reference that is itself short — an embedding of "what time is it" looks
# systematically different from one of a full sentence, even from the same
# mouth.
TAKES = [
    ("normally, like asking from your desk",
     "Hey jarvis, what's the weather looking like tomorrow?", 1.0),
    ("normally",
     "Add a reminder to call the dentist on Thursday afternoon.", 1.0),
    ("quickly, like you're on your way out the door",
     "Actually cancel that, I'll deal with it next week instead.", 1.0),
    ("softly, like someone's asleep nearby",
     "Read me the last email from the university, please.", 1.0),
    ("louder, from a step or two away from the microphone",
     "Hey jarvis, did anything land on my calendar this morning?", 1.0),
    ("casually, mid-thought, like talking to a friend",
     "So I was thinking we could move that meeting to Friday maybe.", 1.0),
    ("normally, first thing in the morning voice if you can fake it",
     "What time is it right now?", 0.6),
    ("briefly — just these few words",
     "What's the time?", 0.6),
    ("briefly again",
     "Yes, go ahead.", 0.6),
    ("however you actually talk to Ciel",
     "Send a quick email to myself with the grocery list.", 1.0),
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


async def record_takes(config, encoder, prompts) -> list[np.ndarray]:
    embeddings: list[np.ndarray] = []
    async with MicStream(config.audio) as mic:
        for i, (style, example, min_s) in enumerate(prompts, 1):
            print(f"\n[{i}/{len(prompts)}] Say — {style}:\n    “{example}”")
            while True:
                utterance = await capture(mic, Endpointer(config.audio))
                seconds = len(utterance) / SAMPLE_RATE
                if seconds >= min_s:
                    break
                print(f"    only {seconds:.1f}s — a little more, retrying")
            embeddings.append(encoder.embed(utterance))
            print(f"    captured {seconds:.1f}s")
    return embeddings


def report(embeddings: list[np.ndarray], config) -> None:
    if len(embeddings) < 2:
        return
    pairs = [float(a @ b) for a, b in itertools.combinations(embeddings, 2)]
    lo, hi = min(pairs), max(pairs)
    print(f"\nself-similarity across takes: {lo:.2f} to {hi:.2f}")
    if lo < 0.40:
        print(
            "warning: some takes disagree strongly — background voices, or a "
            "style so different it may be worth its own extra --add takes "
            "later. Not fatal: scoring uses your best-matching take."
        )
    suggested = max(0.35, round(min(0.5, lo) - 0.08, 2))
    print(
        f"suggested [voice] threshold: {suggested} "
        f"(currently configured: {config.voice.threshold})"
    )


async def enroll(config, encoder) -> None:
    embeddings = await record_takes(config, encoder, TAKES)
    save_profile(config.voice.profile, embeddings)
    report(embeddings, config)
    print(f"\nprofile saved to {config.voice.profile} ({len(embeddings)} takes)")


async def add(config, encoder) -> None:
    if load_profile(config.voice.profile) is None:
        print("no profile yet — run without --add first")
        sys.exit(1)
    print("Three more takes. Use the voice, distance, or sentence length "
          "the gate keeps missing.")
    prompts = [
        ("in the condition that's been failing",
         "Hey jarvis, can you check my calendar for tomorrow?", 0.6),
        ("same condition, different words",
         "What's the latest email in my inbox right now?", 0.6),
        ("same condition once more",
         "Remind me to water the plants this evening.", 0.6),
    ]
    embeddings = await record_takes(config, encoder, prompts)
    total = add_to_profile(config.voice.profile, embeddings)
    print(f"\nprofile now holds {total} takes")


async def adopt(config, encoder) -> None:
    rejected_dir = config.voice.profile.expanduser().parent / "rejected"
    clips = sorted(rejected_dir.glob("*.wav"))
    if not clips:
        print("no rejected clips waiting — nothing to adopt")
        return
    if load_profile(config.voice.profile) is None:
        print("no profile yet — run a fresh enrollment first")
        sys.exit(1)

    print(f"{len(clips)} rejected clip(s). For each: listen, then decide.")
    adopted: list[np.ndarray] = []
    for clip in clips:
        print(f"\n▶ {clip.name} (similarity at rejection: {clip.stem.split('-')[-1]})")
        subprocess.run(["afplay", str(clip)], check=False)
        answer = (await asyncio.to_thread(
            input, "  your voice? [y = adopt / n = discard / r = replay / s = skip] "
        )).strip().lower()
        while answer == "r":
            subprocess.run(["afplay", str(clip)], check=False)
            answer = (await asyncio.to_thread(input, "  y/n/s? ")).strip().lower()
        if answer == "y":
            with wave.open(str(clip)) as f:
                pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
            adopted.append(encoder.embed(pcm.astype(np.float32) / 32768.0))
            clip.unlink()
            print("  adopted")
        elif answer == "n":
            clip.unlink()
            print("  discarded")
        else:
            print("  left in place")

    if adopted:
        total = add_to_profile(config.voice.profile, adopted)
        print(f"\nprofile now holds {total} takes — the gate just learned "
              f"{len(adopted)} condition(s) it used to fail in")


async def test(config, encoder) -> None:
    references = load_profile(config.voice.profile)
    if references is None:
        print("no profile enrolled yet — run without flags first")
        sys.exit(1)
    print(f"Profile holds {len(references) - 1} takes (+centroid). "
          "Say anything; ctrl-C to stop.")
    voice = config.voice
    floor_s = voice.min_utterance_ms / 1000
    async with MicStream(config.audio) as mic:
        endpointer = Endpointer(config.audio)
        while True:
            utterance = await capture(mic, endpointer)
            seconds = len(utterance) / SAMPLE_RATE
            embedding = encoder.embed(utterance)
            sims = references @ embedding
            best, centroid = float(np.max(sims)), float(sims[-1])
            # Mirror the gate's duration taper so the verdict here matches
            # what the pipeline would decide (grace margin aside).
            fraction = 0.0
            if voice.full_confidence_s > floor_s:
                fraction = min(1.0, max(
                    0.0,
                    (voice.full_confidence_s - seconds)
                    / (voice.full_confidence_s - floor_s),
                ))
            effective = voice.threshold - voice.short_discount * fraction
            verdict = "you" if best >= effective else "NOT you"
            print(f"  {seconds:.1f}s, best {best:.2f} (centroid {centroid:.2f}, "
                  f"take #{int(np.argmax(sims)) + 1}) → {verdict} "
                  f"(effective threshold {effective:.2f})")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--add", action="store_true",
                      help="append takes to the existing profile")
    mode.add_argument("--adopt", action="store_true",
                      help="review rejected clips and adopt the ones that are you")
    mode.add_argument("--test", action="store_true",
                      help="score live utterances against the saved profile")
    args = parser.parse_args()

    config = load_config()
    encoder = SherpaEncoder(config.voice.model)
    print("loading speaker model...")
    await encoder.warm_up()

    if args.test:
        await test(config, encoder)
    elif args.add:
        await add(config, encoder)
    elif args.adopt:
        await adopt(config, encoder)
    else:
        await enroll(config, encoder)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
