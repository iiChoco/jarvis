"""The location tool — "where am I", from what this machine can see.

Read-only, so it sits on the Witness list. The pipeline binds the locator
at startup (the ``bind_watcher`` pattern); without the location layer the
tool answers unavailable, and the registry withholds it entirely when
``[location]`` is off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import tool

if TYPE_CHECKING:
    from ciel.location import Locator

log = logging.getLogger(__name__)

_locator: "Locator | None" = None

_FRESH_S = 120.0
"""A reading this recent is answered from memory; older, the sources are
read again before speaking — two minutes is inside any honest "now"."""


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "where_am_i",
    (
        "Where the user is right now, as best this machine can tell: a "
        "named place from their config when the Mac's Wi-Fi or the phone's "
        "Find My position matches one, otherwise the network name or "
        "coordinates — and how old the reading is. Use it for 'where am I', "
        "for anything that depends on place (weather, travel time, what's "
        "nearby), and before assuming the user is at home. The answer says "
        "which device it came from; repeat that if it matters."
    ),
    {},
)
async def where_am_i(args: dict[str, Any]) -> dict[str, Any]:
    if _locator is None:
        return _text("Location is not available right now.")
    try:
        fix = await asyncio.to_thread(_locator.current, _FRESH_S)
    except Exception:  # noqa: BLE001 - a broken source is a sentence, not a crash
        log.exception("location read failed")
        return _text("Could not read the location sources just now.")
    return _text(_locator.describe(fix))


def bind_locator(locator: "Locator") -> None:
    """Attach the locator this tool reads."""
    global _locator
    _locator = locator


LOCATION_TOOLS = [where_am_i]

__all__ = ["LOCATION_TOOLS", "bind_locator", "where_am_i"]
