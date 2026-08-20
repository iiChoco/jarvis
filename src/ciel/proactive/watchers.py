"""The watcher contract: anything that pushes events into the queue.

A watcher is deliberately tiny — an async ``start()``/``close()`` pair around
whatever polling or subscribing its source needs, producing
:class:`~ciel.proactive.events.ProactiveEvent`\\ s. All judgement about
whether and how an event reaches the user lives elsewhere (the policy and
the unattended turn), so a new watcher is a new module and one line in the
pipeline, nothing more. The protocol is transport-agnostic on purpose: a
future *remote* watcher — a local adapter relaying events published by some
other device — satisfies it exactly as a local poller does.

Phase 1 ships one watcher (``ciel.proactive.calendar``); the schedule
watcher for the morning brief lands here in Phase 2.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol, runtime_checkable

from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.policy import parse_hhmm

if TYPE_CHECKING:
    from ciel.config import ProactiveConfig

log = logging.getLogger(__name__)


async def poll_loop(
    kick: asyncio.Event,
    interval_s: float,
    tick: Callable[[], Awaitable[None]],
) -> None:
    """The shared watcher heartbeat: tick, then sleep or be kicked awake.

    One copy of the subtle part (the review found it hand-rolled in three
    watchers, inviting drift): the kick is cleared *after* the wait, so a
    kick landing mid-tick still hurries the next round instead of being
    lost; a tick that raises is logged and the loop survives — a broken
    source must never take the heartbeat down with it; cancellation
    propagates, because close() relies on it.
    """
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the heartbeat must not die
            log.exception("watcher tick failed")
        try:
            await asyncio.wait_for(kick.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
        kick.clear()


@runtime_checkable
class Watcher(Protocol):
    """One event source. ``start()`` may spawn tasks; ``close()`` stops them.

    Both are best-effort from the pipeline's point of view: a watcher that
    can't start (a missing framework, a denied permission) logs and idles —
    it must never take the voice loop down with it.
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def kick(self) -> None:
        """Hurry: rescan now instead of waiting out the poll interval.

        Called when the pipeline detects a wake from sleep — the watcher's
        ``asyncio.sleep`` was paused with the process, so without this a
        nudge due right after lid-open would wait out the remainder of a
        poll interval that no longer corresponds to wall time. Optional in
        spirit: a watcher with no poll loop implements it as a no-op.
        """
        ...


class WatcherHealth:
    """Failure-streak self-report: after enough consecutive failures, stop
    logging the failure and report *the watcher*.

    A watcher that fails every scan logs the same exception forever, and log
    lines are where nobody looks. At the threshold, one importance-1 event
    enters the queue — routine, so it never interrupts; it rides a held note
    into the next conversation ("the calendar watcher has failed three scans
    in a row — worth a look"). One report per streak: the dedupe key is the
    streak's first-failure timestamp, so a watcher that stays broken doesn't
    nag, and a fresh streak after a recovery legitimately reports again.
    """

    def __init__(self, name: str, queue: EventQueue, threshold: int = 3) -> None:
        self._name = name
        self._queue = queue
        self._threshold = threshold
        self._streak = 0
        self._streak_started = 0.0

    def failed(self, now: float) -> None:
        if self._streak == 0:
            self._streak_started = now
        self._streak += 1
        if self._streak != self._threshold:
            return
        self._queue.push(ProactiveEvent(
            id=self._queue.next_id(),
            source="vigil",
            importance=1,
            created_at=now,
            expires_at=None,
            summary=(
                f"The {self._name} watcher has failed {self._threshold} "
                "times in a row — its source may be broken; the log has "
                "the details."
            ),
            dedupe_key=f"vigil:{self._name}:failing:{int(self._streak_started)}",
        ))
        log.warning(
            "%s watcher failing (streak of %d) — reported to the queue",
            self._name, self._streak,
        )

    def succeeded(self) -> None:
        self._streak = 0


_BRIEF_FRESH_S = 4 * 3600.0
"""How long past its clock time the morning brief is still worth having.
A brief is breakfast: four hours late is brunch, later than that is
tomorrow's problem — a laptop opened at dinner should not deliver a
"good morning". The window also bounds the event's expiry, so a brief the
policy held (nobody around) dies quietly at lunch instead of riding a held
note into the evening."""


class ScheduleWatcher:
    """Fires events at configured local clock times — the morning brief first.

    All wall-clock: "07:45" means 07:45 on the wall, through sleep, restarts,
    and DST, which is the same reasoning alarms use in ``timers.py``. The
    poll is a one-minute heartbeat rather than a computed long sleep because
    monotonic sleeps stretch across naps — sixty second granularity is well
    inside the meaning of "in the morning", and the check itself is a few
    clock calls. Dedupe is by local date, so the minutes after the brief's
    time re-offer nothing, and a restart can't double-brief a morning the
    queue already remembers.
    """

    def __init__(self, config: "ProactiveConfig", queue: EventQueue) -> None:
        self._config = config
        self._queue = queue
        self._task: asyncio.Task[None] | None = None
        self._kick = asyncio.Event()

    async def start(self) -> None:
        if parse_hhmm(self._config.brief_time) is None:
            if self._config.brief_time:
                log.warning(
                    "brief_time %r is not HH:MM — the morning brief is off",
                    self._config.brief_time,
                )
            return
        self._task = asyncio.create_task(self._poll())

    def kick(self) -> None:
        """Re-check now — the pipeline detected a wake from sleep."""
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
            self.check(time.time())

        await poll_loop(self._kick, 60.0, tick)

    def check(self, now: float) -> None:
        """Push the brief event if its time has come today and is still
        fresh. ``now`` injected so the probe can drive mornings at will."""
        minutes = parse_hhmm(self._config.brief_time)
        if minutes is None:
            return
        local = time.localtime(now)
        due = time.mktime(
            (local.tm_year, local.tm_mon, local.tm_mday,
             minutes // 60, minutes % 60, 0,
             local.tm_wday, local.tm_yday, -1)
        )
        if now < due or now - due > _BRIEF_FRESH_S:
            return
        day = time.strftime("%Y-%m-%d", local)
        if self._queue.push(ProactiveEvent(
            id=self._queue.next_id(),
            source="brief",
            importance=2,
            created_at=now,
            expires_at=due + _BRIEF_FRESH_S,
            summary=(
                "Morning brief: give a short good-morning rundown — "
                "today's calendar and anything noted overnight; skip "
                "whatever is empty."
            ),
            dedupe_key=f"brief:{day}",
        )):
            log.info("morning brief armed for delivery")


__all__ = ["ScheduleWatcher", "Watcher", "WatcherHealth"]
