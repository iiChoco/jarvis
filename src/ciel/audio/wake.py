"""The wake boundary — how Ciel decides it's being addressed.

Codename: **Characteristic** — the indicator function of "am I being
addressed", one frame at a time.

Detectors are *fed* frames rather than pulling their own. There is exactly one
microphone and exactly one reader of it (the pipeline), so a detector that
opened its own stream would either fail or steal audio from the endpointer.
Push also makes the hotkey and the wake word genuinely interchangeable: both
answer the same question — "has the user addressed us as of this frame?" —
even though only one of them cares about the audio.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class WakeDetector(Protocol):
    """Signals that the user wants Ciel's attention."""

    def push(self, frame: bytes) -> bool:
        """Feed one 30 ms frame; return ``True`` if Ciel was just addressed.

        Must be cheap — it runs on every frame, roughly 33 times a second.
        """
        ...

    def reset(self) -> None:
        """Clear state after a turn.

        Without this the tail of the wake phrase is still sitting in the
        detector's buffer when the turn ends, and immediately re-triggers.
        """
        ...

    async def start(self) -> None:
        ...

    async def close(self) -> None:
        ...


class HotkeyWake:
    """Wake on Enter.

    Armed externally by the pipeline's single stdin reader — a bare Enter on
    the typed lane arms it — rather than opening its own reader. There is
    exactly one reader of stdin for the same reason there is one reader of the
    microphone: two readers of one file descriptor race per line, and an
    asyncio reader also flips the descriptor non-blocking underneath a thread
    blocked in ``readline``. So this detector holds no thread and touches no
    stdin; it only carries the armed flag. Deliberately the dumbest thing that
    works — it is how the rest of the pipeline gets tested before the
    microphone is trusted, and it stays useful whenever the room is noisy.

    ``arm`` and ``push``/``reset`` all run on the event loop (the stdin task
    and the frame loop), never preempting each other, so a plain bool needs no
    lock.
    """

    def __init__(self, prompt: str = "[press Enter to talk]") -> None:
        self._prompt = prompt
        self._armed = False

    async def start(self) -> None:
        self._announce()

    def _announce(self) -> None:
        sys.stdout.write(f"\n{self._prompt} ")
        sys.stdout.flush()

    def arm(self) -> None:
        """Register a bare Enter as a wake. Called by the pipeline's stdin
        reader, which owns the one descriptor."""
        self._armed = True

    def push(self, frame: bytes) -> bool:  # noqa: ARG002 - audio is irrelevant here
        if self._armed:
            self._armed = False
            return True
        return False

    def reset(self) -> None:
        self._armed = False
        self._announce()

    async def close(self) -> None:
        return None


class AlwaysAwake:
    """No wake gate — every utterance is treated as addressed.

    Only sane alone in a quiet room. With a television on, Ciel answers it.
    """

    async def start(self) -> None:
        return None

    def push(self, frame: bytes) -> bool:  # noqa: ARG002
        return True

    def reset(self) -> None:
        return None

    async def close(self) -> None:
        return None


class OpenWakeWord:
    """Hands-free wake via openWakeWord.

    Runs under onnxruntime rather than tflite: the tflite runtime has no wheels
    for current Python on macOS, and is unreliable on Apple Silicon besides.

    v1 uses the pretrained ``hey_jarvis`` model — Ciel is *named* Ciel but
    answers to "hey jarvis" until a custom model is trained, because no
    pretrained model for "Ciel" exists and training one is a separate job.
    """

    def __init__(
        self,
        model: str = "hey_jarvis",
        threshold: float = 0.5,
        vad_threshold: float = 0.3,
    ) -> None:
        self._model_name = model
        self._threshold = threshold
        self._vad_threshold = vad_threshold
        self._model = None
        self._cooldown = 0

    async def start(self) -> None:
        import numpy as np  # noqa: F401 - imported for the side effect of failing early
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        # Idempotent, and a no-op once the ONNX files are cached locally.
        # Skipped entirely for a custom model given as a path — the registry
        # only knows the pretrained names, and the file is already on disk.
        if not Path(self._model_name).expanduser().exists():
            await asyncio.to_thread(download_models, [self._model_name])

        self._model = await asyncio.to_thread(
            Model,
            wakeword_models=[self._model_name],
            inference_framework="onnx",
            vad_threshold=self._vad_threshold,
        )
        log.info("wake word ready (%s, threshold %.2f)", self._model_name, self._threshold)

    def push(self, frame: bytes) -> bool:
        if self._model is None:
            return False

        import numpy as np

        # Suppress re-triggering on the decaying tail of a detection.
        if self._cooldown > 0:
            self._cooldown -= 1
            self._model.predict(np.frombuffer(frame, dtype=np.int16))
            return False

        scores = self._model.predict(np.frombuffer(frame, dtype=np.int16))
        for name, score in scores.items():
            if score >= self._threshold:
                log.info("wake: %s (%.2f)", name, score)
                # ~1s of frames; the phrase keeps scoring high after the peak.
                self._cooldown = 33
                return True
        return False

    def reset(self) -> None:
        if self._model is not None:
            self._model.reset()
        self._cooldown = 0

    async def close(self) -> None:
        self._model = None


def build_wake_detector(config) -> WakeDetector:  # noqa: ANN001 - WakeConfig
    """Pick a detector from config."""
    if config.mode == "hotkey":
        return HotkeyWake()
    if config.mode == "always":
        return AlwaysAwake()
    if config.mode == "wakeword":
        return OpenWakeWord(
            model=config.model,
            threshold=config.threshold,
            vad_threshold=config.vad_threshold,
        )
    raise ValueError(f"unknown wake mode {config.mode!r}")


__all__ = [
    "WakeDetector",
    "HotkeyWake",
    "AlwaysAwake",
    "OpenWakeWord",
    "build_wake_detector",
]
