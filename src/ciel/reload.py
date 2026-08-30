"""Hot reload — pick up code changes without anyone restarting Ciel by hand.

Codename: **Analytic Continuation** — the same conversation extended onto a
new domain, agreeing with the old one everywhere they overlap.

True in-process reloading (importlib.reload) is a minefield for a system like
this: a running asyncio loop, loaded models, live audio streams, and object
instances still bound to their old classes. The failure mode is exactly the
stale-code confusion reload exists to prevent.

So reload is a clean re-exec instead: watch the source tree, and when
something changes, finish the conversation in flight, shut everything down
properly, and replace the process with a fresh one (``os.execv``). Models
reload and the previous conversation resumes via the session file, so the
cost is a few seconds of startup — automated, rather than manual.

Touching ``~/.ciel/reload`` forces one without editing any source.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

POLL_S = 2.0


class SourceWatcher:
    """Polls a set of roots for changes: ``.py`` files under any directory
    root, plus any file root as-is (that's the reload sentinel).

    Polling rather than FSEvents/watchdog: a scan of a few dozen files every
    couple of seconds is nothing, and it needs no extra dependency and no
    platform-specific code.
    """

    def __init__(self, roots: Iterable[Path], poll_s: float = POLL_S) -> None:
        self._roots = list(roots)
        self._poll = poll_s
        self._task: asyncio.Task[None] | None = None
        self._baseline: dict[Path, float] = {}
        self._changed: Path | None = None

    @property
    def changed(self) -> Path | None:
        """The first path seen to change, or ``None`` if nothing has."""
        return self._changed

    def _snapshot(self) -> dict[Path, float]:
        snap: dict[Path, float] = {}
        for root in self._roots:
            try:
                if root.is_file():
                    snap[root] = root.stat().st_mtime
                elif root.is_dir():
                    for path in root.rglob("*.py"):
                        if "__pycache__" in path.parts:
                            continue
                        try:
                            snap[path] = path.stat().st_mtime
                        except OSError:
                            # rglob yielded it, so it exists; a stat blip here
                            # is transient. Carry the last reading forward
                            # rather than dropping the file — dropping it (and,
                            # before, every file after it, since one failure
                            # abandoned the whole scan) reads as a mass removal
                            # and fires a spurious reload.
                            if path in self._baseline:
                                snap[path] = self._baseline[path]
            except OSError:
                continue
        return snap

    async def start(self) -> None:
        self._baseline = await asyncio.to_thread(self._snapshot)
        self._task = asyncio.create_task(self._watch())
        log.debug("watching %d files for reload", len(self._baseline))

    async def _watch(self) -> None:
        while self._changed is None:
            await asyncio.sleep(self._poll)
            snap = await asyncio.to_thread(self._snapshot)
            if snap != self._baseline:
                edited = {p for p, m in snap.items() if self._baseline.get(p) != m}
                removed = set(self._baseline) - set(snap)
                self._changed = sorted(edited | removed)[0]
                log.info("source changed: %s", self._changed)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def default_roots(state_dir: Path) -> list[Path]:
    """What to watch: Ciel's own package, plus a touchable sentinel file."""
    return [Path(__file__).resolve().parent, state_dir / "reload"]


__all__ = ["SourceWatcher", "default_roots", "POLL_S"]
