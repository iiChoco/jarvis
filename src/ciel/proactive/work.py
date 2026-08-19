"""The work watcher: "that thing you started — it finished" (or didn't).

Nothing on the machine tracks background jobs for us, so this watcher is
deliberately minimal: it watches exactly what an attended turn *registered* —
a file that should appear, or a process that should exit — and files an
event when it happens. Registration is a brain tool
(``watch_for_completion``), attended-only under the Witness rule: an
unattended turn arming watches would be scheduling its own future speech,
which is the budget bypass the rule exists to prevent.

Watches persist to disk (the ``TimerService`` pattern) because the
autoreloader re-execs constantly and the laptop sleeps: an export watched at
noon must still be watched after both. A watch that reaches its deadline
unfinished files an event too — "never appeared" is exactly the kind of
thing read-back verification exists to say out loud.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ciel.proactive.events import EventQueue, ProactiveEvent

log = logging.getLogger(__name__)

_POLL_S = 15.0
"""Between checks. A completion noticed within fifteen seconds reads as
prompt; polling faster buys nothing a human would notice and keeps a stat
or a kill-0 in the loop's budget."""


@dataclass(frozen=True, slots=True)
class Watch:
    id: str
    kind: str  # "file" (fires when the path exists) | "pid" (fires when gone)
    target: str
    label: str
    """The spoken sentence for the completion event — the tool tells the
    model to phrase it as one, same contract as timer labels."""
    created_at: float
    expires_at: float
    """Every watch has a deadline. A watch that can outlive its meaning
    polls forever and then announces stale news; expiry converts "never
    happened" into its own honest event instead."""


class WorkWatcher:
    """Polls registered watches and files completion (or timeout) events."""

    def __init__(self, path: Path, queue: EventQueue) -> None:
        self._path = path
        self._queue = queue
        self._watches: list[Watch] = []
        self._next_id = 1
        self._task: asyncio.Task[None] | None = None
        self._kick = asyncio.Event()
        self._load()

    # ── the tool's side ──────────────────────────────────────────────────────

    def add_file(self, target: str, label: str, timeout_minutes: float) -> Watch:
        return self._add("file", target, label, timeout_minutes)

    def add_pid(self, pid: int, label: str, timeout_minutes: float) -> Watch:
        return self._add("pid", str(pid), label, timeout_minutes)

    def active(self) -> list[Watch]:
        return list(self._watches)

    def _add(self, kind: str, target: str, label: str, timeout_minutes: float) -> Watch:
        now = time.time()
        watch = Watch(
            id=f"w{self._next_id}",
            kind=kind,
            target=target,
            label=label.strip(),
            created_at=now,
            expires_at=now + max(1.0, timeout_minutes) * 60,
        )
        self._next_id += 1
        self._watches.append(watch)
        self._save()
        self._kick.set()  # check soon; the file may already exist
        return watch

    # ── the watcher contract ─────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    def kick(self) -> None:
        self._kick.set()

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _poll(self) -> None:
        # Split on the thread boundary: only the *probing* (a stat can stall
        # on a network volume, kill-0 is cheap but keeps it company) runs on
        # a worker; every mutation — the watch list, and pushes into the
        # queue with their whole-file saves — happens back on the event
        # loop, where the tool's add_file and the frame loop's queue calls
        # also live. Everything shared is single-threaded again; the review
        # found the previous shape interleaving two writers on one tmp file.
        while True:
            try:
                if self._watches:
                    snapshot = list(self._watches)
                    completed = await asyncio.to_thread(self._probe, snapshot)
                    self._resolve(completed, time.time())
            except Exception:  # noqa: BLE001 - a bad watch must not kill the loop
                log.exception("work watch check failed")
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=_POLL_S)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    def check(self, now: float) -> None:
        """Probe and resolve synchronously — the single-threaded entry the
        probes drive; the poll loop uses the split halves directly."""
        self._resolve(self._probe(list(self._watches)), now)

    def _probe(self, watches: list[Watch]) -> set[str]:
        """The ids of the completed watches. Reads the world, mutates
        nothing — safe on a worker thread over a snapshot."""
        return {w.id for w in watches if self._completed(w)}

    def _resolve(self, completed: set[str], now: float) -> None:
        """Turn completions and expiries into events, on the event loop.

        Operates on the live list, so a watch added while the probe ran is
        simply kept for the next round rather than clobbered.
        """
        keep: list[Watch] = []
        for watch in self._watches:
            if watch.id in completed:
                self._queue.push(ProactiveEvent(
                    id=self._queue.next_id(),
                    source="work",
                    importance=2,
                    created_at=now,
                    expires_at=now + 3600,
                    summary=watch.label,
                    dedupe_key=f"work:{watch.id}:done",
                ))
                log.info("watch %s completed: %s", watch.id, watch.label)
            elif now >= watch.expires_at:
                self._queue.push(ProactiveEvent(
                    id=self._queue.next_id(),
                    source="work",
                    importance=2,
                    created_at=now,
                    expires_at=now + 3600,
                    summary=(
                        f"Something you asked me to watch never finished: "
                        f"{watch.label} It reached its "
                        f"{(watch.expires_at - watch.created_at) / 60:.0f} "
                        "minute deadline without completing."
                    ),
                    dedupe_key=f"work:{watch.id}:timeout",
                ))
                log.info("watch %s timed out: %s", watch.id, watch.label)
            else:
                keep.append(watch)
        if len(keep) != len(self._watches):
            self._watches = keep
            self._save()

    def _completed(self, watch: Watch) -> bool:
        if watch.kind == "file":
            return Path(watch.target).expanduser().exists()
        try:
            os.kill(int(watch.target), 0)
            return False  # still running
        except ProcessLookupError:
            return True
        except (PermissionError, ValueError):
            # Alive but not ours, or garbage target — either way, not "done".
            return False

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            self._watches = [Watch(**entry) for entry in raw["watches"]]
            self._next_id = int(raw.get("next_id", len(self._watches) + 1))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.warning("could not read %s — starting with no watches", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "next_id": self._next_id,
            "watches": [asdict(w) for w in self._watches],
        }, indent=2))
        tmp.replace(self._path)


__all__ = ["Watch", "WorkWatcher"]
