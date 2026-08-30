"""The turn-slot arbiter — who gets the next moment of Ciel's attention.

The frame loop's priority ladder used to live as a chain of ``if …
continue`` blocks, each re-stating its own guard conditions inline —
correct, but readable only as a whole, testable only end-to-end, and
welded to the microphone: the ladder ran once per audio frame, so no
audio meant no scheduling at all.

``pick_next`` is that ladder as a pure function: a frozen snapshot of
every work source in, one arbitration out, no clock reads, no I/O —
the interruption policy's discipline applied to scheduling. The
pipeline builds a snapshot each frame and dispatches; the hub's
arbiter (the endgame) builds one whenever any source stirs and
dispatches the same way, with no microphone anywhere. The ladder IS
the scheduling contract, so it lives here where both can share it.

The order, and why (each entry outranks everything below it):

1. **A pending confirmation** — an unresolved gate question wedges the
   SDK subprocess; nothing else matters until it resolves.
2. **Due timers** — "the rice is done" tolerates no queueing behind a
   conversation that hasn't started yet.
3. **The keyboard, then the web page** — the user at the machine, in
   tier order; both may claim a turn out of a follow-up window but
   never mid-utterance and never over a held partial thought.
4. **The Discord lane** — a user lane, so it outranks Vigil, but only
   from WAITING: whoever is in the room speaking or mid-follow-up
   outranks the phone, and a text tolerates the seconds that costs.
5. **Vigil** — the machine's own initiative queues behind every human.

Nothing here *consumes* anything: a claimed source is popped by the
dispatch code, so an un-enacted pick (a busy maintenance slot, a hold
sentinel) simply recurs next round with no churn.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class State(Enum):
    """Where the conversation is — the frame loop's three meanings for
    the same incoming audio frame."""

    WAITING = auto()    # not addressed; frames go to the wake detector
    LISTENING = auto()  # addressed; frames go to the endpointer
    BUSY = auto()       # thinking or speaking; frames watched for barge-in


class Source(Enum):
    """Who the turn slot goes to. NONE: nobody pending — the state
    machine (wake word, endpointer, barge-in) owns the frame."""

    CONFIRM = auto()
    TIMERS = auto()
    TYPED = auto()
    WEB = auto()
    REMOTE = auto()
    VIGIL = auto()
    NONE = auto()


@dataclass(frozen=True)
class Snapshot:
    """The facts arbitration needs, frozen at one instant.

    Deadline arithmetic stays with the caller (``vigil_ready`` arrives
    already compared against the policy-poll clock, ``timers_due``
    already swept for the missed-timer flag) so this stays a pure
    membership test — probes drive every row of the table without a
    clock in sight.
    """

    state: State
    confirm_active: bool = False
    endpointer_speaking: bool = False
    held_thought: bool = False
    timers_due: bool = False
    typed_pending: bool = False
    web_pending: bool = False
    remote_pending: bool = False
    vigil_ready: bool = False


def pick_next(s: Snapshot) -> Source:
    """One arbitration of the ladder. Pure; the docstring above is the
    specification and the order below is the whole of it."""
    if s.confirm_active:
        return Source.CONFIRM
    # The user's own audio outranks every queue: never claim the slot
    # while a turn runs (BUSY), mid-utterance, or over a held partial
    # thought (the Cauchy hold resolves first).
    quiet = (
        s.state is not State.BUSY
        and not s.endpointer_speaking
        and not s.held_thought
    )
    if s.timers_due and quiet:
        return Source.TIMERS
    if s.typed_pending and quiet:
        return Source.TYPED
    if s.web_pending and quiet:
        return Source.WEB
    if s.remote_pending and s.state is State.WAITING:
        return Source.REMOTE
    if s.vigil_ready and s.state is State.WAITING:
        return Source.VIGIL
    return Source.NONE


__all__ = ["Snapshot", "Source", "State", "pick_next"]
