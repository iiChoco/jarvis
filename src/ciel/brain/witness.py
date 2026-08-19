"""The Witness rule: an unattended turn may look and remember, nothing more.

Codename: **Witness**. Some turns run with nobody listening — reflection
after a conversation ends, and Vigil's proactive turns. For those, the
spoken-confirmation gate that legitimizes outward actions cannot function
(there is no one to say yes), and confirmation *suppression* already turns
every confirm-tier call into a silent deny. But suppression only covers the
confirm tier: quiet-tier tools — allowlisted shell prefixes, connector
reads-and-not-only-reads, every in-process tool — would still run silently.

This guard closes that gap. While unattended mode is engaged, a turn is a
witness, not an agent: it may observe through read-only tools (which is what
read-back verification stands on), and it may write Ciel's own notebook —
memory and projects, the internal state reflection has always maintained,
never confirm-gated — but nothing outside the notebook changes. Messages,
timers, files, shell, searches, and unlisted connector tools deny with a
reason the model can learn from mid-turn.

Like confirmation suppression, the mode is ambient — a depth counter the
pipeline holds around the turn, no turn-type plumbing in the Brain — and the
guard is a PreToolUse hook, because ``allowed_tools`` auto-approves before
``can_use_tool`` would ever run (see ``agent.py``). It sits *first* in the
hook chain: a Witness deny should land before a confirm guard consults the
suppressed broker and before the recorder considers a call that won't run.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from claude_agent_sdk import HookContext, HookMatcher

from ciel.brain.shellguard import deny_decision

if TYPE_CHECKING:
    from ciel.config import Config

log = logging.getLogger(__name__)

# Read-only built-ins. The workspace guard still applies on top, so "look
# anywhere" really means "look anywhere the attended turns could".
_BUILTIN_OBSERVERS = ("Read", "Glob", "Grep")

# In-process tools that only look.
_CIEL_OBSERVERS = (
    "recall", "recent_actions", "list_timers", "read_messages",
    "find_contact", "open_project",
)

# The notebook: Ciel's own memory and project state. Internal, never
# confirm-gated, and reflection's entire purpose — the Witness rule is scoped
# to the outside world, not to these.
_CIEL_NOTEBOOK = ("remember", "forget", "update_project", "log_progress")

_DENY_REASON = (
    "This is an unattended turn — you are a witness, not an agent. You may "
    "observe with read-only tools and update your own memory and projects, "
    "but actions on the outside world need the user present to confirm. "
    "Do not retry; note what you wanted to do and move on."
)


class UnattendedMode:
    """Ambient flag for turns with nobody listening.

    A depth counter rather than a boolean, for the same reason the confirm
    broker's suppression is one: nesting and exceptions can never leave the
    mode stuck engaged — and a stuck Witness would mute half the toolbox for
    every later conversation.

    Each engagement carries a label ("reflection", "proactive") so writes
    made during the turn can record *which kind* of unattended turn produced
    them — a fact distilled from a conversation the user was part of carries
    different weight than one observed with nobody around, and the memory
    store stamps the difference (see ``memory/store.py``).
    """

    def __init__(self) -> None:
        self._labels: list[str] = []

    @property
    def engaged(self) -> bool:
        return bool(self._labels)

    @property
    def context(self) -> str | None:
        """The innermost engagement's label, or None when attended."""
        return self._labels[-1] if self._labels else None

    @contextmanager
    def engage(self, label: str = "unattended") -> Iterator[None]:
        self._labels.append(label)
        try:
            yield
        finally:
            self._labels.pop()


def witness_allowed(config: "Config") -> frozenset[str]:
    """The tools an unattended turn may call.

    Built-ins by their plain names, in-process tools fully qualified, plus
    any connector tool an ``[mcp.<name>]`` entry lists under ``unattended``
    — the operator's declaration that the tool is read-only enough for a
    turn nobody supervises. Everything absent denies; a new tool is denied
    unattended until someone decides otherwise, which is the right default
    for a list that exists to bound an unsupervised model.
    """
    allowed: set[str] = set(_BUILTIN_OBSERVERS)
    allowed.update(f"mcp__ciel__{name}" for name in (*_CIEL_OBSERVERS, *_CIEL_NOTEBOOK))
    for name, entry in config.mcp.items():
        allowed.update(f"mcp__{name}__{tool}" for tool in entry.unattended)
    return frozenset(allowed)


class WitnessGuard:
    """PreToolUse hook enforcing the Witness rule while the mode is engaged."""

    def __init__(self, mode: UnattendedMode, allowed: frozenset[str]) -> None:
        self._mode = mode
        self._allowed = allowed

    async def __call__(
        self,
        payload: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        if not self._mode.engaged:
            return {}
        tool_name = payload.get("tool_name", "")
        if tool_name in self._allowed:
            return {}
        log.info("witness rule denied %s in an unattended turn", tool_name)
        return deny_decision(_DENY_REASON)

    def as_hooks(self) -> dict[str, list[HookMatcher]]:
        """An unmatched PreToolUse entry, like the connector gate — one guard
        covers every tool. Short timeout, unlike the confirm gates: this
        hook never waits on a human, so anything slow here is a bug."""
        return {"PreToolUse": [HookMatcher(hooks=[self], timeout=10)]}


__all__ = ["UnattendedMode", "WitnessGuard", "witness_allowed"]
