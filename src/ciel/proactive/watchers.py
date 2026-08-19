"""The watcher contract: anything that pushes events into the queue.

A watcher is deliberately tiny — an async ``start()``/``close()`` pair around
whatever polling or subscribing its source needs, producing
:class:`~ciel.proactive.events.ProactiveEvent`\\ s. All judgement about
whether and how an event reaches the user lives elsewhere (the policy and
the unattended turn), so a new watcher is a new module and one line in the
pipeline, nothing more. The protocol is transport-agnostic on purpose: a
future *remote* watcher — a local adapter relaying events published by some
other device — satisfies it exactly as a local poller does.

Phase 1 ships one watcher (``ciel.proactive.calendar``); the schedule
watcher for the morning brief lands here in Phase 2.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from ciel.proactive.events import EventQueue, ProactiveEvent

log = logging.getLogger(__name__)


@runtime_checkable
class Watcher(Protocol):
    """One event source. ``start()`` may spawn tasks; ``close()`` stops them.

    Both are best-effort from the pipeline's point of view: a watcher that
    can't start (a missing framework, a denied permission) logs and idles —
    it must never take the voice loop down with it.
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...


class WatcherHealth:
    """Failure-streak self-report: after enough consecutive failures, stop
    logging the failure and report *the watcher*.

    A watcher that fails every scan logs the same exception forever, and log
    lines are where nobody looks. At the threshold, one importance-1 event
    enters the queue — routine, so it never interrupts; it rides a held note
    into the next conversation ("the calendar watcher has failed three scans
    in a row — worth a look"). One report per streak: the dedupe key is the
    streak's first-failure timestamp, so a watcher that stays broken doesn't
    nag, and a fresh streak after a recovery legitimately reports again.
    """

    def __init__(self, name: str, queue: EventQueue, threshold: int = 3) -> None:
        self._name = name
        self._queue = queue
        self._threshold = threshold
        self._streak = 0
        self._streak_started = 0.0

    def failed(self, now: float) -> None:
        if self._streak == 0:
            self._streak_started = now
        self._streak += 1
        if self._streak != self._threshold:
            return
        self._queue.push(ProactiveEvent(
            id=self._queue.next_id(),
            source="vigil",
            importance=1,
            created_at=now,
            expires_at=None,
            summary=(
                f"The {self._name} watcher has failed {self._threshold} "
                "times in a row — its source may be broken; the log has "
                "the details."
            ),
            dedupe_key=f"vigil:{self._name}:failing:{int(self._streak_started)}",
        ))
        log.warning(
            "%s watcher failing (streak of %d) — reported to the queue",
            self._name, self._streak,
        )

    def succeeded(self) -> None:
        self._streak = 0


__all__ = ["Watcher", "WatcherHealth"]
