"""Enroll, grow, and tune the IFF voice profile.

    uv run python scripts/enroll_voice.py             # fresh enrollment, varied takes
    uv run python scripts/enroll_voice.py --add       # append 3 more takes
    uv run python scripts/enroll_voice.py --adopt     # review rejected clips, adopt yours
    uv run python scripts/enroll_voice.py --calibrate # re-measure the threshold
    uv run python scripts/enroll_voice.py --test      # score yourself live

Every profile change ends in *calibration*: a set of macOS `say` voices is
scored against your takes exactly the way the gate scores, your takes are
scored leave-one-out against each other, and the threshold is placed between
the two distributions and stored with the profile. Takes that disagree with
the rest of the profile are flagged for pruning — with best-match scoring,
every take is another chance for a lucky impostor hit, and a bad capture
pays no rent.

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
    load_takes,
    load_threshold,
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
    # No suggested threshold here anymore — calibration measures one against
    # real impostor audio and stores it with the profile.


async def enroll(config, encoder) -> None:
    embeddings = await record_takes(config, encoder, TAKES)
    save_profile(config.voice.profile, embeddings)
    report(embeddings, config)
    print(f"\nprofile saved to {config.voice.profile} ({len(embeddings)} takes)")
    await calibrate(config, encoder)


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
    await calibrate(config, encoder)


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
        await calibrate(config, encoder)


async def test(config, encoder) -> None:
    references = load_profile(config.voice.profile)
    if references is None:
        print("no profile enrolled yet — run without flags first")
        sys.exit(1)
    print(f"Profile holds {len(references) - 1} takes (+centroid). "
          "Say anything; ctrl-C to stop.")
    voice = config.voice
    # Same resolution order as the gate: explicit config, else calibrated.
    base = (voice.threshold if voice.threshold is not None
            else load_threshold(voice.profile) or 0.5)
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
            effective = base - voice.short_discount * fraction
            verdict = "you" if best >= effective else "NOT you"
            print(f"  {seconds:.1f}s, best {best:.2f} (centroid {centroid:.2f}, "
                  f"take #{int(np.argmax(sims)) + 1}) → {verdict} "
                  f"(effective threshold {effective:.2f})")


# ── calibration ──────────────────────────────────────────────────────────────
# The threshold is measured, not guessed: macOS ships a crowd of `say` voices
# that make free impostors. Each is scored against the profile exactly the
# way the gate scores (best match across takes + centroid), the takes are
# scored leave-one-out against each other, and the bar goes between the two
# distributions. Synthetic voices are imperfect impostors — but an impostor
# floor measured on ten of them beats a constant chosen on none of them.

_IMPOSTOR_SENTENCES = [
    "Hey jarvis, what's on my calendar for tomorrow afternoon?",
    "Could you read me the most recent email in my inbox?",
]
_PREFERRED_VOICES = [
    "Samantha", "Daniel", "Karen", "Moira", "Rishi", "Tessa",
    "Alex", "Fred", "Victoria", "Fiona",
]


def _available_say_voices() -> list[str]:
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)
    available = set()
    for line in out.stdout.splitlines():
        m = __import__("re").match(r"^(.+?)\s{2,}[a-z]{2}[_-]", line)
        if m:
            available.add(m.group(1).strip())
    picked = [v for v in _PREFERRED_VOICES if v in available]
    return picked or sorted(available)[:8]


def _impostor_scores(encoder, references: np.ndarray) -> dict[str, float]:
    """Best-match score per impostor voice, exactly as the gate would score."""
    import tempfile

    scores: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="ciel-calibrate-") as tmp:
        for voice in _available_say_voices()[:10]:
            best = 0.0
            for i, sentence in enumerate(_IMPOSTOR_SENTENCES):
                clip = Path(tmp) / f"{voice}-{i}.wav"
                done = subprocess.run(
                    ["say", "-v", voice, "--file-format=WAVE",
                     "--data-format=LEI16@16000", "-o", str(clip), sentence],
                    capture_output=True,
                )
                if done.returncode != 0 or not clip.exists():
                    continue
                with wave.open(str(clip)) as f:
                    pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
                emb = encoder.embed(pcm.astype(np.float32) / 32768.0)
                best = max(best, float(np.max(references @ emb)))
            if best > 0.0:
                scores[voice] = best
    return scores


def _loo_similarities(takes: np.ndarray) -> np.ndarray:
    """Each take's best match against the *other* takes."""
    sims = takes @ takes.T
    np.fill_diagonal(sims, -1.0)
    return sims.max(axis=1)


