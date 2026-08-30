"""Vigil — the proactive layer: watchers, one event queue, and the earned
right to interrupt.

Everything else Ciel says was asked for. Vigil is the machinery for the one
exception: events the world produces on its own — a meeting drawing near, a
job finishing, a rough night on the ring — flowing through a single queue,
judged by a
deterministic interruption policy, and delivered by an *unattended* brain
turn bound by the Witness rule (see ``ciel.brain.witness``).

The pieces are deliberately hub-shaped: events are plain serializable data,
the policy is a pure function of its arguments, and a watcher is anything
that pushes into the queue — so when Ciel's brain one day moves off this
machine, these parts relocate instead of being rewritten.
"""

from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.policy import Decision, InterruptionPolicy
from ciel.proactive.presence import PresenceProbe, PresenceState

__all__ = [
    "Decision",
    "EventQueue",
    "InterruptionPolicy",
    "PresenceProbe",
    "PresenceState",
    "ProactiveEvent",
]
