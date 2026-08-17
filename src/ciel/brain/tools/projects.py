"""Project tools — how Ciel opens and maintains its durable workspaces.

Same principle as the memory tools: the descriptions are the training.
Nothing else tells the model to open a project before working on it, to
replace state rather than append to it, or to start a project unprompted the
moment ongoing work begins.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import tool

from ciel.projects import ProjectStore

log = logging.getLogger(__name__)

# Bound at startup, same pattern as the memory store.
_store: ProjectStore | None = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "open_project",
    (
        "Read a project's full current state and recent log. The one-line "
        "index in your system prompt tells you which projects exist; open one "
        "before doing any work on it — your recollection of a project is "
        "stale the moment a conversation ends, and the file is the truth. "
        "Use the exact name from the index."
    ),
    {"name": str},
)
async def open_project(args: dict[str, Any]) -> dict[str, Any]:
    if _store is None:
        return _text("Projects are not available right now.")
    name = (args.get("name") or "").strip()
    if not name:
        return _text("A project name is needed.")
    project = _store.get(name)
    if project is None:
        return _text(
            f"No project named '{name}'. The index in your system prompt "
            "lists the exact names; update_project creates new ones."
        )
    log_block = (
        "\n".join(f"- {line}" for line in project.log) if project.log else "(empty)"
    )
    return _text(
        f"# {project.name} ({project.status})\n{project.description}\n\n"
        f"## State\n{project.state}\n\n## Recent log\n{log_block}"
    )


@tool(
    "update_project",
    (
        "Create a project or replace its working state. Projects are durable "
        "workspaces for anything spanning multiple conversations — a training "
        "effort, a trip being planned, a long errand. Create one as soon as "
        "such work starts, without being asked.\n\n"
        "`state` must be the complete current picture, written to be read "
        "cold weeks later: where things stand, key values and paths, what's "
        "next. Replace, don't append — condense as you go; the tool refuses "
        "states over four thousand characters. Update whenever reality "
        "moves: a decision, a result, a changed plan.\n\n"
        "`description` is the one-liner for the index (needed when creating); "
        "`status` is active, paused, or done — mark projects done rather "
        "than abandoning them in the index."
    ),
    {"name": str, "state": str, "description": str, "status": str},
)
async def update_project(args: dict[str, Any]) -> dict[str, Any]:
    if _store is None:
        return _text("Projects are not available right now.")
    name = (args.get("name") or "").strip()
    state = (args.get("state") or "").strip()
    if not name or not state:
        return _text("Both a project name and a state are needed.")
    try:
        project = _store.write(
            name,
            state,
            description=(args.get("description") or "").strip() or None,
            status=(args.get("status") or "").strip() or None,
        )
    except ValueError as exc:
        return _text(str(exc))
    return _text(f"Project '{project.name}' saved ({project.status}).")


@tool(
    "log_progress",
    (
        "Append one dated line to a project's log without rewriting its "
        "state — milestones, measurements, decisions worth a timestamp. One "
        "event per call, one sentence per event."
    ),
    {"name": str, "entry": str},
)
async def log_progress(args: dict[str, Any]) -> dict[str, Any]:
    if _store is None:
        return _text("Projects are not available right now.")
    name = (args.get("name") or "").strip()
    entry = (args.get("entry") or "").strip()
    if not name or not entry:
        return _text("Both a project name and a log entry are needed.")
    if _store.append_log(name, entry):
        return _text(f"Logged to '{name}'.")
    return _text(f"No project named '{name}' — create it with update_project first.")


def bind_projects(store: ProjectStore) -> None:
    """Attach the store these tools operate on."""
    global _store
    _store = store


PROJECT_TOOLS = [open_project, update_project, log_progress]

__all__ = ["PROJECT_TOOLS", "bind_projects", "open_project", "update_project", "log_progress"]
