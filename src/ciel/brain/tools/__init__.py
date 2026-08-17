"""Ciel's custom tool registry.

This is the extension point. To give Ciel a new capability:

1. Write a module in this package with one or more ``@tool``-decorated
   functions (see ``memory.py`` for the shape).
2. Add them to ``TOOLS`` below.

That's the whole procedure — no wiring in the pipeline, no changes to the
brain. Everything registered here is exposed to Ciel through a single
in-process MCP server, and auto-allowed so a locked-down ``permission_mode``
doesn't silently block it.

Connecting an *external* service (Google Calendar, Gmail) is different and even
cheaper: those are MCP servers, so they go in ``config.py`` as connection
entries and need no code here at all.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server

from ciel.brain.tools.actions import ACTION_TOOLS, bind_journal
from ciel.brain.tools.memory import MEMORY_TOOLS, bind_store
from ciel.brain.tools.messages import MESSAGE_TOOLS, SEND_TOOLS
from ciel.brain.tools.messages import bind_client as bind_messages
from ciel.brain.tools.projects import PROJECT_TOOLS, bind_projects
from ciel.config import Config, MCPServerConfig
from ciel.journal import ActionJournal
from ciel.memory.store import MemoryStore
from ciel.messages import MessagesClient
from ciel.projects import ProjectStore

log = logging.getLogger(__name__)

SERVER_NAME = "ciel"

# Every custom tool Ciel can call. Append to this list to add a capability.
TOOLS = [*MEMORY_TOOLS, *MESSAGE_TOOLS, *ACTION_TOOLS, *PROJECT_TOOLS]


def _qualified(tool_name: str) -> str:
    """MCP tools are addressed as ``mcp__<server>__<tool>`` in allowed_tools."""
    return f"mcp__{SERVER_NAME}__{tool_name}"


def _external_server(entry: MCPServerConfig) -> dict[str, Any] | None:
    """Translate one ``[mcp.<name>]`` config entry into the SDK's shape."""
    if entry.url:
        remote: dict[str, Any] = {"type": entry.transport, "url": entry.url}
        if entry.headers:
            remote["headers"] = dict(entry.headers)
        return remote
    if entry.command:
        local: dict[str, Any] = {"type": "stdio", "command": entry.command}
        if entry.args:
            local["args"] = list(entry.args)
        if entry.env:
            local["env"] = dict(entry.env)
        return local
    return None


def build_tool_server(
    config: Config,
) -> tuple[
    dict[str, Any], list[str], MemoryStore | None, ProjectStore | None, ActionJournal | None
]:
    """Assemble Ciel's MCP servers: the in-process one plus external connectors.

    Returns the ``mcp_servers`` mapping, the tool names to add to
    ``allowed_tools``, the memory and project stores (so the caller can wire
    their index providers into the system prompt), and the action journal
    (so the brain can hook its recorder up to the same instance this
    registry's tool reads).
    """
    store: MemoryStore | None = None
    journal: ActionJournal | None = None
    projects: ProjectStore | None = None
    tools = list(TOOLS)

    if config.memory.enabled:
        store = MemoryStore(config.memory.dir, config.memory.max_index_entries)
        bind_store(store)
    else:
        # Without a store the memory tools would answer "unavailable" to every
        # call, which wastes a turn and confuses the model. Better to not offer
        # them at all.
        tools = [t for t in tools if t not in MEMORY_TOOLS]

    if config.projects.enabled:
        projects = ProjectStore(
            config.projects.dir,
            config.projects.max_index_entries,
            config.projects.max_state_chars,
            config.projects.log_tail,
        )
        bind_projects(projects)
    else:
        # Same reasoning as memory: an always-unavailable tool wastes turns.
        tools = [t for t in tools if t not in PROJECT_TOOLS]

    if config.journal.enabled:
        journal = ActionJournal(config.journal)
        journal.ensure()
        bind_journal(journal)
    else:
        # Same reasoning as memory: a recent_actions that always answers
        # "unavailable" wastes a turn and teaches the model not to try.
        tools = [t for t in tools if t not in ACTION_TOOLS]

    if config.messages.enabled:
        bind_messages(MessagesClient(config.messages))
        if not config.messages.allow_send:
            # Reading without sending is a coherent setup and the safer default,
            # so the send tool is withheld rather than left to fail at call
            # time. A tool that is present but always refuses just wastes turns.
            tools = [t for t in tools if t not in SEND_TOOLS]
    else:
        tools = [t for t in tools if t not in MESSAGE_TOOLS]

    servers: dict[str, Any] = {}
    allowed: list[str] = []

    if tools:
        servers[SERVER_NAME] = create_sdk_mcp_server(
            name=SERVER_NAME, version="0.1.0", tools=tools
        )
        allowed.extend(_qualified(t.name) for t in tools)

    # External connectors from [mcp.<name>] config. Each authenticates on its
    # own terms — its own OAuth token, its own API key, whichever account was
    # signed in during its setup. Nothing here touches the Claude subscription
    # that runs the brain.
    for name, entry in config.mcp.items():
        if name == SERVER_NAME:
            log.warning("the MCP server name %r is reserved — skipping it", name)
            continue
        translated = _external_server(entry)
        if translated is None:
            log.warning("[mcp.%s] has neither `command` nor `url` — skipping it", name)
            continue
        servers[name] = translated
        if entry.tools is None:
            # The whole server: permission_mode="dontAsk" denies anything not
            # allowed, so an unlisted connector would just be dead weight.
            # Confirm-listed tools are already covered by the wildcard; the
            # guard still holds them for the spoken yes.
            allowed.append(f"mcp__{name}")
        else:
            allowed.extend(f"mcp__{name}__{tool}" for tool in entry.tools)
            # Confirmed tools are allowed too — the gate lives in the hook,
            # not in this list. Absent from here they would be denied before
            # the hook ever got to ask.
            allowed.extend(f"mcp__{name}__{tool}" for tool in entry.confirm)
        log.info(
            "connector %s: %s (%s%s)",
            name,
            entry.url or entry.command,
            "all tools" if entry.tools is None else f"{len(entry.tools)} quiet tools",
            f", {len(entry.confirm)} confirmed" if entry.confirm else "",
        )

    if allowed:
        log.debug("allowed tools: %s", ", ".join(allowed))
    return servers, allowed, store, projects, journal


__all__ = ["TOOLS", "SERVER_NAME", "build_tool_server"]
