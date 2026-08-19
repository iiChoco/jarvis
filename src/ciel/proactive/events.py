"""Vigil's ledger: events awaiting delivery, held notes, and the day's budget.

The proactive layer's one source of truth. Watchers push events in; the
pipeline pops them when the interruption policy says the moment is right;
whatever can't be spoken now is held for the start of the next conversation.
All of it — pending events, held notes, the daily budget, and the record of
what was already delivered — lives in one JSON file rewritten on every
change, for the same reason timers persist: the autoreloader re-execs the
whole process on any source edit, and a restart must not swallow a nudge,
reset the day's spoken budget, or announce the same meeting twice.

Dedupe lives here rather than in watcher memory for the same reason: a
watcher rescans the world from scratch after every restart, and the queue —
which did survive — is what knows a nudge was already delivered.

Wall-clock time throughout, like timers: an event's expiry is a moment in
the world ("the meeting starts"), not a process-relative offset.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_DELIVERED_RETENTION_S = 48 * 3600
"""How long a delivered event's dedupe key is remembered. Long enough that a
watcher re-deriving yesterday's events can't re-announce them, short enough
that the map can't grow without bound — and every real event key embeds a
timestamp, so 48 hours outlives any window a watcher rescans."""


@dataclass(frozen=True, slots=True)
class ProactiveEvent:
    """One thing the world did that might be worth telling the user about.

    Plain serializable data on purpose — an event round-trips through JSON
    persistence today and, in the endgame, through whatever transport carries
    it from a remote watcher.
    """

    id: str
    source: str
    """Which watcher produced it — "calendar", "brief", "work", "mail",
    "verify". Free-form by design: a new watcher needs no registry edit."""

    importance: int
    """1 routine (news, never interrupts), 2 timely, 3 urgent. The policy
    compares this against configured thresholds; watchers assign it."""

    created_at: float
    """Wall-clock epoch seconds, stamped by the watcher."""

    expires_at: float | None
    """When this stops being worth saying at all — a calendar nudge is
    worthless once the meeting has started. None never expires."""

    summary: str
    """One prompt-ready spoken-English line. This is what the unattended turn
    sees and what a held note delivers, so watchers phrase it as a sentence,
    not a record."""

    dedupe_key: str
    """Identity across rescans and restarts — e.g. "cal:<id>:<start>". A key
    seen in pending, held, or the delivered map is silently dropped on push,
    which is what makes watcher scans idempotent."""

    payload: dict[str, str] = field(default_factory=dict)
    """Source-specific extras (a journal id to verify against, a handle).
    Strings only, so the JSON round-trip is exact."""


class EventQueue:
    """Pending events, held notes, dedupe record, and the daily budget —
    mirrored to disk on every change (the ``TimerService`` pattern)."""

    def __init__(self, path: Path, held_max_age_s: float) -> None:
        self._path = path
        self._held_max_age_s = held_max_age_s
        self._pending: list[ProactiveEvent] = []
        self._held: list[ProactiveEvent] = []
        self._delivered: dict[str, float] = {}  # dedupe_key -> when recorded
        self._inflight: set[str] = set()
        """Keys handed to a turn via begin() and not yet resolved. Part of
        the dedupe horizon: a watcher rescan during a long turn re-derives
        the same key, and without this the second copy is accepted and
        delivered again. Deliberately not persisted — a crash mid-turn
        forgets the claim, and the cancel paths hold their event first, so
        only a hard kill can lose one."""
        self._budget_date = ""
        self._spoken = 0
        self._messaged = 0
        self._next_id = 1
        self._load()

    # ── the watchers' side ───────────────────────────────────────────────────

    def next_id(self) -> str:
        """A process-unique event id, persisted so restarts don't reuse one."""
        eid = f"e{self._next_id}"
        self._next_id += 1
        return eid

    def push(self, event: ProactiveEvent) -> bool:
        """Queue an event; returns False for a duplicate.

        A key already pending, held, or delivered is dropped silently —
        pushing is how watchers report a rescan, not how they insist.
        """
        if self._seen(event.dedupe_key):
            return False
        self._pending.append(event)
        self._save()
        return True

    # ── the pipeline's side ──────────────────────────────────────────────────

    @property
    def pending(self) -> bool:
        """Whether anything awaits a decision. The frame loop checks this at
        frame rate, so it must stay this cheap — everything else here runs
        only when it's True."""
        return bool(self._pending)

    def peek_next(self, now: float) -> ProactiveEvent | None:
        """The first live pending event, without claiming it.

        Expired events are pruned here — recorded as delivered so a watcher
        rescan can't resurrect them, logged so the drop isn't silent. The
        survivor stays in pending: the caller decides, and only an enacted
        decision claims it via :meth:`begin`. Peek-then-begin (rather than a
        destructive pop) is what lets a busy turn slot simply *wait* — no
        pop-and-push churn rewriting the file every poll, no event rotating
        to the back of the queue for being unlucky.
        """
        pruned = False
        keep: list[ProactiveEvent] = []
        head: ProactiveEvent | None = None
        for event in self._pending:
            if head is None and event.expires_at is not None and event.expires_at <= now:
                log.info("event expired undelivered: %s", event.summary)
                self._delivered[event.dedupe_key] = now
                pruned = True
                continue
            if head is None:
                head = event
            keep.append(event)
        if pruned:
            self._pending = keep
            self._save()
        return head

    def begin(self, event: ProactiveEvent) -> None:
        """Claim an event for delivery: out of pending, into the in-flight
        set. Every path out of a turn must resolve the claim — mark_spoken,
        mark_messaged, drop, or hold — or the key stays claimed until
        restart."""
        self._pending = [
            e for e in self._pending if e.dedupe_key != event.dedupe_key
        ]
        self._inflight.add(event.dedupe_key)
        self._save()

    def pop_next(self, now: float) -> ProactiveEvent | None:
        """Peek and claim in one step, for callers with no reason to wait."""
        event = self.peek_next(now)
        if event is not None:
            self.begin(event)
        return event

    def hold(self, event: ProactiveEvent) -> None:
        """Keep an event for the start of the next conversation."""
        self._inflight.discard(event.dedupe_key)
        self._held.append(event)
        self._save()

    def drop(self, event: ProactiveEvent, now: float) -> None:
        """Discard an event, remembering its key so it can't come back."""
        self._inflight.discard(event.dedupe_key)
        self._delivered[event.dedupe_key] = now
        self._save()

    def mark_spoken(self, event: ProactiveEvent, now: float, date: str) -> None:
        """Record a voiced delivery and charge the day's spoken budget.

        Called only after audio actually played — a turn that failed or
        declined must not spend the budget.
        """
        self._inflight.discard(event.dedupe_key)
        self._delivered[event.dedupe_key] = now
        self._roll_budget(date)
        self._spoken += 1
        self._save()

    def spoken_count(self, date: str) -> int:
        """Voiced deliveries so far on the given local date. The date arrives
        as an argument (never read from a clock here) so probes can drive
        the midnight rollover."""
        self._roll_budget(date)
        return self._spoken

    def mark_messaged(self, event: ProactiveEvent, now: float, date: str) -> None:
        """Record a texted delivery and charge the day's texting budget.
        Called only after the send actually succeeded — same reasoning as
        the spoken charge."""
        self._inflight.discard(event.dedupe_key)
        self._delivered[event.dedupe_key] = now
        self._roll_budget(date)
        self._messaged += 1
        self._save()

    def messaged_count(self, date: str) -> int:
        """Texted deliveries so far on the given local date."""
        self._roll_budget(date)
        return self._messaged

    def peek_held(self, now: float) -> list[ProactiveEvent]:
        """The held notes that would deliver right now, without consuming.

        For material a turn *plans* to deliver (the brief): peek to build
        the prompt, then consume with :meth:`take_held` only after delivery
        actually happened — a SKIP, a timeout, or a dead speaker must leave
        the notes held, or the brief's failure silently burns everything it
        was carrying.
        """
        return [
            event for event in self._held
            if not (event.expires_at is not None and event.expires_at <= now)
            and not (now - event.created_at > self._held_max_age_s)
        ]

    def count_source(self, source: str) -> int:
        """Unresolved events (pending + held) from one source — the cheap
        backlog check that keeps a source from flooding the queue."""
        return sum(e.source == source for e in self._pending) + sum(
            e.source == source for e in self._held
        )

    def take_held(self, now: float) -> list[ProactiveEvent]:
        """Consume the held notes worth delivering right now.

        Expired and over-age notes die here, unspoken — a stale held note
        helps no one, same reasoning as missed timers' grace window. Every
        taken or pruned note is recorded as delivered.
        """
        live: list[ProactiveEvent] = []
        for event in self._held:
            self._delivered[event.dedupe_key] = now
            expired = event.expires_at is not None and event.expires_at <= now
            stale = now - event.created_at > self._held_max_age_s
            if expired or stale:
                log.info("held note dropped (%s): %s",
                         "expired" if expired else "stale", event.summary)
                continue
            live.append(event)
        if self._held:
            self._held = []
            self._save()
        return live

    # ── bookkeeping ──────────────────────────────────────────────────────────

    def _seen(self, key: str) -> bool:
        return (
            key in self._delivered
            or key in self._inflight
            or any(e.dedupe_key == key for e in self._pending)
            or any(e.dedupe_key == key for e in self._held)
        )

    def _roll_budget(self, date: str) -> None:
        if date != self._budget_date:
            self._budget_date = date
            self._spoken = 0
            self._messaged = 0

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            self._pending = [ProactiveEvent(**e) for e in raw["pending"]]
            self._held = [ProactiveEvent(**e) for e in raw["held"]]
            self._delivered = {str(k): float(v) for k, v in raw["delivered"].items()}
            budget = raw["budget"]
            self._budget_date = str(budget.get("date", ""))
            self._spoken = int(budget.get("spoken", 0))
            self._messaged = int(budget.get("messaged", 0))
            self._next_id = int(raw.get("next_id", len(self._pending) + len(self._held) + 1))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            # A corrupt file costs the events it held; it must not cost the
            # assistant. Start empty and overwrite on the next change.
            log.warning("could not read %s — starting with no events", self._path)

    def _save(self) -> None:
        # Delivered keys age out here — every mutation passes through, so the
        # map is pruned exactly as often as it grows.
        if self._delivered:
            newest = max(self._delivered.values())
            self._delivered = {
                k: v for k, v in self._delivered.items()
                if newest - v <= _DELIVERED_RETENTION_S
            }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "next_id": self._next_id,
            "pending": [asdict(e) for e in self._pending],
            "held": [asdict(e) for e in self._held],
            "delivered": self._delivered,
            "budget": {
                "date": self._budget_date,
                "spoken": self._spoken,
                "messaged": self._messaged,
            },
        }, indent=2))
        tmp.replace(self._path)


__all__ = ["EventQueue", "ProactiveEvent"]
