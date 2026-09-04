"""Sending mail through an SMTP relay — Ciel's own address.

Where ``gmail.py`` sends *as the user* by borrowing the Gmail connector,
this sends *as Ciel*: a From address on a domain the relay is authorized
for (``ciel@example.com`` through Cloudflare Email Service's SMTP endpoint,
where the username is the literal ``api_token`` and the password is an
API token with the Email Sending permission). Cloudflare relays to the
account's verified destination addresses free of charge, which is exactly
the "email me" case; anything wider needs their paid plan.

Implicit TLS only (SMTPS on 465 — the relay speaks no STARTTLS), stdlib
``smtplib``, synchronous so callers thread it. The recipient and sender
are pinned in config by the user; nothing here chooses either at send
time. ``MailUnavailable`` is the one failure shape both senders share, so
the watcher degrades the same way whichever is wired.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import Protocol

log = logging.getLogger(__name__)

_SMTP_TIMEOUT_S = 20.0


class MailUnavailable(RuntimeError):
    """The relay refused, the token is wrong, or the network is down."""


class Mailer(Protocol):
    """What the watcher needs from a sender — either implementation."""

    def send(self, to: str, subject: str, body: str, sender: str = "") -> str: ...


class SmtpSender:
    """Plain-text mail over SMTPS with a static login."""

    def __init__(
        self, host: str, port: int, user: str, token: str, copy_to: str = ""
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._token = token
        self._copy_to = copy_to
        """Bcc on every message — the sender's own record. smtplib strips
        the header before the wire, so the recipient never sees it."""

    def available(self) -> bool:
        return bool(self._token and self._host)

    def send(self, to: str, subject: str, body: str, sender: str = "") -> str:
        """Send one message; returns the Message-ID stamped on it."""
        if not to or not sender:
            raise MailUnavailable("SMTP sending needs both a recipient and a From address")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = to
        if self._copy_to and self._copy_to.lower() != to.lower():
            message["Bcc"] = self._copy_to
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP_SSL(
                self._host, self._port,
                timeout=_SMTP_TIMEOUT_S, context=ssl.create_default_context(),
            ) as relay:
                relay.login(self._user, self._token)
                refused = relay.send_message(message) or {}
        except smtplib.SMTPAuthenticationError as exc:
            raise MailUnavailable(f"the SMTP relay rejected the token ({exc.smtp_code})") from exc
        except smtplib.SMTPException as exc:
            raise MailUnavailable(f"the SMTP relay refused the message ({exc})") from exc
        except (OSError, socket.timeout) as exc:
            raise MailUnavailable(f"the SMTP relay could not be reached ({exc})") from exc
        # A partial refusal comes back as a dict, not an exception: the
        # recipient refused is a failed send; only the copy refused is a
        # delivered message with a missing record, worth a line in the log.
        if any(r.lower() == to.lower() for r in refused):
            code, reason = next(iter(refused.values()))
            raise MailUnavailable(f"the relay refused {to} ({code} {reason!r})")
        for address, (code, reason) in refused.items():
            log.warning("mail: the copy to %s was refused (%s %r)", address, code, reason)
        return str(message.get("Message-ID", ""))


__all__ = ["MailUnavailable", "Mailer", "SmtpSender"]
