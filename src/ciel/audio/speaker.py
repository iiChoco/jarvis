"""Speaker verification — Ciel answers its enrolled user and nobody else.

Codename: **IFF** — identification friend or foe.

Every captured utterance is embedded (CAM++ speaker model via sherpa-onnx,
~30 ms on CPU) and cosine-compared against the enrolled profile *before*
transcription. A stranger's command costs one embedding and is dropped
silently; it never reaches Whisper, never reaches the brain, never opens a
follow-up window.

Honesty about what this is: a speaker embedding is a filter, not
authentication. It will turn away the TV, guests, and a passing conversation;
it will not resist a recording of your voice, and a cold or a shout moves
your own score. Three design choices follow from that:

* **Fail open, loudly.** No profile enrolled, or the model missing with the
  gate enabled, means everything passes and the console says so on startup.
  An identity *filter* that silently bricks the assistant would be worse than
  the strangers it filters.
* **Short utterances pass.** Below ``min_utterance_ms`` there is not enough
  speech to judge (embeddings of half a word are noise), so brief follow-ups
  like "yes" are allowed through. They only matter inside a conversation your
  verified utterance already started.
* **Confirmations inherit the turn's trust.** The Tower Clearance answer
  window opens seconds after a verified command; it is not separately
  verified, for the same short-utterance reason.

Enroll with ``scripts/enroll_voice.py``, which also measures your
self-similarity and suggests a threshold.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from ciel.config import SAMPLE_RATE, VoiceConfig

log = logging.getLogger(__name__)

# The 3D-Speaker CAM++ model trained on VoxCeleb, published by the sherpa-onnx
# project under a stable release tag (the typo in "recongition" is theirs, and
# load-bearing). 28 MB, downloaded once. English-centric training data, but
# speaker identity transfers across language far better than content does.
_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
)


class SpeakerEncoder(Protocol):
    """Turns an utterance into a fixed-size voice embedding."""

    async def warm_up(self) -> None: ...
    def embed(self, pcm: np.ndarray) -> np.ndarray: ...
    async def close(self) -> None: ...


class SherpaEncoder:
    """CAM++ embeddings through sherpa-onnx, model fetched on first use."""

    def __init__(self, model_path: Path) -> None:
        self._model_path = model_path.expanduser()
        self._extractor = None

    async def warm_up(self) -> None:
        if not self._model_path.exists():
            log.info("downloading speaker model (~28 MB, one time)...")
            self._model_path.parent.mkdir(parents=True, exist_ok=True)
            partial = self._model_path.with_suffix(".part")
            await asyncio.to_thread(urllib.request.urlretrieve, _MODEL_URL, partial)
            partial.rename(self._model_path)

        import sherpa_onnx

        started = time.monotonic()
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(self._model_path), num_threads=1
        )
        self._extractor = await asyncio.to_thread(
            sherpa_onnx.SpeakerEmbeddingExtractor, config
        )
        log.info("speaker model ready in %.1fs", time.monotonic() - started)

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        assert self._extractor is not None, "warm_up() first"
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=pcm)
        stream.input_finished()
        embedding = np.array(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(embedding))
        return embedding / norm if norm else embedding

    async def close(self) -> None:
        self._extractor = None


# ── the profile ──────────────────────────────────────────────────────────────

def save_profile(
    path: Path,
    embeddings: list[np.ndarray],
    threshold: float | None = None,
) -> None:
    """Persist enrollment embeddings, their normalized mean, and optionally a
    calibrated threshold.

    The threshold lives with the profile because it is a *property of the
    profile*: best-match scoring over N takes has different impostor
    statistics than over 3, so every change to the takes deserves a
    recalibration, and a number in a config file silently goes stale."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    stacked = np.stack([e / (np.linalg.norm(e) or 1.0) for e in embeddings])
    mean = stacked.mean(axis=0)
    mean /= np.linalg.norm(mean) or 1.0
    extra = {} if threshold is None else {"threshold": np.float64(threshold)}
    np.savez(path, mean=mean, embeddings=stacked, **extra)


