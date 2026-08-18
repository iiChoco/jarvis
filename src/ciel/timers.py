"""Timers and alarms — the ability to speak *later*.

Everything else Ciel does happens inside a turn the user started. A timer is
the opposite: the user asks once, the conversation ends, and minutes or hours
later Ciel has to open her mouth unprompted. This module owns the bookkeeping
half of that — what is due, what was missed, what to say. The speaking half
lives in the pipeline, which polls ``pop_due()`` from its frame loop and
plays the announcements it gets back.

Wall-clock time throughout, not monotonic: "wake me at seven" means seven on
the clock, and a laptop that sleeps through the night must still know the
alarm came due. That is also why timers persist to disk — a restart (or the
autoreloader) must not silently swallow a running timer. On startup the
pipeline asks for ``pop_missed()``: anything that came due while Ciel was off
gets announced late-but-honestly within a grace window, and dropped with a
log line past it. A stale "your rice is done" from yesterday helps no one.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Timer:
    id: str
    kind: str  # "timer" (relative) or "alarm" (clock time) — phrasing only
    due_at: float  # wall-clock epoch seconds
    label: str = ""  # what to say when it fires; "" for a plain timer
    duration_s: float = 0.0  # original length, so a timer can say what it was
    pending: bool = False
    """Armed mid-turn, countdown not yet started. The tool call lands seconds
    before Ciel finishes saying "ten minutes — starting now", and a countdown
    anchored to the tool call makes that sentence a lie. So a relative timer
    is born pending, and the pipeline calls ``commit_pending()`` when the
    turn's speech ends — re-stamping ``due_at`` so "now" means the moment the
    user heard it. Alarms are clock-anchored and never pending."""


def spoken_duration(seconds: float, *, plural: bool = False) -> str:
    """``600`` → "10 minute" (adjective form) or "10 minutes" with ``plural``,
    for "your 10 minute timer" and "10 minutes left". TTS reads digits."""
    s = round(seconds)
    if s < 60:
        value: float = s
        unit = "second"
    elif s < 90 * 60:
        # Half-minute precision, so a 90 second timer is "1.5 minutes", not
        # a rounded lie of "2 minutes".
        value, unit = round(s / 30) / 2, "minute"
    else:
        value, unit = round(s / 1800) / 2, "hour"
    if plural and value != 1:
        unit += "s"
    return f"{value:g} {unit}"


def spoken_clock(epoch: float) -> str:
    """``…07:00`` → "7:00 AM" — the way a person says a clock time."""
    t = time.localtime(epoch)
    hour = t.tm_hour % 12 or 12
    half = "AM" if t.tm_hour < 12 else "PM"
    return f"{hour}:{t.tm_min:02d} {half}"


def announcement(timer: Timer) -> str:
    """What Ciel says when this fires. The label, when given, *is* the
    message — the tool tells the model to phrase it as a spoken sentence."""
    if timer.label:
        return timer.label
    if timer.kind == "alarm":
        return f"It's {spoken_clock(timer.due_at)}. This is your alarm."
    return f"Your {spoken_duration(timer.duration_s)} timer is done."


class TimerService:
    """The set of armed timers, mirrored to disk on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._timers: list[Timer] = []
        self._next_id = 1
        self._load()

    # ── the brain's side (tools) ─────────────────────────────────────────────

    def set_relative(self, seconds: float, label: str = "") -> Timer:
        """Arm a countdown. Born pending with a provisional ``due_at``; the
        real anchor is stamped by ``commit_pending()`` at end of speech, and
        the provisional one only survives if the process dies first."""
        return self._add(
            "timer", time.time() + seconds, label, duration_s=seconds, pending=True
        )

    def set_at(self, hour: int, minute: int, label: str = "") -> Timer:
        """Arm for the next occurrence of a local clock time — later today if
        it hasn't passed yet, otherwise tomorrow."""
        now = time.localtime()
        due = time.mktime(
            (now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0,
             now.tm_wday, now.tm_yday, -1)
        )
        if due <= time.time():
            due += 24 * 3600
        return self._add("alarm", due, label)

    def cancel(self, query: str = "") -> Timer | None:
        """Cancel by id, by label fragment, or — with no query and exactly one
        timer armed — that one. Returns what was cancelled, None if nothing
        matched unambiguously."""
        q = query.strip().lower()
        if not q:
            matches = list(self._timers)
        else:
            matches = [t for t in self._timers if t.id == q] or [
                t for t in self._timers if q in t.label.lower()
            ]
        if len(matches) != 1:
            return None
        self._timers.remove(matches[0])
        self._save()
        return matches[0]

    def active(self) -> list[Timer]:
        return sorted(self._timers, key=lambda t: t.due_at)

    # ── the pipeline's side ──────────────────────────────────────────────────

    def pop_due(self, now: float | None = None) -> list[Timer]:
        """Remove and return everything that has come due. The caller owns
        announcing them; once popped they are off the books, so a caller that
        drops one drops it silently — announce before doing anything else.

        Pending timers never come due — a two second timer must not fire off
        its provisional anchor before the turn that set it finishes speaking.
        """
        now = time.time() if now is None else now
        due = [t for t in self._timers if not t.pending and t.due_at <= now]
        if due:
            self._timers = [t for t in self._timers if t.pending or t.due_at > now]
            self._save()
        return sorted(due, key=lambda t: t.due_at)

    def commit_pending(self, now: float | None = None) -> None:
        """Start pending countdowns — called as the turn's speech finishes.

        Re-stamps each pending timer's ``due_at`` from *this* moment, so the
        "starting now" the user just heard is when the count actually starts.
        """
        if not any(t.pending for t in self._timers):
            return
        now = time.time() if now is None else now
        self._timers = [
            replace(t, pending=False, due_at=now + t.duration_s) if t.pending else t
            for t in self._timers
        ]
        self._save()

    def pop_missed(self, grace_s: float, now: float | None = None) -> list[Timer]:
        """Startup sweep: due-while-off timers worth a late announcement.

        Within the grace window they come back to be spoken; older ones are
        dropped with a log line. Either way the file is left holding only the
        future.
        """
        now = time.time() if now is None else now
        missed = self.pop_due(now)
        stale = [t for t in missed if now - t.due_at > grace_s]
        for t in stale:
            log.info("dropping stale %s %s (%r), due %.0f min ago",
                     t.kind, t.id, t.label, (now - t.due_at) / 60)
        return [t for t in missed if now - t.due_at <= grace_s]

    # ── persistence ──────────────────────────────────────────────────────────

    def _add(
        self,
        kind: str,
        due_at: float,
        label: str,
        duration_s: float = 0.0,
        pending: bool = False,
    ) -> Timer:
        timer = Timer(
            id=f"t{self._next_id}", kind=kind, due_at=due_at,
            label=label.strip(), duration_s=duration_s, pending=pending,
        )
        self._next_id += 1
        self._timers.append(timer)
        self._save()
        return timer

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            # A timer still pending on disk means the process died between the
            # tool call and the end of that turn's speech. Its provisional
            # anchor is the best remaining truth, so it stands.
            self._timers = [
                replace(t, pending=False) if t.pending else t
                for t in (Timer(**entry) for entry in raw["timers"])
            ]
            self._next_id = int(raw.get("next_id", len(self._timers) + 1))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            # A corrupt file costs the timers it held; it must not cost the
            # assistant. Start empty and overwrite on the next change.
            log.warning("could not read %s — starting with no timers", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "next_id": self._next_id,
            "timers": [asdict(t) for t in self._timers],
        }, indent=2))
        tmp.replace(self._path)


__all__ = ["Timer", "TimerService", "announcement", "spoken_clock", "spoken_duration"]
