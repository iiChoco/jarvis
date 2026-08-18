"""Timer tools — the brain's side of the timer service.

The model converts speech to structure ("ten minutes" → 600, "seven" →
07:00) and, for reminders, phrases the label as the sentence Ciel will
eventually say. The service does the arithmetic and the remembering; the
pipeline does the eventual speaking.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from claude_agent_sdk import tool

from ciel.timers import TimerService, spoken_clock

log = logging.getLogger(__name__)

# Bound at startup, same pattern as the memory store: the SDK's @tool
# decorator wants plain module-level functions.
_service: TimerService | None = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def _describe(timer) -> str:
    remaining = timer.due_at - time.time()
    what = f"{timer.kind} {timer.id}"
    if timer.label:
        what += f" ({timer.label!r})"
    if timer.kind == "alarm":
        return f"{what} — fires at {spoken_clock(timer.due_at)}"
    return f"{what} — fires in {max(0, round(remaining / 60, 1))} minutes"


@tool(
    "set_timer",
    (
        "Set a timer or alarm that makes you speak when it fires — for 'set "
        "a ten minute timer', 'wake me at seven', 'remind me in an hour to "
        "stretch'.\n\n"
        "Pass `seconds` for a duration ('ten minutes' -> 600), or `at` as "
        "24-hour local HH:MM for a clock time ('seven in the morning' -> "
        "'07:00') — one or the other, not both. `label` is optional and is "
        "EXACTLY what will be spoken aloud when it fires, so phrase it as a "
        "complete spoken sentence ('The rice is done.'); leave it empty for "
        "a plain timer and the announcement names the duration instead. "
        "Timers survive restarts. Confirm briefly once set — say when it "
        "will fire, don't read back an id. A duration's countdown starts "
        "when you finish speaking this turn's reply, not at this call — so "
        "telling the user it's starting now is accurate."
    ),
    {"seconds": float, "at": str, "label": str},
)
async def set_timer(args: dict[str, Any]) -> dict[str, Any]:
    if _service is None:
        return _text("Timers are not available right now.")

    seconds, at = args.get("seconds"), (args.get("at") or "").strip()
    label = str(args.get("label") or "")

    if at:
        try:
            hour, minute = (int(p) for p in at.split(":"))
            assert 0 <= hour <= 23 and 0 <= minute <= 59
        except (ValueError, AssertionError):
            return _text(f"Could not parse {at!r} — pass 24-hour HH:MM, like 07:00.")
        timer = _service.set_at(hour, minute, label)
    elif seconds is not None:
        try:
            seconds = float(seconds)
            assert seconds > 0
        except (TypeError, ValueError, AssertionError):
            return _text("`seconds` must be a positive number.")
        timer = _service.set_relative(seconds, label)
    else:
        return _text("Pass either `seconds` (duration) or `at` (HH:MM).")

    log.info("set %s %s (%r)", timer.kind, timer.id, timer.label)
    return _text(f"Set: {_describe(timer)}.")


@tool(
    "cancel_timer",
    (
        "Cancel a running timer or alarm. Pass `which` as the timer's id or "
        "a fragment of its label; with only one timer running you can pass "
        "nothing. If it reports an ambiguity, call list_timers and ask the "
        "user which one they meant."
    ),
    {"which": str},
)
async def cancel_timer(args: dict[str, Any]) -> dict[str, Any]:
    if _service is None:
        return _text("Timers are not available right now.")

    active = _service.active()
    if not active:
        return _text("No timers are running.")
    cancelled = _service.cancel(str(args.get("which") or ""))
    if cancelled is None:
        listing = "; ".join(_describe(t) for t in active)
        return _text(f"That didn't match exactly one timer. Running: {listing}.")
    log.info("cancelled %s %s (%r)", cancelled.kind, cancelled.id, cancelled.label)
    return _text(f"Cancelled: {_describe(cancelled)}.")


@tool(
    "list_timers",
    "List the timers and alarms currently running, with what remains on each.",
    {},
)
async def list_timers(args: dict[str, Any]) -> dict[str, Any]:
    if _service is None:
        return _text("Timers are not available right now.")

    active = _service.active()
    if not active:
        return _text("No timers are running.")
    return _text("\n".join(_describe(t) for t in active))


def bind_timers(service: TimerService) -> None:
    """Attach the service these tools drive."""
    global _service
    _service = service


TIMER_TOOLS = [set_timer, cancel_timer, list_timers]

__all__ = ["TIMER_TOOLS", "bind_timers", "set_timer", "cancel_timer", "list_timers"]
