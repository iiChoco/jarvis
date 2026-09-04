"""Vigil's Oura source — the ring's verdicts, noticed before you mention them.

Two moments in a day are worth one unprompted sentence from a ring:

* **The morning.** Once the night syncs, Oura scores the sleep and the
  readiness. Either in its low band is the ring saying "take it easy
  today"; both low is a rough night. One nudge per day covers whichever
  applies — importance 2, so the policy may voice it or hold it for the
  first conversation, never more than once (the dedupe key is the date;
  there is no morning-hours window, so a first scan late in the day still
  files it). A fine morning produces nothing.
* **The evening.** From a configured hour, an activity score still in its
  low band means a still day — "a walk would fix it". Importance 1: news
  for the next conversation, never an interruption.

Same contract as the calendar watchers (start/kick/close), same plumbing:
urllib on a worker thread via the shared client, one ``WatcherHealth``
streak so a lost authorization surfaces as a note instead of an eternal
log line. Read-only by construction, like everything that touches Oura.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from ciel.oura import (
    OuraClient,
    OuraUnavailable,
    build_auth,
    by_day,
    hours_minutes,
    main_sleep_by_day,
    today_readings,
    weakest_contributor,
)
from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.policy import parse_hhmm
from ciel.proactive.watchers import WatcherHealth, poll_loop

if TYPE_CHECKING:
    from ciel.config import OuraConfig

log = logging.getLogger(__name__)


def _local_day(now: float) -> tuple[str, float]:
    """Today's ISO date and local midnight-after, the nudges' expiry — a
    "go easy today" at midnight is tomorrow's problem."""
    local = time.localtime(now)
    midnight = time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday + 1, 0, 0, 0,
         local.tm_wday, local.tm_yday, -1)
    )
    return time.strftime("%Y-%m-%d", local), midnight


def _score(record: dict[str, Any] | None) -> int | None:
    score = (record or {}).get("score")
    return int(score) if isinstance(score, (int, float)) else None


def morning_nudge(
    readiness: list[dict[str, Any]],
    sleep: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    now: float,
    low_readiness: int,
    low_sleep: int,
    next_id: Callable[[], str],
) -> ProactiveEvent | None:
    """Today's readiness and sleep → one nudge when either is at or under
    its threshold, else None. A threshold of 0 switches that check off.
    Pure, so the probe can drive every shape Oura sends."""
    today, midnight = _local_day(now)
    ready = by_day(readiness).get(today)
    slept = by_day(sleep).get(today)
    session = main_sleep_by_day(sessions).get(today)
    r_score, s_score = _score(ready), _score(slept)
    r_low = low_readiness > 0 and r_score is not None and r_score <= low_readiness
    s_low = low_sleep > 0 and s_score is not None and s_score <= low_sleep
    if not (r_low or s_low):
        return None

    sleep_clause = ""
    if s_score is not None:
        sleep_clause = f"sleep score {s_score}"
        duration = (session or {}).get("total_sleep_duration")
        if isinstance(duration, (int, float)) and duration > 0:
            sleep_clause += f", {hours_minutes(float(duration))} asleep"
    reason = ""
    weakest = weakest_contributor(ready) if ready else None
    if weakest is not None:
        reason = f" The weakest contributor is {weakest[0]}, at {weakest[1]}."

    if r_low and s_low:
        summary = (
            f"Rough night by the ring: {sleep_clause}; readiness {r_score}, "
            f"under the {low_readiness} mark — worth going easy today.{reason}"
        )
    elif r_low:
        summary = (
            f"Oura readiness is {r_score} today, at or under the {low_readiness} "
            f"mark — the ring suggests going easy.{reason}"
        )
    else:
        held = f" — readiness held up at {r_score}" if r_score is not None else ""
        summary = f"Short night by the ring: {sleep_clause}, under the {low_sleep} mark{held}."
    return ProactiveEvent(
        id=next_id(),
        source="oura",
        importance=2,
        created_at=now,
        expires_at=midnight,
        summary=summary,
        dedupe_key=f"oura:morning:{today}",
    )


