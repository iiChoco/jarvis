"""Hooks that feed the action journal.

Codename: **Inverse**, construction side — what records enough to invert.

Two hooks around every watched tool call: PreToolUse snapshots the target of
a file edit *before* it changes, PostToolUse writes the journal entry once the
result exists — which is what captures the ids an undo needs (the eventId a
create-event returns lives only in the response). Only calls that actually ran
are journaled: a denied call has no PostToolUse, and an undo log padded with
things that never happened would send the model undoing fiction.

This recorder never denies anything — it observes. Keeping it separate from
the guards keeps that property legible: nothing in here can be talked into
becoming an enforcement path, and nothing in the guards grows a side effect.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import HookContext, HookMatcher

from ciel.journal import ActionJournal

log = logging.getLogger(__name__)

# Tools that mutate the filesystem, and where their target path lives.
_FILE_MUTATORS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}


def _response_text(response: Any) -> str | None:
    """Flatten a tool response into journal-worthy text, best-effort.

    Responses arrive in several shapes (a string, a content-block dict, a
    list of blocks); an undo needs the text inside — that's where a created
    event's id comes back — not the envelope.
    """
    if response is None:
        return None
    if isinstance(response, str):
        return response or None
    if isinstance(response, dict):
        return _response_text(response.get("content") or response.get("text"))
    if isinstance(response, list):
        parts = [
            text for item in response
            if (text := _response_text(item)) is not None
        ]
        return "\n".join(parts) or None
    return str(response)


class ActionRecorder:
    """Observes watched tool calls and journals what actually happened."""

    def __init__(
        self,
        journal: ActionJournal,
        watched: frozenset[str],
        verify_emitter: "Callable[[str, dict[str, Any]], None] | None" = None,
        verify_for: frozenset[str] | None = None,
    ) -> None:
        self._journal = journal
        self._watched = frozenset(watched) | set(_FILE_MUTATORS) | {"Bash"}
        # Read-back verification (Vigil): the confirm-gated tools are the
        # outward-acting ones whose success is worth independently observing,
        # so a completed call to one also emits a verify event. Only those —
        # file edits and shell have the journal and undo; a send that
        # "returned without error" is the weaker claim the wishlist called
        # out. The emitter observes like everything else here: it can note
        # that an action deserves checking, never affect the action.
        # ``verify_for`` narrows the set when a watched tool verifies itself
        # in-band (the capability grants parse-verify their own edit) —
        # journaled like everything, but no read-back event filed.
        self._verify_for = (
            frozenset(verify_for) if verify_for is not None else frozenset(watched)
        )
        self._verify_emitter = verify_emitter
        # Snapshots taken in the pre-hook, claimed by the post-hook, keyed by
        # tool_use_id. Bounded: a call denied by a guard never reaches the
        # post-hook, and its pending entry must not accumulate forever.
        self._pending: OrderedDict[str, tuple[str | None, str | None]] = OrderedDict()

    async def before(
        self,
        payload: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = payload.get("tool_name", "")
        path_field = _FILE_MUTATORS.get(tool_name)
        if path_field is not None and tool_use_id is not None:
            raw = (payload.get("tool_input") or {}).get(path_field)
            if raw:
                self._pending[tool_use_id] = self._journal.snapshot(Path(str(raw)))
                while len(self._pending) > 32:
                    self._pending.popitem(last=False)
        return {}

    async def after(
        self,
        payload: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = payload.get("tool_name", "")
        if tool_name not in self._watched:
            return {}
        snapshot, note = (
            self._pending.pop(tool_use_id, (None, None))
            if tool_use_id is not None
            else (None, None)
        )
        try:
            self._journal.record(
                tool=tool_name,
                args=payload.get("tool_input") or {},
                response=_response_text(payload.get("tool_response")),
                snapshot=snapshot,
                note=note,
            )
        except Exception:  # noqa: BLE001 - observation must never fail the call
            log.warning("could not journal %s", tool_name, exc_info=True)
        if self._verify_emitter is not None and tool_name in self._verify_for:
            # PostToolUse means the call actually ran, and under the Witness
            # rule these tools deny unattended — so this only ever fires for
            # actions a present user approved.
            try:
                self._verify_emitter(tool_name, payload.get("tool_input") or {})
            except Exception:  # noqa: BLE001 - observation must never fail the call
                log.warning("verify emitter failed", exc_info=True)
        return {}

    def as_hooks(self) -> dict[str, list[HookMatcher]]:
        """Pre and post matcher lists; the caller merges them with the guards.

        The pre-hook belongs *after* the guards in the merged list, so when
        the SDK short-circuits on a denial nothing gets snapshotted for a
        call that never ran (and if it doesn't short-circuit, the journal's
        orphan pruning collects the stray).
        """
        return {
            "PreToolUse": [HookMatcher(hooks=[self.before])],
            "PostToolUse": [HookMatcher(hooks=[self.after])],
        }


__all__ = ["ActionRecorder"]
