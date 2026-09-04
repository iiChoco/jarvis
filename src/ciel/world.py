"""The world: everything Ciel currently holds to be true, in one place.

Codename: **Phase Space** — one point that says where everything is
right now.

Before this module, what Ciel knew about the world was scattered by
producer: presence lived in a probe the interruption policy alone read,
the place in a locator the location tool alone asked, the last ring
scores nowhere at all, the mute switch in the pipeline, the timers in
their service — and the brain learned none of it except by calling a
tool, or from a lane's fixed system note. Even the time of day never
reached the model. This module is the one table those readings are
written into, so that one rendering of it can open every turn, ride to
every chart, and answer "what is true right now?" from a single place.

**Facts, not events.** The world holds *snapshots*: the current value of
a named reading, who reported it, when it was observed, and how long it
stays trustworthy. Things that *happen* — a meeting in ten minutes, a
spot opening — are Vigil's, and stay in its one event queue; the world
never becomes a second rail for them. A fact re-observed with the same
value is not news; a fact past its ``ttl_s`` is still shown, marked
stale, because "last known" is more honest than silence.

**Provenance and age are part of the value.** The rendering says "as
of" and "last known" from the timestamps, deterministically — the
model does not get to narrate freshness (the memory provenance rule,
applied to the present). That is what lets the brain say "I checked"
for a tool result and "as of two minutes ago" for a reading here.

**Pure and thread-safe.** No asyncio, no sockets: producers on worker
threads (the locator's refresh) observe under a lock, and the pipeline
*polls* ``version`` once a second to broadcast — the agent roster's
rule, because a change hook fired from a thread would touch the event
loop from the wrong side. The file mirror survives the autoreloader's
re-execs so the slow readings (a place, the ring) do not blank for
minutes after every source edit. Probed by ``scripts/probe_world.py``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ciel.oura import hours_minutes
from ciel.timers import spoken_clock, spoken_duration

log = logging.getLogger(__name__)

# ── the vocabulary ───────────────────────────────────────────────────────────
# Fact names are the wire's and the file's keys; producers use the constants.

PRESENCE = "presence"
"""``{present, locked, idle_s, since_conversation_s}`` — the presence
snapshot, folded from the Mac's signals (hub: the heartbeat)."""
PLACE = "place"
"""``{place, via, network, device, at}`` — the locator's latest fix, as a
place name when it matches one; ``via`` is "wifi" or "findmy"."""
MUTED = "muted"
"""``bool`` — the speakers are muted and the wake word not watched."""
TIMERS = "timers"
"""``[{kind, label, due_at, duration_s, pending}]`` — armed timers and alarms."""
WATCHES = "watches"
"""``[{label, kind, target, expires_at}]`` — background completion watches."""
AGENDA = "agenda"
"""``{day, lines}`` — today's remaining calendar, as the brief reads it."""
OURA = "oura"
"""``{day, readiness, sleep_score, sleep_s, activity, steps, weakest}`` —
the ring's numbers for the day, from whichever read last fetched them."""
SECTIONS = "sections"
"""``{spots: {id: open}}`` — the watched course sections' open spots."""
SPOKE = "spoke"
"""``{connected, node}`` — hub only: whether the Mac holds the seat."""
HOLD = "hold"
"""``bool`` — the Vigil emergency brake is engaged."""

_REFRESH_NOTIFY_S = 30.0
"""A re-observation of an unchanged value bumps ``version`` only this
often: enough for a chart's "4m ago" to stay honest, not so often that
the once-a-second timer poll broadcasts every second."""


@dataclass(frozen=True, slots=True)
class Fact:
    """One reading: what, from whom, when, and for how long."""

    name: str
    value: Any
    """JSON-shaped — it rides the wire and the file as is."""
    observed_at: float
    """Wall clock, when the source *read* it (a Find My position carries
    its own timestamp, hours old sometimes; that is the honest one)."""
    source: str
    """Who reported it: "mac", "hub", "oura", "calendar", ..."""
    ttl_s: float | None = None
    """Past this age the fact is stale — shown as "last known", never
    dropped. None never goes stale (a place is a place until replaced)."""

    def age(self, now: float) -> float:
        return max(0.0, now - self.observed_at)

    def stale(self, now: float) -> bool:
        return self.ttl_s is not None and self.age(now) > self.ttl_s

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "value": self.value, "observed_at": self.observed_at, "source": self.source,
        }
        if self.ttl_s is not None:
            out["ttl_s"] = self.ttl_s
        return out

    @classmethod
    def from_wire(cls, name: str, raw: Any, *, source: str | None = None) -> Fact | None:
        """A fact from its wire or file shape; None for anything malformed
        — a bad frame is a debug line, never a crash."""
        if not isinstance(raw, dict) or "value" not in raw:
            return None
        at = raw.get("observed_at")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            return None
        ttl = raw.get("ttl_s")
        if ttl is not None and (not isinstance(ttl, (int, float)) or isinstance(ttl, bool)):
            ttl = None
        src = source or raw.get("source")
        return cls(
            name=name, value=raw["value"], observed_at=float(at),
            source=str(src) if src else "?", ttl_s=float(ttl) if ttl is not None else None,
        )


