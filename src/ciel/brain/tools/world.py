"""The world tool — the turn's opening block, re-read on demand.

Every turn already opens with the world's rendering (``world.py``); this
tool exists for the moment that block is old — a long turn, a deep
pass, a question that turns out to hinge on the time or the place. It
reads the same table, so it can never disagree with the block, only be
newer. Read-only, so it sits on the Witness list.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import tool

if TYPE_CHECKING:
    from ciel.world import World

log = logging.getLogger(__name__)

_world: "World | None" = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "world_now",
    (
        "The system's current readings, all in one: the time and date, "
        "whether the user is around, where they are, whether the speakers "
        "are muted, the armed timers, background watches, the rest of "
        "today's calendar, and the ring's numbers — the same block that "
        "opens each turn, read again now. Use it when the turn has run long "
        "enough that the opening block may be stale, or before answering "
        "anything that hinges on the time, the place, or what is armed. "
        "Each reading says how old it is; a reading that must be fresher "
        "has its own tool (where_am_i, oura_summary, list_timers)."
    ),
    {},
)
async def world_now(args: dict[str, Any]) -> dict[str, Any]:
    if _world is None:
        return _text("The world state is not available right now.")
    return _text(_world.render())


def bind_world(world: "World") -> None:
    """Attach the table this tool reads."""
    global _world
    _world = world


WORLD_TOOLS = [world_now]

__all__ = ["WORLD_TOOLS", "bind_world", "world_now"]
