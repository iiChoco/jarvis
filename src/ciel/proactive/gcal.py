"""Vigil's Google Calendar source — for calendars that never touch macOS.

The EventKit watcher reads whatever Calendar.app syncs; on a machine where
Google Calendar *is* the calendar and no macOS account mirrors it, EventKit
sees an empty store and nudges never fire. This watcher goes to the source
instead, piggybacking on the auth the ``[mcp.gcal]`` connector already
established: the same OAuth client keys, and the refresh token its server
saved to disk. Nothing new to authorize — if the brain can read the
calendar, so can the watcher.

Read-only by construction: the only endpoints touched are Google's token
refresh and the events/calendarList GETs, over stdlib urllib on a worker
thread. Access tokens live in memory only; this module never writes the
connector's token file — that file belongs to the MCP server, and two
writers of one token file is how refresh races start. If the connector
re-authorizes, the next refresh here picks the new token up from disk.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any, Callable

from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.watchers import WatcherHealth, poll_loop
from ciel.timers import spoken_clock, spoken_duration

if TYPE_CHECKING:
    from ciel.config import ProactiveConfig

log = logging.getLogger(__name__)

_API = "https://www.googleapis.com/calendar/v3"
_TOKEN_SKEW_S = 60.0
"""Refresh this early. An access token that expires mid-request fails the
scan for nothing; a minute of slack costs one refresh per hour at most."""

_HTTP_TIMEOUT_S = 15.0


def _iso(ts: float) -> str:
    """Epoch → RFC3339 UTC, the shape Google's timeMin/timeMax want."""
    return (
        datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_start(start: dict[str, Any]) -> tuple[float, bool]:
    """A Google event start → ``(epoch, all_day)``.

    Timed events carry ``dateTime`` with an offset; all-day events carry a
    bare ``date``, anchored here to local midnight — the person's day, not
    UTC's, same reasoning as alarms.
    """
    if "dateTime" in start:
        return datetime.datetime.fromisoformat(start["dateTime"]).timestamp(), False
    day = datetime.date.fromisoformat(start["date"])
    return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)), True


def nudges_from_items(
    items: list[dict[str, Any]],
    now: float,
    lead_s: float,
    next_id: Callable[[], str],
) -> list[ProactiveEvent]:
    """Google API event items → nudges inside the lead horizon. Pure, so
    the probe can drive every shape Google sends without a network."""
    nudges: list[ProactiveEvent] = []
    for item in items:
        if item.get("status") == "cancelled":
            continue
        try:
            start_ts, all_day = _parse_start(item.get("start") or {})
        except (KeyError, ValueError, TypeError):
            continue  # a shape we don't recognize is not worth a crash
        if all_day or start_ts <= now:
            continue
        # Push everything the scan can see that starts in the future; the
        # queue holds each nudge until its moment (start minus the lead),
        # so delivery lands at "ten minutes before" exactly rather than
        # quantized to whenever a scan happened to run.
        deliver_after = max(now, start_ts - lead_s)
        remaining = start_ts - deliver_after
        title = (item.get("summary") or "").strip() or "A calendar event"
        nudges.append(ProactiveEvent(
            id=next_id(),
            source="calendar",
            importance=2,
            created_at=now,
            expires_at=start_ts,
            summary=(
                f"{title} starts at {spoken_clock(start_ts)}, about "
                f"{spoken_duration(remaining, plural=True)} from now."
            ),
            dedupe_key=f"gcal:{item.get('id', 'unknown')}:{int(start_ts)}",
            deliver_after=deliver_after,
        ))
    return nudges


