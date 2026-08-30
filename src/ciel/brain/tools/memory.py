"""Memory tools — how Ciel reads and writes its own long-term memory.

The tool *descriptions* matter as much as the code. They are the only thing
telling Ciel when to reach for these, and an unprompted-save habit is the
difference between a memory system that works and one that sits empty because
nobody ever says the words "remember this".
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from claude_agent_sdk import tool

from ciel.memory.store import VALID_KINDS, MemoryStore

log = logging.getLogger(__name__)

# Set once at startup by :func:`bind_store`. The SDK's @tool decorator
# wants plain module-level functions, so the store is bound here rather than
# threaded through every call.
_store: MemoryStore | None = None

# Write-provenance provider, bound by the pipeline: answers "what kind of
# turn is writing right now?" — "conversation" when a user is part of it
# (live turns and reflection over one), "proactive" when an unattended turn
# observed it with nobody around. A callable, not a value, because the answer
# changes turn to turn while the tool binding happens once.
_context: Callable[[], str] = lambda: "conversation"


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "remember",
    (
        "Save something about the user to long-term memory, so you still know it "
        "in future conversations. Use this whenever you learn something durable: "
        "their name, where they live, what they are working on, how they like you "
        "to respond, people and projects they mention repeatedly. Save it as soon "
        "as you learn it — do not wait to be asked to remember.\n\n"
        "Save ONE fact per call. If the user tells you their name, their current "
        "project, and a preference all in one breath, that is three separate "
        "calls, not one combined memory. Each is recalled, updated, and forgotten "
        "independently, so a memory holding three unrelated facts cannot be "
        "corrected when only one of them changes.\n\n"
        "The description is a short summary line that will appear in your system "
        "prompt — make it specific enough to recognize later ('User's name is "
        "Choco', not 'user info'). The content holds the detail.\n\n"
        "Facts say who the user is and what is true; kind 'procedure' says HOW "
        "a class of task should be done for this user ('how the morning brief "
        "is structured'). Name a procedure for the task class, not today's "
        "instance, and update it in place as the method improves — saving with "
        "the same description overwrites.\n\n"
        "Do not save one-off details that will not matter later, anything told to "
        "you in confidence for this conversation only, or passwords, keys, and "
        "credentials of any kind. Do not save negative claims about your own "
        "tools ('the calendar tool is broken') — a transient failure saved as "
        "fact hardens into a refusal you will cite long after it is fixed; if "
        "something failed and a workaround succeeded, save the workaround."
    ),
    {"description": str, "content": str, "kind": str},
)
async def remember(args: dict[str, Any]) -> dict[str, Any]:
    """description: a short summary line. content: the full fact. kind: one of
    identity, preference, project, fact, reference, procedure."""
    if _store is None:
        return _text("Memory is not available right now.")

    description = (args.get("description") or "").strip()
    content = (args.get("content") or "").strip()
    if not description or not content:
        return _text("Both a description and content are needed to save a memory.")

    kind = (args.get("kind") or "fact").strip().lower()
    if kind not in VALID_KINDS:
        kind = "fact"

    memory = _store.write(description, content, kind, context=_context())
    return _text(f"Saved as '{memory.name}'.")


@tool(
    "recall",
    (
        "Read the full content of things you have remembered about the user. The "
        "summaries in your system prompt tell you what you know; use this to load "
        "the details of a specific one when it becomes relevant. Search by topic, "
        "not by exact wording.\n\n"
        "A memory is what was true when it was saved — it is NOT evidence about "
        "the current state of anything outside this store. If a live source can "
        "be checked (messages, calendar, files, the screen), check it before "
        "asserting, and never conclude that something is absent or unchanged "
        "from memory alone. Recalling is 'I recall', not 'I checked' — say "
        "which one you did."
    ),
    {"query": str},
)
async def recall(args: dict[str, Any]) -> dict[str, Any]:
    if _store is None:
        return _text("Memory is not available right now.")

    query = (args.get("query") or "").strip()
    if not query:
        return _text("A search query is needed.")

    matches = _store.search(query)
    if not matches:
        return _text(f"Nothing remembered about '{query}'.")

    parts = []
    for memory in matches:
        label = memory.kind
        if memory.context == "proactive":
            # A fact no user ever heard or confirmed carries a hedge into
            # the model's context, deterministically — the runtime knows the
            # provenance, so the runtime says it.
            label += ", noted unattended — unconfirmed by the user"
        parts.append(f"## {memory.description}\n({label})\n\n{memory.content}")
    return _text("\n\n---\n\n".join(parts))


@tool(
    "forget",
    (
        "Delete something from long-term memory. Use this when the user asks you "
        "to forget something, or when you learn that something you saved was "
        "wrong and a corrected version has replaced it."
    ),
    {"name": str},
)
async def forget(args: dict[str, Any]) -> dict[str, Any]:
    if _store is None:
        return _text("Memory is not available right now.")

    name = (args.get("name") or "").strip()
    if not name:
        return _text("A memory name is needed.")

    if _store.forget(name):
        return _text(f"Forgotten: {name}.")
    return _text(f"No memory named '{name}'. Check the list in your system prompt.")


def bind_store(store: MemoryStore) -> None:
    """Attach the store these tools operate on."""
    global _store
    _store = store


def bind_context(provider: Callable[[], str]) -> None:
    """Attach the write-provenance provider (see ``_context`` above)."""
    global _context
    _context = provider


MEMORY_TOOLS = [remember, recall, forget]

__all__ = [
    "MEMORY_TOOLS", "bind_context", "bind_store", "remember", "recall", "forget",
]
