"""The spoke's timer mirror — an alarm clock that never depends on a server.

The hub owns the timers: the brain's tools arm them, the hub's loop pops
them when due and delivers the announcement through the spoke. That is
the right home for the bookkeeping and the wrong place for the ringing
to *depend* on: a timer the user armed is a promise, and "the hub was
unreachable at 7 AM" is not an excuse a person accepts from an alarm.

So the hub broadcasts its timer set as ``timers.sync`` whenever it
changes, and the spoke keeps this mirror, persisted like the hub's own
file so a restart mid-countdown loses nothing. Two rules make it safe:

* The spoke rings a mirrored timer itself only when the hub cannot —
  the link is down at the due moment, or the hub has been silent about
  a timer that came due ``grace_s`` ago. Otherwise the hub's
  ``deliver.speak`` rings it, exactly as before.
* Whatever the spoke rang is remembered by id. A hub that comes back
  and delivers the same timer is receipted without a sound. Nothing
  rings twice; nothing goes unrung.

With the hub down, the command grammar runs here too: "ten minute
timer" arms a *local* timer in this mirror (it never reaches the hub —
the hub was not there to tell), "cancel the timer" and "what timers are
running" answer from it. When the hub is back, the grammar goes up the
wire again as it always did.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ciel.timers import Timer, announcement, spoken_clock, spoken_duration

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Mirrored:
    """One timer as the spoke knows it: the hub's, or its own."""

    timer: Timer
    local: bool
    """Armed here, offline; the hub never heard of it."""
    rung_at: float | None = None
    """When the spoke rang it itself; None until then."""


def timer_from(raw: dict[str, Any]) -> Timer:
    return Timer(
        id=str(raw["id"]), kind=str(raw.get("kind") or "timer"),
        due_at=float(raw["due_at"]), label=str(raw.get("label") or ""),
        duration_s=float(raw.get("duration_s") or 0.0),
        pending=bool(raw.get("pending")),
    )


class TimerMirror:
    """The hub's timers as last synced, plus the spoke's own."""

    def __init__(self, path: Path, grace_s: float = 20.0) -> None:
        self._path = path
        self._grace_s = grace_s
        self._items: dict[str, Mirrored] = {}
        self._next_local = 1
        self._load()

    # ── the wire's side ──────────────────────────────────────────────────────

    def sync(self, timers: list[dict[str, Any]], now: float | None = None) -> None:
        """The hub's whole set. Its timers replace the mirrored ones;
        local timers and the rung record survive."""
        now = time.time() if now is None else now
        kept = {k: v for k, v in self._items.items() if v.local}
        rung = {k: v.rung_at for k, v in self._items.items() if v.rung_at is not None}
        for raw in timers:
            try:
                t = timer_from(raw)
            except (KeyError, TypeError, ValueError):
                continue
            kept[t.id] = Mirrored(t, local=False, rung_at=rung.get(t.id))
        # A timer the hub no longer lists but the spoke rang: remembered a
        # little longer, so a late delivery is still receipted silently.
        for k, at in rung.items():
            if k not in kept and k in self._items and now - at < 600:
                kept[k] = self._items[k]
        self._items = kept
        self._save()

    def rung_already(self, timer_id: str) -> bool:
        item = self._items.get(timer_id)
        return item is not None and item.rung_at is not None

    # ── the room's side ──────────────────────────────────────────────────────

    def due(self, now: float, *, hub_connected: bool) -> list[Timer]:
        """The timers the spoke should ring now: local ones at their time;
        the hub's only when the hub cannot — the link down, or the timer
        overdue by the grace with no delivery."""
        out: list[Timer] = []
        for item in self._items.values():
            t = item.timer
            if item.rung_at is not None or t.pending or t.due_at > now:
                continue
            if item.local or not hub_connected or now - t.due_at >= self._grace_s:
                out.append(t)
        return sorted(out, key=lambda t: t.due_at)

    def mark_rung(self, timer_id: str, now: float) -> None:
        item = self._items.get(timer_id)
        if item is None:
            return
        self._items[timer_id] = replace(item, rung_at=now)
        if item.local:
            del self._items[timer_id]
        self._save()

    def set_local(self, seconds: float, now: float) -> Timer:
        t = Timer(
            id=f"local-{self._next_local}", kind="timer", due_at=now + seconds,
            duration_s=seconds,
        )
        self._next_local += 1
        self._items[t.id] = Mirrored(t, local=True)
        self._save()
        return t

    def cancel(self, now: float) -> Timer | None:
        """Cancel the one running timer, or None when there are none or
        several (the hub's rule: tell me which). Local first — those are
        the ones only this side can cancel — else the hub's, noted here so
        it is not rung by the mirror; the hub keeps its own copy until it
        hears otherwise."""
        active = self.active(now)
        if len(active) != 1:
            return None
        t = active[0]
        self._items.pop(t.id, None) if self._items[t.id].local else self.mark_rung(t.id, now)
        self._save()
        return t

    def active(self, now: float) -> list[Timer]:
        return sorted(
            (i.timer for i in self._items.values() if i.rung_at is None),
            key=lambda t: t.due_at,
        )

    def listing(self, now: float) -> str:
        """The spoken answer to "what timers are running", the hub's wording."""
        active = self.active(now)
        if not active:
            return "No timers are running."
        parts = []
        for t in active:
            if t.kind == "alarm":
                parts.append(f"an alarm at {spoken_clock(t.due_at)}")
            else:
                left = spoken_duration(max(1.0, t.due_at - now), plural=True)
                parts.append(f"your {spoken_duration(t.duration_s)} timer with {left} left")
        listing = "; ".join(parts)
        return f"{listing[0].upper()}{listing[1:]}."

    def announcement(self, timer: Timer) -> str:
        return announcement(timer)

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        try:
            self._next_local = int(raw.get("next_local", 1))
            for entry in raw.get("items", []):
                t = timer_from(entry["timer"])
                self._items[t.id] = Mirrored(t, bool(entry.get("local")), entry.get("rung_at"))
        except (KeyError, TypeError, ValueError):
            log.warning("timer mirror unreadable — starting empty")
            self._items = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "next_local": self._next_local,
                "items": [
                    {"timer": asdict(i.timer), "local": i.local, "rung_at": i.rung_at}
                    for i in self._items.values()
                ],
            }))
            tmp.replace(self._path)
        except OSError:
            log.debug("could not save the timer mirror", exc_info=True)


__all__ = ["Mirrored", "TimerMirror", "timer_from"]
