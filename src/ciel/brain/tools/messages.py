"""iMessage tools — reading texts, resolving names, sending.

The descriptions carry most of the safety here. Ciel acts on transcribed
speech, so "text Sarah I'm running late" arrives already one lossy step away
from what was said, and a sent message cannot be recalled. The rule the
descriptions enforce is: resolve the name, say the recipient and the words out
loud, get a yes, then send.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import tool

from ciel.messages import MessagesClient, MessagesUnavailable

log = logging.getLogger(__name__)

# Bound once at startup by :func:`bind_client`, for the same reason the memory
# tools bind a store: the SDK's @tool decorator wants module-level functions.
_client: MessagesClient | None = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "find_contact",
    (
        "Look up how to reach someone by name, returning every phone number and "
        "email that matches. ALWAYS call this before send_message — never guess "
        "or reconstruct a phone number.\n\n"
        "If it returns more than one match, do not pick one. Ask the user which "
        "person they meant, out loud, naming them. Heard names are unreliable: "
        "'Sarah' and 'Sara' sound identical, and sending to the wrong one cannot "
        "be undone."
    ),
    {"name": str},
)
async def find_contact(args: dict[str, Any]) -> dict[str, Any]:
    if _client is None:
        return _text("Messages access is not available right now.")

    name = (args.get("name") or "").strip()
    if not name:
        return _text("A name to look up is needed.")

    try:
        contacts = await _client.find_contacts(name)
    except MessagesUnavailable as exc:
        return _text(f"Could not look that up: {exc}")

    if not contacts:
        return _text(
            f"No contact matching '{name}'. Ask the user for the number or "
            "email directly."
        )
    if len(contacts) == 1:
        return _text(f"One match: {contacts[0].describe()}")

    lines = "\n".join(f"- {contact.describe()}" for contact in contacts)
    return _text(
        f"{len(contacts)} matches for '{name}' — ask the user which one before "
        f"sending:\n{lines}"
    )


@tool(
    "read_messages",
    (
        "Read recent iMessages, oldest first. With no sender, returns the latest "
        "across all conversations; with a sender's phone number or email, returns "
        "just that conversation.\n\n"
        "This is the user's private message history and it is being read aloud in "
        "a room. Summarize rather than reciting everything — say who texted and "
        "what they wanted, and read a message verbatim only when asked to."
    ),
    {"sender": str, "limit": int},
)
async def read_messages(args: dict[str, Any]) -> dict[str, Any]:
    if _client is None:
        return _text("Messages access is not available right now.")

    sender = (args.get("sender") or "").strip() or None
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20

    try:
        messages = await _client.recent(limit=limit, handle=sender)
    except MessagesUnavailable as exc:
        return _text(f"Could not read messages: {exc}")

    if not messages:
        where = f" from {sender}" if sender else ""
        return _text(f"No messages{where}.")

    return _text("\n".join(message.describe() for message in messages))


@tool(
    "send_message",
    (
        "Send an iMessage. This is irreversible — the message arrives on someone "
        "else's phone immediately and cannot be recalled.\n\n"
        "Required sequence, every time:\n"
        "1. Call find_contact to turn the name into a phone number or email.\n"
        "2. Say the recipient's name AND the exact message text out loud to the "
        "user, then stop and wait for them to confirm.\n"
        "3. Only after they say yes, call this with the resolved handle.\n\n"
        "Never send to a name — 'to' must be a phone number or email address. "
        "Never send to a group chat: group sends are broken on current macOS and "
        "fail silently, reporting success while the message goes nowhere. If the "
        "user asks for a group, tell them that instead of trying."
    ),
    {"to": str, "text": str},
)
async def send_message(args: dict[str, Any]) -> dict[str, Any]:
    if _client is None:
        return _text("Messages access is not available right now.")

    to = (args.get("to") or "").strip()
    body = (args.get("text") or "").strip()
    if not to or not body:
        return _text("Both a recipient and a message are needed.")

    try:
        await _client.send(to, body)
    except MessagesUnavailable as exc:
        return _text(f"Not sent: {exc}")
    except Exception as exc:  # noqa: BLE001 - a failed send must be reported, not raised
        log.exception("send failed")
        return _text(f"Not sent — something went wrong: {exc}")

    return _text(
        f"Sent to {to}. Tell the user it went, and read the message back only if "
        "they ask."
    )


def bind_client(client: MessagesClient) -> None:
    """Attach the client these tools operate on."""
    global _client
    _client = client


READ_TOOLS = [find_contact, read_messages]
SEND_TOOLS = [send_message]
MESSAGE_TOOLS = [*READ_TOOLS, *SEND_TOOLS]

__all__ = [
    "MESSAGE_TOOLS",
    "READ_TOOLS",
    "SEND_TOOLS",
    "bind_client",
    "find_contact",
    "read_messages",
    "send_message",
]
