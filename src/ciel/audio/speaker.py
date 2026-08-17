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

def save_profile(path: Path, embeddings: list[np.ndarray]) -> None:
    """Persist enrollment embeddings plus their normalized mean."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    stacked = np.stack(embeddings)
    mean = stacked.mean(axis=0)
    mean /= np.linalg.norm(mean) or 1.0
    np.savez(path, mean=mean, embeddings=stacked)


def load_profile(path: Path) -> np.ndarray | None:
    """The enrolled mean embedding, or None if never enrolled."""
    path = path.expanduser()
    if not path.exists():
        return None
    try:
        with np.load(path) as data:
            return np.asarray(data["mean"], dtype=np.float32)
    except Exception:  # noqa: BLE001 - a corrupt profile means "not enrolled"
        log.warning("could not read the voice profile at %s", path, exc_info=True)
        return None


# ── the gate ─────────────────────────────────────────────────────────────────

class SpeakerGate:
    """Decides whether an utterance is the enrolled user speaking."""

    def __init__(self, config: VoiceConfig, encoder: SpeakerEncoder) -> None:
        self._config = config
        self._encoder = encoder
        self._profile: np.ndarray | None = None
        self._min_samples = int(SAMPLE_RATE * config.min_utterance_ms / 1000)

    @property
    def enrolled(self) -> bool:
        return self._profile is not None

    async def warm_up(self) -> None:
        self._profile = load_profile(self._config.profile)
        if self._profile is None:
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

        ``nan`` similarity marks the pass-through cases — no profile, or too
        short to judge — so a caller logging scores can tell "matched" from
        "waved through".
        """
        if self._profile is None:
            return True, float("nan")
        if len(utterance) < self._min_samples:
            return True, float("nan")
        embedding = await asyncio.to_thread(self._encoder.embed, utterance)
        similarity = float(embedding @ self._profile)
        return similarity >= self._config.threshold, similarity

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
    "load_profile",
]
