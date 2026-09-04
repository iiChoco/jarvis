"""The watch tool — ask Vigil to notice when background work finishes.

Attended-only by construction: this tool is absent from the Witness allow
list, so an unattended turn that tries to arm a watch is denied — arming one
is scheduling future unprompted speech, which is a budget bypass. The
pipeline binds the watcher after Vigil is constructed (the
``bind_memory_context`` pattern); until then, and whenever Vigil is
disabled, the tool answers unavailable.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import tool

if TYPE_CHECKING:
    from ciel.proactive.work import WorkWatcher

log = logging.getLogger(__name__)

_watcher: "WorkWatcher | None" = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


async def _settle(value: Any) -> Any:
    """The local watcher answers at once; the hub's answers over the
    wire. The tool treats both the same."""
    if inspect.isawaitable(value):
        return await value
    return value


@tool(
    "watch_for_completion",
    (
        "Ask to be told when background work finishes: watch for a file to "
        "appear, or for a process to exit. When it happens (or the deadline "
        "passes without it), the user is notified through the proactive "
        "layer — spoken if they're around, held for the next conversation "
        "if not.\n\n"
        "Use this when you start or learn about something long-running — an "
        "export writing a file, a build, a download — instead of promising "
        "to 'check later' (you cannot; between turns you are not running, "
        "but the watcher is).\n\n"
        "`label` is EXACTLY what will be spoken when it completes — phrase "
        "it as a complete spoken sentence ('The video export has finished.'), "
        "not a tag. Provide `path` for a file that should appear, or `pid` "
        "for a process that should exit — exactly one of the two."
    ),
    {"path": str, "pid": float, "label": str, "timeout_minutes": float},
)
async def watch_for_completion(args: dict[str, Any]) -> dict[str, Any]:
    if _watcher is None:
        return _text("Background watching is not available right now.")

    label = (args.get("label") or "").strip()
    if not label:
        return _text("A label is needed — it is the sentence spoken on completion.")
    raw_timeout = args.get("timeout_minutes")
    try:
        timeout = float(raw_timeout) if raw_timeout is not None else 60.0
    except (TypeError, ValueError):
        return _text("timeout_minutes must be a number of minutes.")
    if not (0 < timeout <= 24 * 60):
        return _text("timeout_minutes must be between 0 and 1440 (a day).")

    path = (args.get("path") or "").strip()
    pid = args.get("pid")
    if bool(path) == (pid is not None):
        return _text("Provide exactly one of `path` or `pid`.")

    if path:
        target = Path(path).expanduser()
        if getattr(_watcher, "observe", None) is None and target.exists():
            # Only the local watcher can check this machine's disk; the
            # Mac's watcher checks its own the moment the watch is armed.
            return _text(
                f"{target} already exists — nothing to watch. If a new "
                "version is being written, watch a temporary name or check "
                "it directly."
            )
        try:
            watch = await _settle(_watcher.add_file(str(target), label, timeout))
        except Exception as exc:  # noqa: BLE001 - the Mac may be unreachable
            return _text(f"Could not arm the watch: {exc}")
        return _text(
            f"Watching for {target} (id {watch.id}, up to {timeout:.0f} "
            "minutes). The user will be told when it appears."
        )

    try:
        pid_int = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _text("pid must be a process id number.")
    try:
        watch = await _settle(_watcher.add_pid(pid_int, label, timeout))
    except Exception as exc:  # noqa: BLE001 - the Mac may be unreachable
        return _text(f"Could not arm the watch: {exc}")
    return _text(
        f"Watching process {pid_int} (id {watch.id}, up to {timeout:.0f} "
        "minutes). The user will be told when it exits."
    )


def bind_watcher(watcher: "WorkWatcher") -> None:
    """Attach the work watcher these registrations land in."""
    global _watcher
    _watcher = watcher


WATCH_TOOLS = [watch_for_completion]

__all__ = ["WATCH_TOOLS", "bind_watcher", "watch_for_completion"]
