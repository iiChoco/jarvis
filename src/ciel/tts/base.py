"""The text-to-speech boundary.

Synthesis is exposed as an async iterator of PCM chunks rather than a single
buffer. That shape is what makes barge-in possible: the player can stop pulling
chunks the instant the user talks over Ciel, instead of being committed to a
whole sentence of audio it has already generated.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class TextToSpeech(Protocol):
    """Turns text into speakable audio."""

    @property
    def sample_rate(self) -> int:
        """Output rate in Hz. Engines differ — `say` gives 22.05 kHz, Piper
        voices are usually 16 or 22.05 kHz — so the player asks rather than
        assumes."""
        ...

    def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield mono int16 PCM chunks for ``text``.

        Implementations should yield the first chunk as early as they can; the
        time to *first* chunk is what the user perceives as responsiveness, not
        the total synthesis time.

        Cancellation is the contract for stopping: when the consumer aborts
        iteration, the implementation must tear down any subprocess or stream
        it owns rather than leaking it.
        """
        ...

    async def warm_up(self) -> None:
        ...

    async def close(self) -> None:
        ...
