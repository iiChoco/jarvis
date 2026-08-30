"""The action journal — what Ciel did, kept long enough to take it back.

Codename: **Inverse** — kept so that operations can be run backwards.

Ciel acts on transcribed speech, and mishearings are discovered *after* the
action, not before: the wrong file edited, an event created on the wrong day.
The voice gate handles consent; this handles regret. Every mutating tool call
is recorded — what ran, with which arguments, what came back — and file edits
save a snapshot of the file's previous contents first, so "put it back how it
was" is a copy, not a reconstruction from the model's memory of what it thinks
it wrote.

Undo itself is deliberately *not* automated here. The journal gives the model
accurate ground truth (snapshot paths, the event id a create call returned)
and the model performs the inverse with its ordinary tools — which means undo
actions pass through the same guards and spoken confirmations as everything
else, instead of becoming a side channel that skips them. The one concession
the guard makes in return: reads (never writes) under the snapshots
directory are allowed even outside the workspace, or a narrow workspace
would leave the model holding a snapshot path it cannot open.

Entries live in one JSONL file, snapshots beside it, both pruned together:
when an entry falls off the end of the journal, its snapshot goes with it.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ciel.config import JournalConfig

log = logging.getLogger(__name__)

# Journal entries hold *summaries*; bulk lives elsewhere. A Write's full
# content is recoverable from the file itself and its previous state from the
# snapshot, so the journal only needs enough of each value to identify the
# action when read back.
_MAX_ARG_CHARS = 300
_MAX_RESPONSE_CHARS = 700

# Orphaned snapshots (their call was denied after the snapshot was taken, or
# the process died mid-call) are collected on the next prune — but only after
# a grace period, so a snapshot for a call still in flight is never reaped.
_ORPHAN_GRACE_S = 3600


def _trim(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}… [truncated]"
    if isinstance(value, dict):
        return {k: _trim(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v, limit) for v in value]
    return value


class ActionJournal:
    """A pruned-to-recent, on-disk record of Ciel's mutating actions."""

    def __init__(self, config: JournalConfig) -> None:
        self._dir = config.dir.expanduser()
        self._file = self._dir / "actions.jsonl"
        self._snapshots = self._dir / "snapshots"
        self._max_entries = max(config.max_entries, 10)
        self._max_snapshot_bytes = config.max_snapshot_kb * 1024

    def ensure(self) -> None:
        self._snapshots.mkdir(parents=True, exist_ok=True)

    # ── recording ────────────────────────────────────────────────────────────

    def snapshot(self, path: Path) -> tuple[str | None, str | None]:
        """Copy a file's current contents aside; returns (path, note).

        Both can be None-ish in ways that still matter to an undo: a file that
        did not exist yet means undo-by-deletion, and one too large to copy
        means the honest answer is "no snapshot", said in the note rather
        than silently.
        """
        try:
            resolved = path.expanduser()
            if not resolved.exists():
                return None, "file did not exist before this call"
            if resolved.stat().st_size > self._max_snapshot_bytes:
                return None, "file too large to snapshot"
            self.ensure()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = self._snapshots / f"{stamp}-{uuid.uuid4().hex[:6]}-{resolved.name}"
            shutil.copy2(resolved, dest)
            return str(dest), None
        except OSError as exc:  # a failed snapshot must not block the action
            log.warning("could not snapshot %s: %s", path, exc)
            return None, f"snapshot failed ({exc})"

    def record(
        self,
        *,
        tool: str,
        args: dict[str, Any] | None,
        response: str | None = None,
        snapshot: str | None = None,
        note: str | None = None,
    ) -> None:
        entry = {
            "ts": time.time(),
            "when": datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "args": _trim(args or {}, _MAX_ARG_CHARS),
            "response": _trim(response, _MAX_RESPONSE_CHARS) if response else None,
            "snapshot": snapshot,
            "note": note,
        }
        try:
            entries = self._load()
            entries.append(entry)
            kept, dropped = entries[-self._max_entries:], entries[:-self._max_entries]
            self.ensure()
            # Write-then-rename: a crash or a full disk mid-write leaves the
            # temp file behind and the real journal intact. Rewriting in place
            # ("w") could truncate the whole history to nothing over one failed
            # append — the record exists precisely for the moments things go
            # wrong, so it must not evaporate at one.
            tmp = self._file.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for e in kept:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            tmp.replace(self._file)
            self._prune_snapshots(dropped, kept)
        except OSError:
            log.warning("could not write the action journal", exc_info=True)

    # ── reading ──────────────────────────────────────────────────────────────

    def recent(self, count: int = 10) -> list[dict[str, Any]]:
        """The last ``count`` actions, newest first."""
        return list(reversed(self._load()[-max(count, 1):]))

    # ── internals ────────────────────────────────────────────────────────────

    def _load(self) -> list[dict[str, Any]]:
        if not self._file.exists():
            return []
        entries = []
        for line in self._file.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn write costs one entry, not the journal
        return entries

    def _prune_snapshots(self, dropped: list[dict], kept: list[dict]) -> None:
        """Delete snapshots for dropped entries, plus stale orphans."""
        referenced = {e.get("snapshot") for e in kept if e.get("snapshot")}
        for entry in dropped:
            snap = entry.get("snapshot")
            if snap and snap not in referenced:
                Path(snap).unlink(missing_ok=True)
        try:
            cutoff = time.time() - _ORPHAN_GRACE_S
            for file in self._snapshots.iterdir():
                if str(file) not in referenced and file.stat().st_mtime < cutoff:
                    file.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["ActionJournal"]