class GoogleCalendarWatcher:
    """Polls Google Calendar directly and nudges the queue — the same
    contract as the EventKit watcher (start/kick/close, agenda_today for
    the brief), different source of truth."""

    def __init__(self, config: "ProactiveConfig", queue: EventQueue) -> None:
        self._config = config
        self._queue = queue
        self._task: asyncio.Task[None] | None = None
        self._kick = asyncio.Event()
        self._health = WatcherHealth("calendar", queue)
        self._warned_unavailable = False
        self._token_lock = threading.Lock()
        """The scan and agenda threads share the cached token; the lock
        keeps a refresh from racing itself (two concurrent refreshes are
        harmless to Google but one clobbers the other's expiry)."""
        self._access_token: str | None = None
        self._token_expiry = 0.0
        self._calendar_ids: list[str] | None = None  # resolved once, cached

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
        async def tick() -> None:
            try:
                found = await asyncio.to_thread(self._scan_sync)
                for event in found:
                    if self._queue.push(event):
                        log.info("calendar nudge queued: %s", event.summary)
                self._health.succeeded()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed scan must not kill the watcher
                log.exception("google calendar scan failed")
                self._health.failed(time.time())

        await poll_loop(self._kick, self._config.calendar_poll_s, tick)

    # ── the brief's agenda ───────────────────────────────────────────────────

    async def agenda_today(self) -> list[str]:
        """Today's remaining events as spoken-ready lines — the same
        degrade-to-empty contract as the EventKit watcher."""
        try:
            return await asyncio.to_thread(self._agenda_sync)
        except Exception:  # noqa: BLE001 - a failed agenda must not kill the brief
            log.exception("google agenda fetch failed")
            return []

    def _agenda_sync(self) -> list[str]:
        now = time.time()
        local = time.localtime(now)
        midnight = time.mktime(
            (local.tm_year, local.tm_mon, local.tm_mday + 1, 0, 0, 0,
             local.tm_wday, local.tm_yday, -1)
        )
        lines: list[tuple[float, str]] = []
        for calendar_id in self._calendars():
            for item in self._events_window(calendar_id, now, midnight):
                if item.get("status") == "cancelled":
                    continue
                try:
                    start_ts, all_day = _parse_start(item.get("start") or {})
                except (KeyError, ValueError, TypeError):
                    continue
                title = (item.get("summary") or "").strip() or "An untitled event"
                if all_day:
                    lines.append((0.0, f"All day — {title}"))
                else:
                    lines.append((start_ts, f"{spoken_clock(start_ts)} — {title}"))
        return [line for _ts, line in sorted(lines, key=lambda pair: pair[0])]

    # ── Google plumbing, all on worker threads ───────────────────────────────

    def _scan_sync(self) -> list[ProactiveEvent]:
        now = time.time()
        lead_s = self._config.calendar_lead_minutes * 60
        window_end = now + lead_s + self._config.calendar_poll_s + 30
        items: list[dict[str, Any]] = []
        for calendar_id in self._calendars():
            items.extend(self._events_window(calendar_id, now, window_end))
        return nudges_from_items(items, now, lead_s, self._queue.next_id)

    def _events_window(
        self, calendar_id: str, start: float, end: float
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "timeMin": _iso(start),
            "timeMax": _iso(end),
            "singleEvents": "true",  # recurring events arrive expanded
            "orderBy": "startTime",
            "maxResults": "50",
        })
        encoded = urllib.parse.quote(calendar_id, safe="@")
        payload = self._get(f"{_API}/calendars/{encoded}/events?{query}")
        return payload.get("items") or []

    def _calendars(self) -> list[str]:
        """Calendar ids to scan: ``primary`` unless config names calendars,
        which are resolved against calendarList by title once and cached."""
        if not self._config.calendars:
            return ["primary"]
        if self._calendar_ids is None:
            wanted = {name.strip().lower() for name in self._config.calendars}
            listing = self._get(f"{_API}/users/me/calendarList").get("items") or []
            self._calendar_ids = [
                entry["id"]
                for entry in listing
                if str(entry.get("summary", "")).strip().lower() in wanted
            ]
            if len(self._calendar_ids) < len(wanted):
                log.info(
                    "watching %d of %d configured calendars (matched by "
                    "title, case-insensitively)",
                    len(self._calendar_ids), len(wanted),
                )
        return self._calendar_ids

    def _get(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token()}"}
        )
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))

    def _token(self) -> str:
        """A live access token, refreshed from the connector's saved
        refresh token when the cached one is stale."""
        with self._token_lock:
            if (
                self._access_token is not None
                and time.time() < self._token_expiry - _TOKEN_SKEW_S
            ):
                return self._access_token

            try:
                keys = json.loads(
                    self._config.google_oauth_keys.read_text()
                )["installed"]
                saved = json.loads(self._config.google_token_file.read_text())
                # @cocal/google-calendar-mcp nests its tokens under an
                # account label ("normal"); take the first entry that has a
                # refresh token so a layout change degrades gracefully.
                refresh = next(
                    entry["refresh_token"]
                    for entry in saved.values()
                    if isinstance(entry, dict) and entry.get("refresh_token")
                )
            except (OSError, json.JSONDecodeError, KeyError, StopIteration) as exc:
                if not self._warned_unavailable:
                    self._warned_unavailable = True
                    log.warning(
                        "google calendar auth is unavailable (%s) — the "
                        "watcher is idle; authorize the [mcp.gcal] "
                        "connector once and it picks the tokens up", exc,
                    )
                raise RuntimeError("google calendar auth unavailable") from exc

            body = urllib.parse.urlencode({
                "client_id": keys["client_id"],
                "client_secret": keys["client_secret"],
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            }).encode("ascii")
            request = urllib.request.Request(
                keys.get("token_uri", "https://oauth2.googleapis.com/token"),
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
                granted = json.loads(response.read().decode("utf-8"))
            self._access_token = granted["access_token"]
            self._token_expiry = time.time() + float(granted.get("expires_in", 3600))
            self._warned_unavailable = False
            return self._access_token


__all__ = ["GoogleCalendarWatcher", "nudges_from_items"]
