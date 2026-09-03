"""Vigil's section-signup source — a spot opening in a section you want.

The one fact worth interrupting for: a watched section on
sections.datastructur.es stopped being full. Spots there are taken in
minutes, so the event is importance 3 — urgent — which is what lets the
policy speak it the moment you're present and text your phone when you're
not; it expires half an hour after it was seen, because "a spot was open
at two" delivered at five is pure disappointment.

The scan is a transition detector, not a level detector: each poll
remembers every watched section's spot count, and an event fires when a
section goes full → open (or is already open on the first scan after a
start — the watch exists because the section *was* full, so finding it
open is the news the watch was armed for). A section the site says you
are already enrolled in is skipped outright: an opening there is old
news by definition.

Dedupe rides the queue, as always, and the key is bucketed by local hour
— ``sections:<id>:<YYYY-MM-DD-HH>`` — which encodes a deliberate trade.
The autoreloader restarts the process on any source edit and the watcher
rescans from scratch, so a key must survive a restart mid-opening
without re-buzzing the phone; and a section flapping open-full-open must
not burn the day's three-text budget in one lunch hour. The cost is real
but small: a genuine re-opening lands silently when it falls in the same
hour as a ping already delivered — and a second buzz about the same
section within the hour is nagging, not news.

Same contract as every watcher (start/kick/close), same plumbing: urllib
on a worker thread, one ``WatcherHealth`` streak so an expired login
cookie surfaces as a note pointing at the fix instead of an eternal log
line. Read-only by construction — Ciel says a spot opened; taking it
stays a human act (``ciel.sections`` deliberately has no join call).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable

from ciel.gmail import GmailSender
from ciel.mail import Mailer, MailUnavailable, SmtpSender
from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.watchers import WatcherHealth, poll_loop
from ciel.sections import (
    SectionsState,
    SectionsUnavailable,
    fetch_state,
    when_phrase,
)

if TYPE_CHECKING:
    from ciel.config import MailConfig, SectionsConfig

log = logging.getLogger(__name__)

_FRESH_S = 30 * 60.0
"""How long a spot-opened event stays worth delivering. Spots go in
minutes; half an hour is already generous, and past it the nudge would
more likely announce a spot that no longer exists."""


def _summary(state: SectionsState, section_id: str) -> str:
    section = next(s for s in state.sections if s.id == section_id)
    course = f"{state.course} " if state.course else ""
    spots = section.spots
    opener = (
        "A spot just opened" if spots == 1 else f"{spots} spots are open"
    )
    where = f" at {section.location}" if section.location else ""
    staff = f" with {section.staff}" if section.staff else ""
    return (
        f"{opener} in {course}{section.kind.lower()} section {section.id} — "
        f"{when_phrase(section)}{where}{staff}. Grab it on the sections "
        "site before it fills."
    )


def opening_events(
    state: SectionsState,
    watch: tuple[str, ...],
    last_spots: dict[str, int] | None,
    now: float,
    next_id: Callable[[], str],
) -> tuple[list[ProactiveEvent], dict[str, int]]:
    """One scan's verdict: the events worth pushing, and the spot counts
    the next scan compares against.

    Pure, so the probe can drive every shape — first scans, transitions,
    flaps, enrollment — without a site or a clock. ``last_spots`` is the
    previous scan's second return value; None means a first scan, where
    any watched open section is announced (the hour-bucketed key keeps a
    restart from turning that into a repeat buzz).
    """
    seen: dict[str, int] = {}
    events: list[ProactiveEvent] = []
    hour = time.strftime("%Y-%m-%d-%H", time.localtime(now))
    for section in state.sections:
        if section.id not in watch:
            continue
        seen[section.id] = section.spots
        if section.spots <= 0 or section.id in state.enrolled_ids:
            continue
        was_full = last_spots is None or last_spots.get(section.id, 0) <= 0
        if not was_full:
            continue
        events.append(ProactiveEvent(
            id=next_id(),
            source="sections",
            importance=3,
            created_at=now,
            expires_at=now + _FRESH_S,
            summary=_summary(state, section.id),
            dedupe_key=f"sections:{section.id}:{hour}",
            payload={"section_id": section.id, "spots": str(section.spots)},
        ))
    missing = [sid for sid in watch if sid not in seen]
    if missing:
        log.warning(
            "watched section id%s %s not in the site's list — a typo in "
            "[sections] watch, or the section was deleted",
            "" if len(missing) == 1 else "s", ", ".join(missing),
        )
    return events, seen


def alarm_message(event: ProactiveEvent, n: int, count: int) -> tuple[str, str]:
    """Subject and body of the n-th of ``count`` alarm emails for one
    opening. Distinct subjects on purpose — Gmail threads by subject, and
    a thread is one notification; five threads are five."""
    section = event.payload.get("section_id", "?")
    spots = event.payload.get("spots", "?")
    subject = (
        f"Spot open in section {section} — {spots} left ({n} of {count})"
    )
    body = (
        f"{event.summary}\n\n"
        f"https://sections.datastructur.es/\n\n"
        f"Alarm {n} of {count} from Ciel's section watcher."
    )
    return subject, body


def _pick_mailer(
    config: "SectionsConfig", mail: "MailConfig | None"
) -> tuple[Mailer | None, str, str]:
    """The sender, the From, and the To for the alarm.

    In order: a relay of the watcher's own (``smtp_token`` here), then
    Ciel's own address (``[mail]``, the usual case — one token, one
    address, shared), then the user's Gmail. None, with a warning, when
    nothing is usable — the alarm then simply doesn't exist and openings
    fall back to the ordinary nudge.
    """
    if config.smtp_token:
        if not (config.email_from and config.email_to):
            log.warning(
                "sections: smtp_token is set but email_from/email_to are "
                "not both pinned — openings will not email"
            )
            return None, "", ""
        return (
            SmtpSender(config.smtp_host, config.smtp_port, config.smtp_user, config.smtp_token),
            config.email_from, config.email_to,
        )
    if mail is not None and mail.armed:
        sender = config.email_from or mail.address
        to = config.email_to or mail.owner
        if not to:
            log.warning(
                "sections: [mail] is armed but neither email_to nor [mail] "
                "owner names a recipient — openings will not email"
            )
            return None, "", ""
        return (
            SmtpSender(mail.smtp_host, mail.smtp_port, mail.smtp_user, mail.token, mail.copy_to),
            sender, to,
        )
    gmail = GmailSender(config.gmail_oauth_keys, config.gmail_token_file)
    if gmail.available():
        return gmail, config.email_from, config.email_to
    log.warning(
        "sections: email_on_opening is set but no sender is usable (no "
        "smtp_token, no [mail], Gmail connector not authorized) — openings "
        "will not email"
    )
    return None, "", ""


class SectionsWatcher:
    """Polls the signup site and nudges the queue when a watched section
    stops being full."""

    def __init__(
        self,
        config: "SectionsConfig",
        queue: EventQueue,
        mail: "MailConfig | None" = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._task: asyncio.Task[None] | None = None
        self._kick = asyncio.Event()
        self._health = WatcherHealth("sections", queue)
        self._last_spots: dict[str, int] | None = None
        self._last_refresh = 0.0
        """When the self-heal refresh was last spawned — the cooldown anchor
        that keeps a dead cookie from launching a browser every poll."""
        self._mailer: Mailer | None = None
        self._alarm_from = ""
        self._alarm_to = ""
        if config.email_on_opening > 0:
            self._mailer, self._alarm_from, self._alarm_to = _pick_mailer(config, mail)
        self._alarms: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if not self._config.current_cookie() or not self._config.watch:
            log.warning(
                "sections watcher enabled but %s — it will idle",
                "no cookie is set" if not self._config.current_cookie()
                else "no sections are watched",
            )
            return
        self._task = asyncio.create_task(self._poll())

    def kick(self) -> None:
        """Rescan now — the pipeline detected a wake from sleep, and a
        spot that opened mid-nap is exactly the race this watcher exists
        to win."""
        self._kick.set()

    async def close(self) -> None:
        for alarm in list(self._alarms):
            alarm.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def _start_alarm(self, event: ProactiveEvent) -> None:
        """Email the opening ``email_on_opening`` times, spaced out, as a
        background task the poll never waits on. Rides alongside the
        queued event rather than through the policy: the policy's job is
        judging whether a moment has earned an interruption, and this
        alarm is the user's standing answer that for this one thing it
        always has."""
        if self._mailer is None:
            return
        task = asyncio.create_task(self._email_alarm(event))
        self._alarms.add(task)
        task.add_done_callback(self._alarms.discard)

    async def _email_alarm(self, event: ProactiveEvent) -> None:
        count = self._config.email_on_opening
        for n in range(1, count + 1):
            subject, body = alarm_message(event, n, count)
            try:
                await asyncio.to_thread(
                    self._mailer.send, self._alarm_to, subject, body, self._alarm_from,
                )
                log.info("sections alarm email %d/%d sent", n, count)
            except MailUnavailable as exc:
                log.warning("sections alarm email %d/%d failed: %s", n, count, exc)
            if n < count:
                await asyncio.sleep(self._config.email_interval_s)

    async def _poll(self) -> None:
        async def tick() -> None:
            now = time.time()
            try:
                events = await asyncio.to_thread(self._scan_sync)
            except asyncio.CancelledError:
                raise
            except SectionsUnavailable as exc:
                # A bad cookie is the one failure a re-login fixes; try to
                # self-heal, then still count it so a refresh that keeps
                # failing (the remember-window lapsed) surfaces the note.
                log.warning("sections scan failed: %s", exc)
                if exc.auth:
                    self._maybe_refresh(now)
                self._health.failed(now)
                return
            except Exception:  # noqa: BLE001 - a failed scan must not kill the watcher
                log.exception("sections scan failed")
                self._health.failed(now)
                return
            for event in events:
                if self._queue.push(event):
                    log.info("sections nudge queued: %s", event.summary)
                    self._start_alarm(event)
            self._health.succeeded()

        await poll_loop(self._kick, self._config.poll_s, tick)

    def _scan_sync(self) -> list[ProactiveEvent]:
        state = fetch_state(self._config.url, self._config.current_cookie())
        events, self._last_spots = opening_events(
            state, self._config.watch, self._last_spots,
            time.time(), self._queue.next_id,
        )
        return events

    def _maybe_refresh(self, now: float) -> None:
        """Spawn the cookie-refresh command, at most once per cooldown.

        Fire-and-forget onto the loop: the refresh drives a browser and
        can take seconds, and the poll must not block on it — the fresh
        cookie is picked up by a later scan (``current_cookie`` re-reads
        the file), not by this one.
        """
        if not self._config.auto_refresh or not self._config.refresh_cmd:
            return
        if now - self._last_refresh < self._config.refresh_cooldown_s:
            return
        self._last_refresh = now
        asyncio.create_task(self._run_refresh())

    async def _run_refresh(self) -> None:
        cmd = self._config.refresh_cmd
        log.info("sections: cookie signed out — running refresh")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                log.info("sections: refresh succeeded; the next scan uses the new cookie")
            else:
                tail = (out or b"").decode(errors="replace").strip()[-300:]
                log.warning("sections: refresh exited %s: %s", proc.returncode, tail)
        except Exception:  # noqa: BLE001 - a broken refresh must not kill the watcher
            log.exception("sections: refresh command could not be run")


__all__ = ["SectionsWatcher", "alarm_message", "opening_events"]
