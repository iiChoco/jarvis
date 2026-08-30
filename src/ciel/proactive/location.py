"""Vigil's location source — moves between known places, noted.

Polls the locator and watches the *place* change: arriving somewhere
named, leaving it for somewhere unnamed. Each transition is one
importance-1 event — news, never an interruption — so "you got to the
office at ten past nine" rides into the next conversation rather than
being announced to an empty room (or, worse, to a room you just walked
into). The first reading after startup sets the baseline silently:
"you're at home" the moment Ciel wakes is not news.

Same contract as the other watchers (start/kick/close, a health streak),
and the locator it polls is the same object the where_am_i tool reads,
so the tool's answer is never older than the last poll.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable

from ciel.location import Fix, Locator
from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.watchers import WatcherHealth, poll_loop
from ciel.timers import spoken_clock

if TYPE_CHECKING:
    from ciel.config import LocationConfig

log = logging.getLogger(__name__)

_NOTE_LIFETIME_S = 12 * 3600.0
"""A move is worth mentioning for a working day; after that the next
conversation has newer news."""


def transition_event(
    previous: str | None,
    current: str | None,
    fix: Fix,
    now: float,
    next_id: Callable[[], str],
) -> ProactiveEvent | None:
    """A place change → one note, or None when nothing changed. Unnamed to
    unnamed is not a change: the laptop hopping between two coffee-shop
    networks is not news."""
    if previous == current:
        return None
    if current is not None:
        summary = f"You arrived at {current} around {spoken_clock(now)}."
    elif previous is not None:
        summary = f"You left {previous} around {spoken_clock(now)}."
    else:
        return None
    return ProactiveEvent(
        id=next_id(),
        source="location",
        importance=1,
        created_at=now,
        expires_at=now + _NOTE_LIFETIME_S,
        summary=summary,
        dedupe_key=f"loc:{previous or '-'}>{current or '-'}:{int(now) // 600}",
        payload={"source": fix.source, "place": current or ""},
    )


class LocationWatcher:
    """Polls the locator and notes moves between configured places."""

    def __init__(self, config: "LocationConfig", locator: Locator, queue: EventQueue) -> None:
        self._config = config
        self._locator = locator
        self._queue = queue
        self._task: asyncio.Task[None] | None = None
        self._kick = asyncio.Event()
        self._health = WatcherHealth("location", queue)
        self._place: str | None = None
        self._baselined = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    def kick(self) -> None:
        """Re-read now — the pipeline detected a wake from sleep, which
        for a laptop is exactly when the place may have changed."""
        self._kick.set()

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _poll(self) -> None:
        async def tick() -> None:
            try:
                fix = await asyncio.to_thread(self._locator.refresh)
                if fix is not None:
                    self.observe(fix, time.time())
                self._health.succeeded()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed read must not kill the watcher
                log.exception("location read failed")
                self._health.failed(time.time())

        await poll_loop(self._kick, self._config.poll_s, tick)

    def observe(self, fix: Fix, now: float) -> ProactiveEvent | None:
        """Fold one fix into the place state; queue a note on a change.
        Public so the probe can drive moves without a poll loop."""
        place = self._locator.place_of(fix)
        if not self._baselined:
            self._baselined = True
            self._place = place
            log.info("location baseline: %s", place or "no known place")
            return None
        event = transition_event(self._place, place, fix, now, self._queue.next_id)
        self._place = place
        if event is not None and self._queue.push(event):
            log.info("location note queued: %s", event.summary)
            return event
        return None


__all__ = ["LocationWatcher", "transition_event"]
