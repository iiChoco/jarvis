"""The connector gate: spoken confirmation for outward-acting MCP tools.

Codename: **Tower Clearance**, ground-crew division — the connectors service
the aircraft, but departures still clear through the tower.

Connector tools run under ``permission_mode="dontAsk"``, which is fine for
reading a calendar and indefensible for sending mail off a misheard sentence.
The ``confirm`` list on an ``[mcp.<name>]`` entry names the tools that are
allowed but not trusted to run silently: this guard fronts them with the same
voice gate as shell commands — the hook blocks, the user hears what is about
to happen, and only a spoken yes lets the call through.

Unlike the shell gate there is no classifier here: the config already did the
classifying. ``tools`` is the quiet tier, ``confirm`` is the confirm tier, and
anything on neither list was never allowed at all. What this guard adds is the
*question* — a tool call is a name and a dict of arguments, and "mcp underscore
gmail underscore send email" read aloud is not a question anyone can answer.
The formatters below turn the calls we expect into something a listener can
actually judge ("Send an email to sam at gmail dot com, subject Lunch"), with
a plain fallback for tools nobody wrote a formatter for. Formatters are
best-effort by design: a missing field degrades the sentence, never the gate —
the deciding voice is the user's, not the formatter's.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from claude_agent_sdk import HookContext, HookMatcher

from ciel.brain.shellguard import deny_decision

log = logging.getLogger(__name__)

_MAX_SPOKEN_FIELD = 80  # a subject or title longer than this is truncated


def _shorten(value: Any) -> str:
    text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    text = " ".join(text.split())  # newlines in a subject read as silence
    return text if len(text) <= _MAX_SPOKEN_FIELD else f"{text[:_MAX_SPOKEN_FIELD]}…"


def _describe_send_email(args: dict[str, Any]) -> str:
    to = args.get("to")
    subject = args.get("subject")
    parts = ["Send an email"]
    if to:
        parts.append(f"to {_shorten(to)}")
    if subject:
        parts.append(f"with the subject {_shorten(subject)}")
    return " ".join(parts)


def _describe_event(verb: str) -> Callable[[dict[str, Any]], str]:
    def describe(args: dict[str, Any]) -> str:
        title = args.get("summary") or args.get("title")
        start = args.get("start")
        parts = [f"{verb} a calendar event"]
        if title:
            parts.append(f"called {_shorten(title)}")
        if start:
            parts.append(f"starting {_shorten(start)}")
        return " ".join(parts)

    return describe


def _describe_respond(args: dict[str, Any]) -> str:
    response = args.get("response") or args.get("responseStatus") or "reply"
    return f"Respond {_shorten(response)} to a calendar invitation"


# Keyed by the *unprefixed* tool name, matching what goes in a `confirm` list.
# The dict is small on purpose: only tools someone chose to gate need a voice,
# and the fallback below keeps unlisted ones askable, just less gracefully.
_FORMATTERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "send_email": _describe_send_email,
    "create-event": _describe_event("Create"),
    "update-event": _describe_event("Update"),
    "delete-event": _describe_event("Delete"),
    "respond-to-event": _describe_respond,
}


def describe_call(qualified: str, args: dict[str, Any]) -> str:
    """A speakable question for one gated tool call, ending in "— okay?"."""
    parts = qualified.split("__", 2)
    server, tool = (parts[1], parts[2]) if len(parts) == 3 else ("", qualified)
    formatter = _FORMATTERS.get(tool)
    if formatter is not None:
        try:
            return f"{formatter(args)} — okay?"
        except Exception:  # noqa: BLE001 - a bad formatter must not kill the ask
            log.debug("formatter for %s failed", tool, exc_info=True)
    humanized = tool.replace("_", " ").replace("-", " ")
    suffix = f" through {server}" if server else ""
    return f"{humanized.capitalize()}{suffix} — okay?"


class ConfirmToolGuard:
    """PreToolUse hook holding listed connector tools for a spoken yes."""

    def __init__(
        self,
        gated: frozenset[str],
        confirm: Callable[[str], Awaitable[bool]],
    ) -> None:
        self._gated = gated
        self._confirm = confirm

    async def __call__(
        self,
        payload: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = payload.get("tool_name", "")
        if tool_name not in self._gated:
            return {}

        question = describe_call(tool_name, payload.get("tool_input") or {})
        try:
            approved = await self._confirm(question)
        except Exception:  # noqa: BLE001 - the hook must always return a decision
            log.exception("confirmation failed — denying %s", tool_name)
            approved = False
        if approved:
            log.info("user approved %s", tool_name)
            return {}
        log.info("user declined %s", tool_name)
        return deny_decision(
            "The user was asked out loud and declined, or didn't answer. Do "
            "not retry the call; acknowledge briefly and move on."
        )

    def as_hooks(self) -> dict[str, list[HookMatcher]]:
        """An unmatched PreToolUse entry — membership is checked in-call.

        No ``matcher`` because one guard covers tools across servers, and the
        same generous timeout as the shell gate: a confirmation legitimately
        blocks for the question plus the answer window plus one retry.
        """
        return {"PreToolUse": [HookMatcher(hooks=[self], timeout=120)]}


__all__ = ["ConfirmToolGuard", "describe_call"]
