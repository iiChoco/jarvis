"""Conversation continuity across restarts.

This is the *shallower* half of memory: picking up the thread you were on when
Ciel was last running. It is deliberately separate from the durable memory
store, because the two fail differently — a resumed session eventually gets
compacted and its details dissolve, which is exactly why facts worth keeping
need their own home.

The time window matters. Resuming a conversation from ten minutes ago is
helpful; resuming one from last week means Ciel answers a question you have
long since forgotten asking.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    updated_at: float

    @property
    def age_hours(self) -> float:
        return (time.time() - self.updated_at) / 3600.0


class SessionStore:
    """Remembers the last session id, so the next launch can resume it."""

    def __init__(self, path: Path, resume_window_hours: float) -> None:
        self._path = path
        self._window = resume_window_hours

    def load(self) -> SessionRecord | None:
        """Return the previous session, or ``None`` if there isn't a usable one."""
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text())
            record = SessionRecord(
                session_id=str(raw["session_id"]),
                updated_at=float(raw["updated_at"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # A corrupt state file should cost a fresh conversation, never a
            # crash on startup.
            log.warning("session state at %s is unreadable — starting fresh", self._path)
            return None

        if record.age_hours > self._window:
            log.info(
                "last conversation was %.1f hours ago (window %.1f) — starting fresh",
                record.age_hours,
                self._window,
            )
            return None

        log.info("resuming conversation from %.0f minutes ago", record.age_hours * 60)
        return record

    def save(self, session_id: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": session_id, "updated_at": time.time()}
        # Write-then-rename so an interrupted write can't leave a half-file
        # that the next launch has to recover from.
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


__all__ = ["SessionStore", "SessionRecord"]