async def calibrate(config, encoder, *, prune: bool = True) -> None:
    takes = load_takes(config.voice.profile)
    if takes is None or len(takes) < 2:
        print("calibration needs an enrolled profile with at least 2 takes")
        return

    references = load_profile(config.voice.profile)
    print(f"\ncalibrating against {len(takes)} takes...")
    impostors = await asyncio.to_thread(_impostor_scores, encoder, references)
    if not impostors:
        print("no say voices available — keeping the current threshold")
        return
    loo = _loo_similarities(takes)

    worst_impostor = max(impostors.values())
    worst_name = max(impostors, key=impostors.get)
    print(f"impostor best-match scores ({len(impostors)} voices): "
          f"{min(impostors.values()):.2f} to {worst_impostor:.2f} "
          f"(worst: {worst_name})")
    print(f"your takes, leave-one-out: {loo.min():.2f} to {loo.max():.2f}")

    if prune:
        # A take that barely agrees with the rest of the profile is a
        # liability, not coverage: every reference is another chance for a
        # lucky impostor hit through the best-match max, and a bad capture
        # (or a mistaken --adopt) pays no rent for that risk.
        flagged = [i for i in range(len(takes)) if loo[i] < 0.45]
        if flagged:
            print("\ntakes that disagree with the rest of your profile "
                  "(possible bad captures or mistaken --adopt):")
            for i in flagged:
                print(f"  take #{i + 1}: best agreement with others {loo[i]:.2f}")
            answer = (await asyncio.to_thread(
                input, "drop these takes? [y/N] ")).strip().lower()
            if answer == "y":
                keep = [t for i, t in enumerate(takes) if i not in set(flagged)]
                if len(keep) >= 2:
                    takes = np.stack(keep)
                    save_profile(config.voice.profile, list(takes))
                    references = load_profile(config.voice.profile)
                    impostors = await asyncio.to_thread(
                        _impostor_scores, encoder, references)
                    loo = _loo_similarities(takes)
                    worst_impostor = max(impostors.values())
                    print(f"re-measured: impostors up to {worst_impostor:.2f}, "
                          f"takes {loo.min():.2f} to {loo.max():.2f}")
                else:
                    print("would leave fewer than 2 takes — keeping all")

    genuine_floor = float(loo.min())
    if genuine_floor > worst_impostor:
        threshold = round(
            min(0.70, max(0.42, (worst_impostor + genuine_floor) / 2)), 2)
        print(f"\nclear gap — calibrated threshold: {threshold}")
    else:
        threshold = round(min(0.70, worst_impostor + 0.03), 2)
        print(
            f"\nwarning: your weakest take ({genuine_floor:.2f}) scores below "
            f"the best impostor ({worst_impostor:.2f}). Setting the threshold "
            f"just above the impostors ({threshold}); expect that weak take's "
            "conditions to bounce — re-record it with --add, or prune it."
        )

    save_profile(config.voice.profile, list(takes), threshold=threshold)
    print(f"stored with the profile ({config.voice.profile.name}); "
          "[voice] threshold in config stays unset and inherits it")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--add", action="store_true",
                      help="append takes to the existing profile")
    mode.add_argument("--adopt", action="store_true",
                      help="review rejected clips and adopt the ones that are you")
    mode.add_argument("--test", action="store_true",
                      help="score live utterances against the saved profile")
    mode.add_argument("--calibrate", action="store_true",
                      help="re-measure the threshold against impostor voices")
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
    elif args.calibrate:
        await calibrate(config, encoder)
    else:
        await enroll(config, encoder)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