def load_takes(path: Path) -> np.ndarray | None:
    """The raw enrollment takes only — no centroid row."""
    path = path.expanduser()
    if not path.exists():
        return None
    try:
        with np.load(path) as data:
            return np.asarray(data["embeddings"], dtype=np.float32)
    except Exception:  # noqa: BLE001
        log.warning("could not read the voice profile at %s", path, exc_info=True)
        return None


def load_threshold(path: Path) -> float | None:
    """The calibrated threshold stored with the profile, if any."""
    path = path.expanduser()
    if not path.exists():
        return None
    try:
        with np.load(path) as data:
            if "threshold" in data:
                return float(data["threshold"])
    except Exception:  # noqa: BLE001
        pass
    return None


def add_to_profile(path: Path, embeddings: list[np.ndarray]) -> int:
    """Append takes to an existing profile; returns the new take count.

    Growing beats re-recording: the takes that fix inconsistency are the ones
    captured in the conditions where the gate failed — across the room, over
    the dishwasher, at 7am — and those accumulate over days, not in one
    sitting. Loads the raw takes, not ``load_profile``'s scoring matrix,
    which carries a derived centroid row that must not be re-saved as if it
    were a take. The stored calibration is dropped on purpose: it was
    measured against the old take set, and stale-but-present is worse than
    absent — recalibrate after growing."""
    existing = load_takes(path)
    combined = ([*existing, *embeddings] if existing is not None
                else list(embeddings))
    save_profile(path, combined)
    return len(combined)


def load_profile(path: Path) -> np.ndarray | None:
    """All enrolled reference embeddings as a matrix, or None if unenrolled.

    Rows are the individual takes plus their centroid; scoring takes the best
    match across rows. The centroid is what a *consistent* voice matches
    best; the takes are what catch the mornings and the moods."""
    path = path.expanduser()
    if not path.exists():
        return None
    try:
        with np.load(path) as data:
            takes = np.asarray(data["embeddings"], dtype=np.float32)
            mean = np.asarray(data["mean"], dtype=np.float32)
            return np.vstack([takes, mean[None, :]])
    except Exception:  # noqa: BLE001 - a corrupt profile means "not enrolled"
        log.warning("could not read the voice profile at %s", path, exc_info=True)
        return None


# ── the gate ─────────────────────────────────────────────────────────────────

