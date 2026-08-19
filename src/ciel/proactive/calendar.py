"""The calendar watcher: "your two o'clock is in ten minutes."

Reads the local calendar store through EventKit — the same calendars
Calendar.app shows, whatever accounts feed them. EventKit rather than the
alternatives for the same reasons the Contacts framework won in
``messages/imessage.py``: AppleScript against Calendar.app takes seconds and
launches the app; the store's SQLite is an undocumented schema that churns
across macOS releases; MCP calendar connectors are brain-side tools a
watcher can't reach. The bindings are a separate package
(``pyobjc-framework-EventKit``), imported lazily so a missing install
degrades this watcher to a logged warning, never a crash.

First use puts up the one-time macOS calendar permission dialog. That
request runs inside the poll task, not ``start()`` — the prompt still
appears at launch, but an unanswered dialog stalls only this watcher, not
the startup gather that makes Ciel say hello.

Scans are idempotent by construction: every nudge carries a dedupe key of
event identity plus start time, and the queue remembers what it has already
delivered. A rescheduled meeting changes its start time and so legitimately
earns a fresh nudge.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING

from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.watchers import WatcherHealth
from ciel.timers import spoken_clock, spoken_duration

if TYPE_CHECKING:
    from ciel.config import ProactiveConfig

log = logging.getLogger(__name__)

_AUTH_WAIT_S = 120.0
"""How long the first scan waits on the permission dialog. Generous because
a human is walking to the screen; bounded because an abandoned dialog must
not pin the poll task forever — the next scan simply asks the status again."""


class CalendarWatcher:
    """Polls EventKit and nudges the queue about imminent events."""

    def __init__(self, config: "ProactiveConfig", queue: EventQueue) -> None:
        self._config = config
        self._queue = queue
        self._task: asyncio.Task[None] | None = None
        self._store = None  # EKEventStore, created lazily in the worker thread
        self._warned_unavailable = False
        self._health = WatcherHealth("calendar", queue)
        self._kick = asyncio.Event()
        self._store_lock = threading.Lock()
        """Guards store creation and authorization. Two worker threads reach
        EventKit — the poll's scan and the brief's agenda fetch — and both
        lazily create the store; unguarded, each could allocate one and the
        second assignment replaces an instance mid-query, or both could
        fire the one-time permission request. Queries themselves run outside
        the lock: EKEventStore is documented thread-safe; its *creation*
        and our auth bookkeeping are not."""

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    def kick(self) -> None:
        """Rescan now — the pipeline detected a wake from sleep."""
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
        while True:
            try:
                found = await asyncio.to_thread(self._scan_sync)
                for event in found:
                    if self._queue.push(event):
                        log.info("calendar nudge queued: %s", event.summary)
                self._health.succeeded()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed scan must not kill the watcher
                log.exception("calendar scan failed")
                self._health.failed(time.time())
            # An interruptible sleep: kick() ends it early. Cleared after the
            # wait so a kick that lands mid-scan still hurries the next one.
            try:
                await asyncio.wait_for(
                    self._kick.wait(), timeout=self._config.calendar_poll_s
                )
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    # ── EventKit, on a worker thread ─────────────────────────────────────────

    def _scan_sync(self) -> list[ProactiveEvent]:
        """One blocking scan: authorize if needed, fetch the near window,
        return nudges for events starting inside the lead horizon."""
        try:
            import EventKit
        except ImportError:
            if not self._warned_unavailable:
                self._warned_unavailable = True
                log.warning(
                    "EventKit bindings are not installed — the calendar "
                    "watcher is idle (uv sync should fix this)"
                )
            return []

        with self._store_lock:
            if self._store is None:
                self._store = EventKit.EKEventStore.alloc().init()
            if not self._authorized(EventKit):
                return []

        from Foundation import NSDate

        now = time.time()
        lead_s = self._config.calendar_lead_minutes * 60
        window_end = now + lead_s + self._config.calendar_poll_s + 30
        calendars = self._watched_calendars(EventKit)
        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            NSDate.dateWithTimeIntervalSince1970_(now),
            NSDate.dateWithTimeIntervalSince1970_(window_end),
            calendars,
        )
        nudges: list[ProactiveEvent] = []
        for item in self._store.eventsMatchingPredicate_(predicate) or []:
            if item.isAllDay():
                continue
            start = float(item.startDate().timeIntervalSince1970())
            if not (0 < start - now <= lead_s):
                continue
            title = str(item.title() or "").strip() or "A calendar event"
            nudges.append(ProactiveEvent(
                id=self._queue.next_id(),
                source="calendar",
                importance=2,
                created_at=now,
                # A nudge about a meeting is worthless once it has started.
                expires_at=start,
                summary=(
                    f"{title} starts at {spoken_clock(start)}, about "
                    f"{spoken_duration(start - now, plural=True)} from now."
                ),
                dedupe_key=f"cal:{item.eventIdentifier()}:{int(start)}",
            ))
        return nudges

    async def agenda_today(self) -> list[str]:
        """Today's remaining events as spoken-ready lines, for the brief.

        Blocking EventKit work on a thread, same as scans. Empty on any
        failure — the brief degrades to "nothing on the calendar" rather
        than dying, which is the watcher contract everywhere.
        """
        try:
            return await asyncio.to_thread(self._agenda_sync)
        except Exception:  # noqa: BLE001 - a failed agenda must not kill the brief
            log.exception("agenda fetch failed")
            return []

    def _agenda_sync(self) -> list[str]:
        try:
            import EventKit
        except ImportError:
            return []
        with self._store_lock:
            if self._store is None:
                self._store = EventKit.EKEventStore.alloc().init()
            if not self._authorized(EventKit):
                return []

        from Foundation import NSDate

        now = time.time()
        local = time.localtime(now)
        midnight = time.mktime(
            (local.tm_year, local.tm_mon, local.tm_mday + 1, 0, 0, 0,
             local.tm_wday, local.tm_yday, -1)
        )
        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            NSDate.dateWithTimeIntervalSince1970_(now),
            NSDate.dateWithTimeIntervalSince1970_(midnight),
            self._watched_calendars(EventKit),
        )
        lines: list[str] = []
        for item in sorted(
            self._store.eventsMatchingPredicate_(predicate) or [],
            key=lambda e: float(e.startDate().timeIntervalSince1970()),
        ):
            title = str(item.title() or "").strip() or "An untitled event"
            if item.isAllDay():
                lines.append(f"All day — {title}")
            else:
                start = float(item.startDate().timeIntervalSince1970())
                lines.append(f"{spoken_clock(start)} — {title}")
        return lines

    def _authorized(self, EventKit) -> bool:  # noqa: N803 - the module *is* the namespace
        """Check calendar access, requesting it on first use.

        Handles both authorization vocabularies: macOS 14+ has FullAccess
        and ``requestFullAccessToEventsWithCompletion_``; older systems have
        Authorized and ``requestAccessToEntityType_completion_``. The
        completion fires on a private queue; this runs on a worker thread,
        so blocking on it like a person answering a dialog is fine.
        """
        status = EventKit.EKEventStore.authorizationStatusForEntityType_(
            EventKit.EKEntityTypeEvent
        )
        granted_statuses = {
            getattr(EventKit, "EKAuthorizationStatusAuthorized", None),
            getattr(EventKit, "EKAuthorizationStatusFullAccess", None),
        } - {None}
        if status in granted_statuses:
            return True
        if status != EventKit.EKAuthorizationStatusNotDetermined:
            if not self._warned_unavailable:
                self._warned_unavailable = True
                log.warning(
                    "calendar access is denied — the calendar watcher is "
                    "idle (grant it in System Settings → Privacy → Calendars)"
                )
            return False

        answered = threading.Event()
        verdict: list[bool] = []

        def _answer(granted: bool, _error: object) -> None:
            verdict.append(bool(granted))
            answered.set()

        request_full = getattr(
            self._store, "requestFullAccessToEventsWithCompletion_", None
        )
        if request_full is not None:
            request_full(_answer)
        else:
            self._store.requestAccessToEntityType_completion_(
                EventKit.EKEntityTypeEvent, _answer
            )
        if not answered.wait(_AUTH_WAIT_S):
            log.info("calendar permission dialog still unanswered — will re-check")
            return False
        return bool(verdict and verdict[0])

    def _watched_calendars(self, EventKit):  # noqa: N803 - the module *is* the namespace
        """The EKCalendar list to scan — None (all calendars) unless config
        names specific ones. Unknown names simply match nothing, and the
        mismatch is visible in the log line here."""
        if not self._config.calendars:
            return None
        wanted = {name.strip().lower() for name in self._config.calendars}
        found = [
            cal
            for cal in self._store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
            if str(cal.title()).strip().lower() in wanted
        ]
        if len(found) < len(wanted):
            log.debug(
                "watching %d of %d configured calendars (names are matched "
                "case-insensitively against calendar titles)",
                len(found), len(wanted),
            )
        return found


__all__ = ["CalendarWatcher"]
