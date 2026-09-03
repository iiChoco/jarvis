"""Ciel's custom tool registry.

This is the extension point. To give Ciel a new capability:

1. Write a module in this package with one or more ``@tool``-decorated
   functions (see ``memory.py`` for the shape).
2. Add them to ``TOOLS`` below.

That's the whole procedure for a self-contained tool — no wiring in the
pipeline, no changes to the brain. One that needs a bound service, a config
gate, or the confirmation tier also needs its branch in
:func:`build_tool_server` below and, for the confirm gate, an entry in
``agent.py``'s gated set. Everything registered here is exposed to Ciel
through a single in-process MCP server, and auto-allowed so a locked-down
``permission_mode`` doesn't silently block it.

Connecting an *external* service (Google Calendar, Gmail) is different and even
cheaper: those are MCP servers, so they go in ``config.py`` as connection
entries and need no code here at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server

from ciel.brain.tools.actions import ACTION_TOOLS, bind_journal
from ciel.brain.tools.grants import GRANT_TOOLS
from ciel.brain.tools.grants import bind_config as bind_grants
from ciel.brain.tools.location import LOCATION_TOOLS, bind_locator
from ciel.brain.tools.mac import MAC_TOOLS, bind_mac
from ciel.brain.tools.mail import MAIL_TOOLS, bind_mail
from ciel.brain.tools.memory import MEMORY_TOOLS, bind_store
from ciel.brain.tools.messages import MESSAGE_TOOLS, SEND_TOOLS
from ciel.brain.tools.messages import bind_client as bind_messages
from ciel.brain.tools.oura import OURA_TOOLS, bind_oura
from ciel.brain.tools.projects import PROJECT_TOOLS, bind_projects
from ciel.brain.tools.screen import SCREEN_TOOLS, bind_screen
from ciel.brain.tools.timers import TIMER_TOOLS, bind_timers
from ciel.brain.tools.watch import WATCH_TOOLS, bind_watcher
from ciel.config import Config, MCPServerConfig
from ciel.journal import ActionJournal
from ciel.mail import SmtpSender
from ciel.memory.store import MemoryStore
from ciel.messages import MessagesClient
from ciel.oura import OuraClient, build_auth
from ciel.projects import ProjectStore
from ciel.timers import TimerService

log = logging.getLogger(__name__)

SERVER_NAME = "ciel"

# Every custom tool Ciel can call. Append to this list to add a capability.
TOOLS = [
    *MEMORY_TOOLS, *MESSAGE_TOOLS, *ACTION_TOOLS, *PROJECT_TOOLS,
    *SCREEN_TOOLS, *TIMER_TOOLS, *WATCH_TOOLS, *GRANT_TOOLS, *OURA_TOOLS,
    *LOCATION_TOOLS, *MAIL_TOOLS,
]


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
    remote: Any | None = None,
) -> tuple[
    dict[str, Any],
    list[str],
    MemoryStore | None,
    ProjectStore | None,
    ActionJournal | None,
    TimerService | None,
]:
    """Assemble Ciel's MCP servers: the in-process one plus external connectors.

    Returns the ``mcp_servers`` mapping, the tool names to add to
    ``allowed_tools``, the memory and project stores (so the caller can wire
    their index providers into the system prompt), the action journal (so the
    brain can hook its recorder up to the same instance this registry's tool
    reads), and the timer service (so the pipeline can poll it for due
    announcements — the tools only *arm* timers; the pipeline speaks them).

    ``remote`` is the hub's ``RemoteBindings``: with it, everything
    Mac-bound — the screen, Messages, location, the work watcher — binds
    the wire's duck type instead of the local implementation, and the
    Mac tools (the user's shell and files, from a brain elsewhere) are
    registered. None is the single process and the spoke.
    """
    store: MemoryStore | None = None
    journal: ActionJournal | None = None
    projects: ProjectStore | None = None
    timers: TimerService | None = None
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

    if config.screen.enabled:
        bind_screen(config.screen, remote.screen if remote is not None else None)
    else:
        # Same reasoning as memory: an always-refusing tool wastes turns.
        tools = [t for t in tools if t not in SCREEN_TOOLS]

    if config.timers.enabled:
        timers = TimerService(config.timers.file)
        bind_timers(timers)
    else:
        # Same reasoning as memory: an always-unavailable tool wastes turns.
        tools = [t for t in tools if t not in TIMER_TOOLS]

    if not config.proactive.enabled:
        # The watch tool lands events in Vigil's queue; without Vigil there
        # is no delivery path. Same reasoning as memory: an always-refusing
        # tool wastes turns. The pipeline binds the watcher after it builds
        # the queue (the bind-late pattern memory's context provider uses).
        tools = [t for t in tools if t not in WATCH_TOOLS]
    elif remote is not None:
        # The watches live on the Mac; the hub's registry is the wire.
        bind_watcher(remote.watcher)

    if config.grants.enabled:
        # The path is the loader's default, not derived from state_dir:
        # load_config reads ~/.ciel/config.toml regardless of where state
        # lives, and the grant must edit the file the next startup reads.
        bind_grants(config, Path.home() / ".ciel" / "config.toml")
    else:
        # Same reasoning as memory: an always-refusing tool wastes turns —
        # and this one is the escalation channel, so absent means absent.
        tools = [t for t in tools if t not in GRANT_TOOLS]

    auth = build_auth(config.oura) if config.oura.armed else None
    if auth is not None:
        bind_oura(OuraClient(auth))
    else:
        if config.oura.enabled:
            log.warning(
                "[oura] is enabled but %s holds no authorization — run "
                "`uv run scripts/probe_oura.py --authorize`; the ring tool is off",
                config.oura.token_file,
            )
        # Same reasoning as memory: an always-unavailable tool wastes turns.
        tools = [t for t in tools if t not in OURA_TOOLS]

    if not config.location.enabled:
        # The pipeline binds the locator at startup (the watch tool's
        # bind-late pattern); off, the tool would only ever answer
        # unavailable — same reasoning as memory.
        tools = [t for t in tools if t not in LOCATION_TOOLS]
    elif remote is not None:
        bind_locator(remote.locator)

    if config.mail.armed:
        # Ciel's own address. The gate for the tool lives in agent.py's
        # confirm set; here it only needs its relay.
        bind_mail(
            SmtpSender(
                config.mail.smtp_host, config.mail.smtp_port,
                config.mail.smtp_user, config.mail.token, config.mail.copy_to,
            ),
            config.mail,
        )
    else:
        if config.mail.enabled:
            log.warning(
                "[mail] is enabled but address or token is missing — "
                "send_as_ciel is off"
            )
        # Same reasoning as memory: an always-unavailable tool wastes turns.
        tools = [t for t in tools if t not in MAIL_TOOLS]

    if config.messages.enabled:
        bind_messages(
            remote.messages if remote is not None else MessagesClient(config.messages)
        )
        if not config.messages.allow_send:
            # Reading without sending is a coherent setup and the safer default,
            # so the send tool is withheld rather than left to fail at call
            # time. A tool that is present but always refuses just wastes turns.
            tools = [t for t in tools if t not in SEND_TOOLS]
    else:
        tools = [t for t in tools if t not in MESSAGE_TOOLS]

    if remote is not None and (config.shell.enabled or config.files.enabled):
        # The user's Mac from the hub: the shell when the shell is on,
        # the files when files are on — the same switches, the same
        # guards, one machine over.
        bind_mac(remote.mac, config.shell)
        mac_tools = [
            t for t in MAC_TOOLS
            if (t.name == "run_on_mac" and config.shell.enabled)
            or (t.name != "run_on_mac" and config.files.enabled)
        ]
        tools = [*tools, *mac_tools]
    else:
        tools = [t for t in tools if t not in MAC_TOOLS]

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
    return servers, allowed, store, projects, journal, timers


__all__ = ["TOOLS", "SERVER_NAME", "build_tool_server"]