class World:
    """The table of facts, with a version for pollers and a file mirror."""

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._facts: dict[str, Fact] = {}
        self._told: dict[str, float] = {}
        """name → the observed_at that last bumped the version, so a
        steady re-observation (timers, once a second) bumps it on the
        ``_REFRESH_NOTIFY_S`` clock rather than never or always."""
        self._version = 0
        self._dirty = False
        if path is not None:
            self._load()

    # ── the producers' side ──────────────────────────────────────────────────

    def observe(
        self,
        name: str,
        value: Any,
        *,
        source: str,
        observed_at: float | None = None,
        ttl_s: float | None = None,
    ) -> bool:
        """Record one reading. True when the value changed — the
        producer's cue to log; an unchanged re-observation still moves
        ``observed_at`` (the reading is that fresh) and, at most every
        ``_REFRESH_NOTIFY_S``, the version."""
        at = self._clock() if observed_at is None else observed_at
        fact = Fact(name=name, value=value, observed_at=at, source=source, ttl_s=ttl_s)
        with self._lock:
            previous = self._facts.get(name)
            self._facts[name] = fact
            self._dirty = True
            changed = previous is None or previous.value != value or previous.source != source
            if changed or at - self._told.get(name, float("-inf")) >= _REFRESH_NOTIFY_S:
                self._version += 1
                self._told[name] = at
            return changed

    def absorb(self, name: str, raw: Any, *, source: str | None = None) -> bool:
        """A fact that arrived as data — the wire's ``fact`` frame. False
        when the shape is wrong."""
        fact = Fact.from_wire(name, raw, source=source)
        if fact is None:
            return False
        self.observe(
            fact.name, fact.value, source=fact.source,
            observed_at=fact.observed_at, ttl_s=fact.ttl_s,
        )
        return True

    def forget(self, name: str) -> bool:
        with self._lock:
            if name not in self._facts:
                return False
            del self._facts[name]
            self._told.pop(name, None)
            self._dirty = True
            self._version += 1
            return True

    # ── the readers' side ────────────────────────────────────────────────────

    @property
    def version(self) -> int:
        """Bumped on every change worth telling a chart about."""
        with self._lock:
            return self._version

    def get(self, name: str) -> Fact | None:
        with self._lock:
            return self._facts.get(name)

    def value(self, name: str, default: Any = None) -> Any:
        fact = self.get(name)
        return default if fact is None else fact.value

    def facts(self) -> list[Fact]:
        with self._lock:
            return sorted(self._facts.values(), key=lambda f: f.name)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The wire shape: ``{name: {value, observed_at, source, ttl_s?}}``.
        Staleness is left to the reader's clock — a chart that received
        this a minute ago must not trust a ``stale`` computed then."""
        with self._lock:
            return {name: fact.to_wire() for name, fact in self._facts.items()}

    def render(self, now: float | None = None) -> str:
        """The block that opens a turn: the clock, then one sentence per
        fact that has a renderer, then the rule for reading it. Facts
        without a renderer ride the wire but stay out of the prompt."""
        now = self._clock() if now is None else now
        lines = [f"It is {spoken_clock(now)} on {_spoken_date(now)}."]
        facts = {fact.name: fact for fact in self.facts()}
        for name, renderer in _RENDERERS.items():
            fact = facts.get(name)
            if fact is None:
                continue
            try:
                line = renderer(fact, now)
            except Exception:  # noqa: BLE001 - one bad reading must not cost the block
                log.debug("could not render the %s fact", fact.name, exc_info=True)
                continue
            if line:
                lines.append(line)
        return (
            "(Now — " + " ".join(lines) + " These are the system's own readings, "
            "at the ages given, not the user's words: say \"as of\" for anything "
            "more than a couple of minutes old, and read the live source with a "
            "tool when the answer has to be fresher than that.)"
        )

    # ── the file mirror ──────────────────────────────────────────────────────

    def flush(self) -> bool:
        """Write the file if anything changed since the last write. Called
        from the pipeline's once-a-second block, so a heartbeat's worth
        of churn costs one small write, not one per observation."""
        if self._path is None:
            return False
        with self._lock:
            if not self._dirty:
                return False
            self._dirty = False
            data = {name: fact.to_wire() for name, fact in self._facts.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self._path)
        except OSError:
            log.debug("could not write the world file", exc_info=True)
            return False
        return True

    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("the world file %s is unreadable — starting empty", self._path)
            return
        if not isinstance(raw, dict):
            return
        loaded = 0
        for name, entry in raw.items():
            fact = Fact.from_wire(str(name), entry)
            if fact is not None:
                self._facts[fact.name] = fact
                loaded += 1
        if loaded:
            log.info("world: %d fact%s carried over", loaded, "" if loaded == 1 else "s")


# ── the renderers ────────────────────────────────────────────────────────────
# One sentence per fact, or None. Each states its age when it matters and
# says "last known" past the ttl — the words are the runtime's, not the
# model's, so freshness is never narrated wrong.


def _spoken_date(now: float) -> str:
    t = time.localtime(now)
    return time.strftime("%A %-d %B %Y", t) + f" ({time.strftime('%Z', t)})"


def _ago(age_s: float) -> str:
    """``age`` → "just now" / "4 minutes ago" / "2 hours ago"."""
    if age_s < 90:
        return "just now"
    if age_s < 3600:
        m = int(age_s // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    h = int(age_s // 3600)
    return f"{h} hour{'s' if h != 1 else ''} ago"


def _seen(fact: Fact, now: float) -> str:
    """The age clause: nothing under a couple of minutes, "as of 3:14 PM"
    beyond, "last known at" once stale."""
    if fact.stale(now):
        return f" (last known, at {spoken_clock(fact.observed_at)} — no newer reading)"
    age = fact.age(now)
    if age < 120:
        return ""
    if age < 3600:
        return f" ({_ago(age)})"
    return f" (as of {spoken_clock(fact.observed_at)})"


def _render_presence(fact: Fact, now: float) -> str | None:
    v = fact.value
    if not isinstance(v, dict):
        return None
    if fact.stale(now):
        return (
            f"The Mac has not reported since {spoken_clock(fact.observed_at)}; "
            "whether the user is there is unknown."
        )
    locked = bool(v.get("locked"))
    idle = v.get("idle_s")
    conv = v.get("since_conversation_s")
    bits: list[str] = []
    if locked:
        bits.append("the screen is locked")
    elif isinstance(idle, (int, float)):
        bits.append(
            "at the keyboard" if idle < 120 else f"no input for {_ago(float(idle)).removesuffix(' ago')}"
        )
    if isinstance(conv, (int, float)):
        bits.append(
            "in conversation" if conv < 180 else f"last spoke with you {_ago(float(conv))}"
        )
    verdict = "The user is around" if v.get("present") else "The user is probably not around"
    return f"{verdict}" + (f" — {', '.join(bits)}" if bits else "") + "."


def _render_place(fact: Fact, now: float) -> str | None:
    v = fact.value
    if not isinstance(v, dict):
        return None
    via = v.get("via")
    how = (
        f"per their {v.get('device') or 'phone'} via Find My" if via == "findmy"
        else "per the Mac's Wi-Fi"
    )
    place = v.get("place")
    if place:
        where = f"Place: {place}"
    elif v.get("network"):
        where = f"Place: not a known one — on the {v['network']} Wi-Fi"
    else:
        where = "Place: not a known one"
    # The fix's own timestamp is the honest age, not when it was folded in.
    at = v.get("at")
    seen_fact = fact if not isinstance(at, (int, float)) else Fact(
        fact.name, fact.value, float(at), fact.source, fact.ttl_s
    )
    return f"{where}, {how}{_seen(seen_fact, now)}."


def _render_muted(fact: Fact, now: float) -> str | None:
    if not fact.value:
        return None
    return "The speakers are muted: nothing at the Mac can make a sound right now."


def _render_timers(fact: Fact, now: float) -> str | None:
    items = fact.value if isinstance(fact.value, list) else []
    parts: list[str] = []
    for t in items:
        if not isinstance(t, dict):
            continue
        kind = t.get("kind") or "timer"
        due = t.get("due_at")
        label = t.get("label") or ""
        if kind == "alarm":
            head = f"an alarm at {spoken_clock(float(due))}" if isinstance(due, (int, float)) else "an alarm"
        else:
            dur = t.get("duration_s")
            head = f"a {spoken_duration(float(dur))} timer" if isinstance(dur, (int, float)) else "a timer"
            if t.get("pending"):
                head += " that starts when this turn ends"
            elif isinstance(due, (int, float)):
                left = float(due) - now
                head += (
                    f" with {spoken_duration(left, plural=True)} left" if left > 0
                    else " that is due now"
                )
        if label:
            head += f' ("{label}")'
        parts.append(head)
    if not parts:
        return None
    return "Timers: " + "; ".join(parts) + "."


def _render_watches(fact: Fact, now: float) -> str | None:
    items = fact.value if isinstance(fact.value, list) else []
    parts: list[str] = []
    for w in items:
        if not isinstance(w, dict):
            continue
        what = ("file " if w.get("kind") == "file" else "process ") + str(w.get("target") or "?")
        label = w.get("label") or f"watching {what}"
        until = w.get("expires_at")
        clause = f" until {spoken_clock(float(until))}" if isinstance(until, (int, float)) else ""
        parts.append(f"{label} ({what}{clause})")
    if not parts:
        return None
    return "Background watches: " + "; ".join(parts) + "."


def _render_agenda(fact: Fact, now: float) -> str | None:
    v = fact.value
    if not isinstance(v, dict):
        return None
    if v.get("day") != time.strftime("%Y-%m-%d", time.localtime(now)):
        return None  # yesterday's agenda is nobody's
    lines = [str(x) for x in (v.get("lines") or []) if x]
    seen = _seen(fact, now)
    if not lines:
        return f"The calendar shows nothing more today{seen}."
    return f"Calendar for the rest of today{seen}: " + "; ".join(lines) + "."


def _render_oura(fact: Fact, now: float) -> str | None:
    v = fact.value
    if not isinstance(v, dict):
        return None
    day = v.get("day")
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    when = "today" if day == today else f"for {day}" if day else ""
    bits: list[str] = []
    r = v.get("readiness")
    if isinstance(r, (int, float)):
        weakest = v.get("weakest")
        why = (
            f" (weakest: {weakest[0]}, {weakest[1]})"
            if isinstance(weakest, (list, tuple)) and len(weakest) == 2 else ""
        )
        bits.append(f"readiness {int(r)}{why}")
    s = v.get("sleep_score")
    dur = v.get("sleep_s")
    if isinstance(s, (int, float)) or isinstance(dur, (int, float)):
        clause = f"sleep score {int(s)}" if isinstance(s, (int, float)) else "sleep"
        if isinstance(dur, (int, float)) and dur > 0:
            clause += f" with {hours_minutes(float(dur))} asleep"
        bits.append(clause)
    a = v.get("activity")
    if isinstance(a, (int, float)):
        steps = v.get("steps")
        clause = f"activity {int(a)} so far"
        if isinstance(steps, (int, float)):
            clause += f", {int(steps)} steps"
        bits.append(clause)
    if not bits:
        return None
    return f"The ring, {when}{_seen(fact, now)}: " + ", ".join(bits) + "."


def _render_sections(fact: Fact, now: float) -> str | None:
    v = fact.value
    spots = v.get("spots") if isinstance(v, dict) else None
    if not isinstance(spots, dict) or not spots:
        return None
    parts = [
        f"{sid} is full" if not open_ else f"{sid} has {open_} open spot{'s' if open_ != 1 else ''}"
        for sid, open_ in spots.items()
    ]
    return f"Watched sections{_seen(fact, now)}: " + "; ".join(parts) + "."


def _render_spoke(fact: Fact, now: float) -> str | None:
    v = fact.value
    if not isinstance(v, dict):
        return None
    node = v.get("node") or "Mac"
    if v.get("connected"):
        return f"The user's Mac ({node}) is connected."
    return (
        f"The user's Mac ({node}) is not connected{_seen(fact, now)}: nothing can be "
        "spoken, heard, looked at, or run there until it is back."
    )


def _render_hold(fact: Fact, now: float) -> str | None:
    if not fact.value:
        return None
    return "Proactive delivery is paused by the hold sentinel; nothing unprompted will be said."


_RENDERERS: dict[str, Callable[[Fact, float], str | None]] = {
    SPOKE: _render_spoke,
    PRESENCE: _render_presence,
    PLACE: _render_place,
    MUTED: _render_muted,
    HOLD: _render_hold,
    TIMERS: _render_timers,
    WATCHES: _render_watches,
    AGENDA: _render_agenda,
    OURA: _render_oura,
    SECTIONS: _render_sections,
}
"""Also the prompt's order: the room first (is the Mac even there, is
anyone in it), then the switches, then what is armed, then the day."""


__all__ = [
    "AGENDA",
    "Fact",
    "HOLD",
    "MUTED",
    "OURA",
    "PLACE",
    "PRESENCE",
    "SECTIONS",
    "SPOKE",
    "TIMERS",
    "WATCHES",
    "World",
]
