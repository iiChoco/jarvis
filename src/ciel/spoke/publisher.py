"""The spoke's publisher: the Mac's watchers, reporting to the hub.

The watchers that must run on the Mac — the calendar store, the
Wi-Fi-and-Find-My locator, the work watcher's file and process polling
— are the same classes the single process runs. What changes is where
their events go. There they push into Vigil's queue; here they push
into this outbox, which sends each one up as ``event.publish`` and
holds it until the hub's ``event.ack``. At-least-once by design: an
unacked event rides again after a reconnect, and the hub's queue
dedupes by key, so a resend costs nothing and a drop is impossible.

The watchers see a queue-shaped object (``next_id``, ``push``) and
nothing else; ``WatcherHealth`` pushes its own self-reports through
the same door. ``ProactiveEvent`` was already plain data (the endgame
notes promised exactly this), so an event is one ``asdict`` from being
a frame.

The heartbeat rides alongside: every ``interval_s`` (and at once when
something changes) the spoke sends ``presence`` — the raw Quartz
signals the in-process probe used to read directly, plus the roster of
active watches, which the hub's agents chip needs and can't read.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Callable

from ciel.proactive.events import ProactiveEvent

if TYPE_CHECKING:
    from ciel.proactive.presence import PresenceProbe
    from ciel.proactive.work import WorkWatcher

log = logging.getLogger(__name__)

_DEDUPE_HORIZON_S = 48 * 3600
"""How long a pushed key is remembered here, so a watcher's rescan does
not re-publish yesterday's meeting every poll. The hub's queue is the
real dedupe; this only keeps the wire quiet."""


class PublishQueue:
    """The watchers' queue, with the hub on the other side."""

    def __init__(self, send: Callable[[dict[str, Any]], bool], node: str = "mac") -> None:
        self._send = send
        self._node = node
        self._seq = 0
        self._outbox: dict[str, dict[str, Any]] = {}
        """publish_id → the event dict, until acked; insertion order is
        the resend order."""
        self._pushed: dict[str, float] = {}
        """dedupe_key → when it was first pushed here."""

    # ── the watchers' side (EventQueue's shape) ──────────────────────────────

    def next_id(self) -> str:
        self._seq += 1
        return f"{self._node}-{self._seq}"

    def push(self, event: ProactiveEvent) -> bool:
        """Publish one event; False for a key already published within
        the horizon (a rescan), which is what the queue's own push says."""
        now = time.time()
        self._forget(now)
        if event.dedupe_key in self._pushed:
            return False
        self._pushed[event.dedupe_key] = now
        self._seq += 1
        publish_id = f"p-{self._node}-{self._seq}"
        frame = {"type": "event.publish", "publish_id": publish_id, "event": asdict(event)}
        self._outbox[publish_id] = frame
        if not self._send(frame):
            log.debug("hub not connected — %s held in the outbox", publish_id)
        return True

    @property
    def pending(self) -> bool:
        return bool(self._outbox)

    # ── the wire's side ──────────────────────────────────────────────────────

    def acked(self, publish_id: str) -> None:
        self._outbox.pop(publish_id, None)

    def resend(self) -> int:
        """After a reconnect: everything still owed rides again."""
        sent = 0
        for frame in list(self._outbox.values()):
            if self._send(frame):
                sent += 1
        return sent

    @property
    def outstanding(self) -> int:
        return len(self._outbox)

    def _forget(self, now: float) -> None:
        stale = [k for k, at in self._pushed.items() if now - at > _DEDUPE_HORIZON_S]
        for k in stale:
            del self._pushed[k]


class Heartbeat:
    """The presence report, on a clock and on change."""

    def __init__(
        self,
        send: Callable[[dict[str, Any]], bool],
        probe: "PresenceProbe | None",
        watcher: "WorkWatcher | None",
        *,
        node: str = "mac",
        interval_s: float = 10.0,
        idle_threshold_s: float = 300.0,
    ) -> None:
        self._send = send
        self._probe = probe
        self._watcher = watcher
        self._node = node
        self._interval = interval_s
        self._idle_threshold = idle_threshold_s
        self._task: asyncio.Task[None] | None = None
        self._last: dict[str, Any] | None = None

    def frame(self) -> dict[str, Any]:
        """One reading, built from the probe's raw readers."""
        locked = False
        idle = float("inf")
        if self._probe is not None:
            state = self._probe.state(time.monotonic())
            locked = state.screen_locked
            idle = state.seconds_since_input
        watches = (
            [
                {
                    "id": w.id, "kind": w.kind, "target": w.target, "label": w.label,
                    "created_at": w.created_at, "expires_at": w.expires_at,
                }
                for w in self._watcher.active()
            ]
            if self._watcher is not None
            else []
        )
        frame: dict[str, Any] = {
            "type": "presence",
            "node": self._node,
            "attended": (not locked) and idle < self._idle_threshold,
            "locked": locked,
            "active_watches": len(watches),
            "watches": watches,
        }
        if idle != float("inf"):
            frame["idle_s"] = round(idle, 1)
        return frame

    def beat(self, *, force: bool = False) -> bool:
        """Send now if something changed (or ``force``); True when sent."""
        frame = self.frame()
        comparable = {k: v for k, v in frame.items() if k != "idle_s"}
        last = {k: v for k, v in (self._last or {}).items() if k != "idle_s"}
        if not force and comparable == last and self._last is not None:
            return False
        if self._send(frame):
            self._last = frame
            return True
        return False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                self.beat(force=True)
            except Exception:  # noqa: BLE001 - a failed reading is one missed beat
                log.debug("heartbeat failed", exc_info=True)
            await asyncio.sleep(self._interval)


__all__ = ["Heartbeat", "PublishQueue"]
