"""The recent-actions tool — the model's read side of the action journal.

Undo has to start from ground truth. The model's memory of what it did is a
paraphrase — it remembers "I fixed the config", not the byte-exact previous
contents or the eventId the calendar returned — and after a restart or session
resume it may remember nothing at all. This tool hands back the journal:
timestamps, arguments, response excerpts, and snapshot paths, in exactly the
form an accurate inverse needs.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import tool

from ciel.journal import ActionJournal

log = logging.getLogger(__name__)

# Bound at startup, same pattern as the memory store: the SDK's @tool
# decorator wants plain module-level functions.
_journal: ActionJournal | None = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def _format(entry: dict[str, Any]) -> str:
    lines = [f"{entry.get('when', '?')} — {entry.get('tool', '?')}"]
    args = entry.get("args") or {}
    if args:
        rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
        lines.append(f"  args: {rendered}")
    if entry.get("snapshot"):
        lines.append(f"  previous contents saved at: {entry['snapshot']}")
    if entry.get("note"):
        lines.append(f"  note: {entry['note']}")
    if entry.get("response"):
        lines.append(f"  result: {entry['response']}")
    return "\n".join(lines)


@tool(
    "recent_actions",
    (
        "List the actions you recently performed — file writes, shell "
        "commands, emails, calendar changes — newest first, with their "
        "arguments, results, and (for file edits) the path of a snapshot "
        "holding the file's previous contents.\n\n"
        "Call this FIRST whenever the user says something you did was wrong "
        "or asks you to undo something. Undo from this record, never from "
        "recollection: restore a file by reading its snapshot and writing "
        "those contents back; delete a calendar event you created using the "
        "id in its recorded result. If the record shows the action cannot be "
        "reversed — a sent email is sent — say so plainly."
    ),
    {"count": int},
)
async def recent_actions(args: dict[str, Any]) -> dict[str, Any]:
    if _journal is None:
        return _text("The action journal is not available right now.")

    try:
        count = max(1, min(int(args.get("count") or 10), 50))
    except (TypeError, ValueError):
        count = 10

    entries = _journal.recent(count)
    if not entries:
        return _text("No actions recorded yet.")
    return _text("\n\n".join(_format(e) for e in entries))


def bind_journal(journal: ActionJournal) -> None:
    """Attach the journal this tool reads."""
    global _journal
    _journal = journal


ACTION_TOOLS = [recent_actions]

__all__ = ["ACTION_TOOLS", "bind_journal", "recent_actions"]
