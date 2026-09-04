"""Mail as Ciel — one tool, one address, behind the spoken-yes gate.

The Gmail connector sends *as the user*; this sends *as Ciel*, from the
address in ``[mail]``. The description carries the etiquette (whose voice
a message is in), the gate carries the safety: ``send_as_ciel`` sits in
the agent's confirm set, so the call is read aloud — recipient and subject
— and runs only on the user's yes, and it is absent from the Witness
observers, so an unattended turn can never send. The recipient is the
model's proposal and the user's decision; the From is never a choice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import tool

from ciel.mail import Mailer, MailUnavailable

if TYPE_CHECKING:
    from ciel.config import MailConfig

log = logging.getLogger(__name__)

_sender: Mailer | None = None
_config: "MailConfig | None" = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "send_as_ciel",
    (
        "Send an email from your own address — Ciel's, not the user's. This is "
        "irreversible: it arrives in someone's inbox immediately and cannot be "
        "recalled.\n\n"
        "Use it for mail that is genuinely yours: a note the user asked you to "
        "send them (\"email me the list\"), a message you are sending on your "
        "own behalf, anything a reader should see as coming from an assistant "
        "rather than from the user. Mail that should carry the user's name and "
        "voice goes through their own email tool instead, never this one.\n\n"
        "Before calling: say the recipient and the subject out loud, and the "
        "gist of the body, then wait for the user's yes. `to` must be an email "
        "address; when the user says \"email me\", it is their own address, "
        "which you know. Plain text only — it is read in a mail client, so "
        "write in full sentences, no markdown."
    ),
    {"to": str, "subject": str, "body": str},
)
async def send_as_ciel(args: dict[str, Any]) -> dict[str, Any]:
    if _sender is None or _config is None:
        return _text("Your own email address is not set up right now.")

    to = (args.get("to") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()
    if not to or not subject or not body:
        return _text("A recipient, a subject, and a body are all needed.")
    if "@" not in to or " " in to:
        return _text(f"'{to}' is not an email address — resolve it first.")

    try:
        await asyncio.to_thread(_sender.send, to, subject, body, _config.address)
    except MailUnavailable as exc:
        return _text(f"Not sent: {exc}")
    except Exception as exc:  # noqa: BLE001 - a failed send must be reported, not raised
        log.exception("send_as_ciel failed")
        return _text(f"Not sent — something went wrong: {exc}")

    return _text(
        f"Sent to {to} from {_config.address}. Tell the user it went; read "
        "the body back only if they ask."
    )


def bind_mail(sender: Mailer, config: "MailConfig") -> None:
    """Attach the relay and the address this tool sends from."""
    global _sender, _config
    _sender = sender
    _config = config


MAIL_TOOLS = [send_as_ciel]

__all__ = ["MAIL_TOOLS", "bind_mail", "send_as_ciel"]