def activity_nudge(
    activity: list[dict[str, Any]],
    now: float,
    low_activity: int,
    check_after_minutes: int | None,
    next_id: Callable[[], str],
) -> ProactiveEvent | None:
    """Today's activity → a note once the check hour has passed and the
    score is still at or under the threshold, else None."""
    if low_activity <= 0 or check_after_minutes is None:
        return None
    local = time.localtime(now)
    if local.tm_hour * 60 + local.tm_min < check_after_minutes:
        return None
    today, midnight = _local_day(now)
    record = by_day(activity).get(today)
    score = _score(record)
    if score is None or score > low_activity:
        return None
    steps = (record or {}).get("steps")
    steps_clause = f", {int(steps)} steps" if isinstance(steps, (int, float)) else ""
    return ProactiveEvent(
        id=next_id(),
        source="oura",
        importance=1,
        created_at=now,
        expires_at=midnight,
        summary=(
            f"The ring says it's been a still day: activity score {score} so far"
            f"{steps_clause} — a walk would fix it."
        ),
        dedupe_key=f"oura:activity:{today}",
    )


class OuraWatcher:
    """Polls today's ring summaries and nudges the queue on low scores."""

    def __init__(
        self, config: "OuraConfig", queue: EventQueue, world: Any | None = None
    ) -> None:
        self._config = config
        self._queue = queue
        self._world = world
        """The world table (``world.py``): every scan leaves the day's
        numbers there, so "how did I sleep" can be answered from the
        opening block of a turn without a fetch."""
        auth = build_auth(config)
        self._client: OuraClient | None = OuraClient(auth) if auth is not None else None
        self._task: asyncio.Task[None] | None = None
        self._kick = asyncio.Event()
        self._health = WatcherHealth("oura", queue)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    def kick(self) -> None:
        """Rescan now — the pipeline detected a wake from sleep (the
        machine's; the ring syncs while the lid is shut, so the score is
        often already there)."""
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
                events = await asyncio.to_thread(self._scan_sync)
                for event in events:
                    if self._queue.push(event):
                        log.info("oura nudge queued: %s", event.summary)
                self._health.succeeded()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed scan must not kill the watcher
                log.exception("oura scan failed")
                self._health.failed(time.time())

        await poll_loop(self._kick, self._config.poll_s, tick)

    def _scan_sync(self) -> list[ProactiveEvent]:
        if self._client is None:
            raise OuraUnavailable("the ring is not authorized")
        cfg = self._config
        now = time.time()
        today = datetime.date.fromtimestamp(now)
        events: list[ProactiveEvent] = []
        readiness: list[dict[str, Any]] = []
        sleep: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        activity: list[dict[str, Any]] = []
        if cfg.low_readiness > 0 or cfg.low_sleep > 0:
            readiness = self._client.daily_readiness(today, today) if cfg.low_readiness > 0 else []
            sleep = self._client.daily_sleep(today, today) if cfg.low_sleep > 0 else []
            sessions = self._client.sleep_sessions(today, today) if cfg.low_sleep > 0 else []
            event = morning_nudge(
                readiness, sleep, sessions, now, cfg.low_readiness, cfg.low_sleep,
                self._queue.next_id,
            )
            if event is not None:
                events.append(event)
        check_after = parse_hhmm(cfg.activity_check_after)
        if cfg.low_activity > 0 and check_after is not None:
            local = time.localtime(now)
            if local.tm_hour * 60 + local.tm_min >= check_after:
                activity = self._client.daily_activity(today, today)
                event = activity_nudge(
                    activity, now, cfg.low_activity, check_after, self._queue.next_id
                )
                if event is not None:
                    events.append(event)
        if self._world is not None:
            readings = today_readings(today.isoformat(), readiness, sleep, activity, sessions)
            if len(readings) > 1:  # more than the day itself
                self._world.observe("oura", readings, source="oura", observed_at=now)
        return events


__all__ = ["OuraWatcher", "activity_nudge", "morning_nudge"]
