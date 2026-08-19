"""Speech-to-text engines and the factory that picks one.

The factory lives here rather than in the pipeline so the dev probes can
build the configured engine without importing the whole assistant.
"""

from __future__ import annotations

import importlib.util
import logging

from ciel.config import STTConfig
from ciel.stt.base import SpeechToText

log = logging.getLogger(__name__)


def build_stt(config: STTConfig) -> SpeechToText:
    """Instantiate the configured transcription engine.

    The only place either engine is named. Swapping them is a config value,
    which is the whole point of the protocol.
    """
    if config.engine == "mlx-whisper":
        # Probed with find_spec, not imported — MlxWhisperSTT loads
        # mlx_whisper lazily, so constructing it succeeds even when the
        # package is missing and the ImportError would otherwise surface as
        # a startup crash in warm_up instead of this fallback.
        if importlib.util.find_spec("mlx_whisper") is not None:
            from ciel.stt.local_mlx import MlxWhisperSTT

            return MlxWhisperSTT(config)
        # A missing mlx install should cost GPU speed, not the whole
        # assistant — faster-whisper on CPU still works everywhere.
        log.warning("mlx-whisper is not installed — falling back to faster-whisper")

    from ciel.stt.local_whisper import WhisperSTT

    return WhisperSTT(config)


__all__ = ["SpeechToText", "build_stt"]
