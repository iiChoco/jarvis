"""The interruption policy: whether this moment has earned an interruption.

Ciel's own wishlist said it plainly: the mechanism for speaking unprompted is
easy — the policy for when interrupting is justified is the hard part. This
module is that policy, kept deliberately pure: no clock reads, no Quartz, no
file I/O. Every fact it needs — the event, a frozen presence snapshot, the
budget spent, the time — arrives as an argument, so a probe can drive every
branch without waiting for midnight or locking a screen.

The decision here is a *ceiling*, not a verdict (the one-way valve): the
unattended turn that follows may still decline to speak (SKIP) or de-escalate
to a held note (HOLD), but nothing downstream can turn a "hold" into speech.
Event text is attacker-influenced — calendar titles, mail subjects — and the
model reads it, so routing authority stays in this deterministic code where
injected words can't argue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ciel.config import ProactiveConfig
    from ciel.proactive.events import ProactiveEvent
    from ciel.proactive.presence import PresenceState


@dataclass(frozen=True, slots=True)
class Decision:
    """What the policy chose, and the reason a log line can print."""

    action: Literal["speak", "message", "hold", "drop"]
    reason: str


def parse_hhmm(value: str) -> int | None:
    """``"22:00"`` → minutes since local midnight; None for anything else.

    Lenient on purpose: quiet hours misconfigured shouldn't crash the
    assistant, they should warn at load and disable the window — the caller
    decides which.
    """
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


class InterruptionPolicy:
    """Deterministic routing for one event: speak now, hold, or drop."""

    def __init__(self, config: "ProactiveConfig") -> None:
        self._config = config
        self._quiet_start = parse_hhmm(config.quiet_hours_start)
        self._quiet_end = parse_hhmm(config.quiet_hours_end)

    def in_quiet_hours(self, local_minutes: int) -> bool:
        """Whether a local clock time (minutes since midnight) is inside the
        quiet window. The window may span midnight — "22:00" to "08:00" is
        quiet at 23:30 and at 06:00 — and start == end means no window at
        all rather than a 24-hour one: a zero-width setting reading as
        "never speak" would be a config trap."""
        start, end = self._quiet_start, self._quiet_end
        if start is None or end is None or start == end:
            return False
        if start < end:
            return start <= local_minutes < end
        return local_minutes >= start or local_minutes < end

    def decide(
        self,
        event: "ProactiveEvent",
        *,
        presence: "PresenceState",
        spoken_today: int,
        now: float,
        local_minutes: int,
        messaged_today: int = 0,
        can_message: bool = False,
    ) -> Decision:
        """Route one event. Order matters and is the policy:

        expiry first (a dead event needs no judgement), then quiet hours
        (they outrank importance — the whole point of a quiet window is that
        nothing crosses it, and a phone buzz at 2am violates it exactly as
        speech does, so texts hold too), then the importance floor, then
        presence, then the budgets. Away routes to "message" only when the
        event clears the higher texting bar, the caller says messaging is
        actually wired (``can_message``: owner handle pinned AND the
        messages switches on), and the texting budget has room. Everything
        that isn't "speak" or "message" is "hold": held notes are the safety
        net that makes every other rule cheap to apply.
        """
        if event.expires_at is not None and event.expires_at <= now:
            return Decision("drop", "expired")
        if self.in_quiet_hours(local_minutes):
            return Decision("hold", "quiet hours")
        if event.importance < self._config.min_importance_to_speak:
            return Decision("hold", "below the importance floor")
        if not presence.present:
            if (
                can_message
                and event.importance >= self._config.min_importance_to_message
                and messaged_today < self._config.max_messaged_per_day
            ):
                return Decision("message", "urgent, and the user is away")
            return Decision("hold", "nobody appears to be around")
        if spoken_today >= self._config.max_spoken_per_day:
            return Decision("hold", "the day's spoken budget is spent")
        return Decision("speak", "present, in hours, within budget")


__all__ = ["Decision", "InterruptionPolicy", "parse_hhmm"]
