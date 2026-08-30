"""The lane contract — the queue-and-send surface every remote lane speaks.

This Protocol is the package docstring's "small queue-and-send surface"
made checkable. Discord and the web GUI already implement it by
convention (their docstrings each say "built hub-shaped on purpose");
writing the shape down means the third implementation — the hub link the
endgame notes describe — conforms by type check rather than by prose.

The queue element is ``(arrival, text, channel)``: a monotonic arrival
stamp (the confirm broker's predating rule needs it), the message, and
an opaque channel the lane's ``send`` knows how to route back to —
``None`` means the lane's default destination. ``pop_batch`` coalesces
only a contiguous same-channel run, because a DM and a server mention
are different conversations whose replies must not fuse.

``can_send`` is a *right-now* fact, not a config claim: a Discord
gateway that dropped reads False even though the lane is armed, which is
what keeps the proactive policy from texting into the void.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Lane(Protocol):
    """A remote text lane: messages in through a queue, replies out
    through ``send``. Implemented by ``DiscordLink`` and ``WebLink``;
    the pipeline talks to this shape and never to a chat library."""

    @property
    def pending(self) -> bool:
        """Whether at least one message is queued."""
        ...

    def peek(self) -> tuple[float, str, Any] | None:
        """The head of the queue, unconsumed: (arrival, text, channel)."""
        ...

    def pop(self) -> tuple[float, str, Any] | None:
        """Consume and return the head of the queue."""
        ...

    def pop_batch(self) -> tuple[str, Any] | None:
        """Consume the head run of same-channel messages, joined into one
        turn's text: (text, channel)."""
        ...

    @property
    def can_send(self) -> bool:
        """Whether a send would reach anyone *right now*."""
        ...

    async def send(self, text: str, channel: Any = None) -> None:
        """Deliver a reply — to ``channel``, or the lane's default when
        None. Raises the lane's unavailable error when delivery fails,
        so the caller can rewind rather than assume."""
        ...

    async def start(self) -> None:
        """Spawn the lane's transport; never blocks the greeting."""
        ...

    async def close(self) -> None:
        """Tear the transport down."""
        ...


__all__ = ["Lane"]
