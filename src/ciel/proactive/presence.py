"""Presence: is the user actually around to hear an interruption?

Three signals, none perfect alone:

* the screen being unlocked (locked means gone, on any machine that locks);
* recency of keyboard/mouse input — meaningful only on a machine somebody
  types on;
* recency of conversation with Ciel — the one signal that works on a box
  with no keyboard attached, where the mic *is* the interface.

Which signals count is ``presence_mode`` config, because the deployment
machine is undecided: a daily driver trusts the first two, an appliance in
the corner of a room has only the third. The Quartz reads are cheap C calls
in the already-installed pyobjc, but they still happen lazily — only when a
deliverable event exists, never at frame rate.

The output is a frozen :class:`PresenceState` snapshot: the policy consumes
facts, never Quartz, so probes inject readers and the endgame's "which node
last heard the user" swaps in behind the same shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ciel.config import ProactiveConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PresenceState:
    screen_locked: bool
    seconds_since_input: float
    seconds_since_conversation: float | None
    present: bool
    """Derived once, here, from the signals above and the configured mode —
    so the policy stays a consumer of one boolean fact."""


def _read_screen_locked() -> bool:
    """Whether the login session's screen is locked.

    Verified behavior on this machine: ``CGSessionCopyCurrentDictionary``
    carries a ``CGSSessionScreenIsLocked`` key *only while locked* — a
    missing key, or no session dictionary at all (headless/SSH contexts),
    reads as unlocked. That default errs toward "present", which is the
    right error: the budget and quiet hours still bound the damage, while
    the opposite default would silence Vigil entirely on any box that
    can't answer.
    """
    import Quartz

    session = Quartz.CGSessionCopyCurrentDictionary()
    if not session:
        return False
    return bool(dict(session).get("CGSSessionScreenIsLocked", False))


def _read_hid_idle() -> float:
    """Seconds since the last keyboard/mouse event, system-wide."""
    import Quartz

    return float(
        Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
        )
    )


class PresenceProbe:
    """Snapshots presence on demand; remembers when Ciel was last talked to.

    Readers are injectable for probes; the defaults are the Quartz calls
    above, each guarded — a failed read degrades that one signal to its
    "contributes nothing" value rather than costing the snapshot.
    """

    def __init__(
        self,
        config: "ProactiveConfig",
        *,
        lock_reader: Callable[[], bool] | None = None,
        idle_reader: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._lock_reader = lock_reader or _read_screen_locked
        self._idle_reader = idle_reader or _read_hid_idle
        self._last_conversation: float | None = None

    def note_conversation(self, now: float) -> None:
        """The pipeline calls this as a conversation ends — monotonic clock,
        same as the frame loop's own bookkeeping."""
        self._last_conversation = now

    def state(self, now: float) -> PresenceState:
        try:
            locked = self._lock_reader()
        except Exception:  # noqa: BLE001 - one dead signal must not cost the snapshot
            log.debug("screen lock read failed", exc_info=True)
            locked = False
        try:
            idle = self._idle_reader()
        except Exception:  # noqa: BLE001 - same: degrade the signal, keep the snapshot
            log.debug("HID idle read failed", exc_info=True)
            idle = float("inf")

        since_conv = (
            now - self._last_conversation
            if self._last_conversation is not None
            else None
        )

        at_keyboard = not locked and idle < self._config.presence_idle_s
        in_conversation = (
            since_conv is not None
            and since_conv < self._config.conversation_presence_s
        )
        mode = self._config.presence_mode
        if mode == "hid":
            present = at_keyboard
        elif mode == "conversation":
            present = in_conversation
        else:  # "any"
            present = at_keyboard or in_conversation

        return PresenceState(
            screen_locked=locked,
            seconds_since_input=idle,
            seconds_since_conversation=since_conv,
            present=present,
        )


__all__ = ["PresenceProbe", "PresenceState"]