class SpeakerGate:
    """Decides whether an utterance is the enrolled user speaking."""

    def __init__(self, config: VoiceConfig, encoder: SpeakerEncoder) -> None:
        self._config = config
        self._encoder = encoder
        self._references: np.ndarray | None = None
        self._threshold: float = config.threshold if config.threshold is not None else 0.5
        self._min_samples = int(SAMPLE_RATE * config.min_utterance_ms / 1000)
        self._last_pass: float | None = None
        self._rejected_dir = config.profile.expanduser().parent / "rejected"

    @property
    def enrolled(self) -> bool:
        return self._references is not None

    async def warm_up(self) -> None:
        self._references = load_profile(self._config.profile)
        # Config wins when the user set a number; otherwise the calibrated
        # threshold stored with the profile; 0.5 as the uncalibrated default.
        self._threshold = (
            self._config.threshold
            if self._config.threshold is not None
            else load_threshold(self._config.profile) or 0.5
        )
        if self._references is not None:
            log.info(
                "voice gate: %d takes, threshold %.2f (%s)",
                self._references.shape[0] - 1,
                self._threshold,
                "from config" if self._config.threshold is not None
                else "calibrated",
            )
        if self._references is None:
            # Fail open, loudly: see the module docstring.
            print(
                "\nvoice gate: enabled but no profile enrolled — everyone "
                "passes until you run scripts/enroll_voice.py",
                flush=True,
            )
            log.warning("voice gate enabled with no profile at %s",
                        self._config.profile)
            return
        await self._encoder.warm_up()

    async def check(self, utterance: np.ndarray) -> tuple[bool, float]:
        """(is the enrolled speaker, similarity). Unjudgeable audio passes.

        Similarity is the *best* match across the enrolled takes and their
        centroid, not the centroid alone: a voice is multimodal (morning
        voice, excited voice, across-the-room voice), and averaging modes
        produces a reference nobody actually sounds like. ``nan`` marks the
        pass-through cases — no profile, or too short to judge — so a caller
        logging scores can tell "matched" from "waved through".

        A small grace margin applies shortly after a verified pass: the
        costliest false rejection is the one mid-conversation, and the
        speaker two sentences after a verified sentence is overwhelmingly
        the same person.
        """
        if self._references is None:
            return True, float("nan")
        if len(utterance) < self._min_samples:
            return True, float("nan")

        embedding = await asyncio.to_thread(self._encoder.embed, utterance)
        scores = self._references @ embedding
        similarity = float(np.max(scores))

        # The two leniencies cover distinct failure modes (weak measurement;
        # mid-conversation continuity). When both apply, take the larger —
        # summing them is how a gate quietly drops to two-thirds of its
        # threshold and lets the room in.
        recent = (
            self._last_pass is not None
            and time.monotonic() - self._last_pass < self._config.recent_window_s
        )
        discount = max(
            self._short_discount(len(utterance)),
            self._config.recent_margin if recent else 0.0,
        )
        threshold = self._threshold - discount

        if similarity >= threshold:
            self._last_pass = time.monotonic()
            log.info("voice recognized (%.2f, take %d)",
                     similarity, int(np.argmax(scores)) + 1)
            return True, similarity

        await asyncio.to_thread(self._keep_rejected, utterance, similarity)
        return False, similarity

    def _short_discount(self, samples: int) -> float:
        """Threshold leniency that tapers with utterance length.

        A one-second embedding is a weak measurement and the same speaker
        legitimately scores lower on it; demanding full-sentence confidence
        from "what time is it" is what makes the gate feel broken. Maximal at
        the min-utterance floor, zero at ``full_confidence_s`` and beyond.
        """
        full = self._config.full_confidence_s * SAMPLE_RATE
        floor = self._min_samples
        if full <= floor:
            return 0.0
        fraction = (full - samples) / (full - floor)
        return self._config.short_discount * float(np.clip(fraction, 0.0, 1.0))

    def _keep_rejected(self, utterance: np.ndarray, similarity: float) -> None:
        """Save a rejected utterance so a false rejection can teach the gate.

        The loop that actually fixes inconsistency: rejections land as wav
        files, `enroll_voice.py --adopt` plays each one back, and the ones
        the user confirms as their own voice join the profile — the gate
        learns precisely the conditions it failed in. A ring of the last few
        keeps disk (and any stranger's audio) bounded and local.
        """
        if self._config.keep_rejected <= 0:
            return
        try:
            import wave

            self._rejected_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = self._rejected_dir / f"{stamp}-{similarity:.2f}.wav"
            pcm = (np.clip(utterance, -1.0, 1.0) * 32767).astype(np.int16)
            with wave.open(str(dest), "wb") as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(SAMPLE_RATE)
                f.writeframes(pcm.tobytes())
            kept = sorted(self._rejected_dir.glob("*.wav"))
            for old in kept[: -self._config.keep_rejected]:
                old.unlink(missing_ok=True)
        except OSError:
            log.debug("could not keep the rejected utterance", exc_info=True)

    async def close(self) -> None:
        await self._encoder.close()


def build_speaker_gate(config: VoiceConfig) -> SpeakerGate | None:
    """The configured gate, or None when disabled or uninstallable.

    A missing sherpa-onnx with the gate enabled fails open with a warning —
    the voice extra is optional, and an import error should cost the filter,
    not the assistant.
    """
    if not config.enabled:
        return None
    try:
        import sherpa_onnx  # noqa: F401 - probing the optional extra
    except ImportError:
        log.warning(
            "voice gate enabled but sherpa-onnx is not installed — "
            "run `uv sync --extra voice`; letting everyone through"
        )
        return None
    return SpeakerGate(config, SherpaEncoder(config.model))


__all__ = [
    "SpeakerGate",
    "SpeakerEncoder",
    "SherpaEncoder",
    "build_speaker_gate",
    "save_profile",
    "add_to_profile",
    "load_profile",
    "load_takes",
    "load_threshold",
]
