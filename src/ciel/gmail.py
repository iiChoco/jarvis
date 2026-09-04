"""Sending mail as the user — by borrowing the Gmail connector's login.

The brain sends mail through the ``[mcp.gmail]`` connector, which saved a
refresh token in ``~/.gmail-mcp/credentials.json`` when it was authorized.
Deterministic code (a watcher that must email without a model in the
loop) needs the same power, and the honest way to get it is the way the
Google Calendar watcher gets its calendar: read the connector's refresh
token, mint access tokens in memory, never write back — two writers of
one token file is how refresh races start.

One capability, deliberately narrow: send a plain-text message. The
recipient defaults to the signed-in account itself ("email me"), read
from the Gmail profile and cached; any other address is pinned in config
by the user, never chosen at send time. Synchronous urllib, so callers
thread it.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from ciel.mail import MailUnavailable

log = logging.getLogger(__name__)

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_HTTP_TIMEOUT_S = 15.0
_TOKEN_SKEW_S = 60.0


class GmailUnavailable(MailUnavailable):
    """The connector's tokens are missing, revoked, or Google refused."""


class GmailSender:
    """Sends as whoever authorized the Gmail connector."""

    def __init__(self, keys_file: Path, token_file: Path) -> None:
        self._keys_file = keys_file
        self._token_file = token_file
        self._access_token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()
        self._own_address: str | None = None

    def available(self) -> bool:
        """Whether the connector has ever been authorized — a file peek,
        no network, so the watcher can decide at construction."""
        try:
            saved = json.loads(self._token_file.read_text())
            keys = json.loads(self._keys_file.read_text())["installed"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return False
        return bool(saved.get("refresh_token") and keys.get("client_id"))

    def own_address(self) -> str:
        """The signed-in account's address — the default "me"."""
        if self._own_address is None:
            profile = self._request("GET", f"{_API}/profile")
            address = profile.get("emailAddress")
            if not isinstance(address, str) or "@" not in address:
                raise GmailUnavailable("the Gmail profile has no address")
            self._own_address = address
        return self._own_address

    def send(self, to: str, subject: str, body: str, sender: str = "") -> str:
        """Send one plain-text message; returns Gmail's message id.

        ``sender`` sets the From header — a "send mail as" alias the account
        has verified (an unverified one is silently rewritten by Gmail to
        the account's own address, so a typo degrades, never fails).
        Empty leaves Gmail to stamp the primary address."""
        message = EmailMessage()
        message["To"] = to or self.own_address()
        if sender:
            message["From"] = sender
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        sent = self._request("POST", f"{_API}/messages/send", {"raw": raw})
        return str(sent.get("id", ""))

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _request(self, method: str, url: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token()}",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
                raw = response.read().decode("utf-8")
                # Gmail answers some calls (an empty filter list, a batch
                # modify) with 204 and no body; that is success, not JSON.
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise GmailUnavailable(f"Gmail answered {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GmailUnavailable(f"Gmail could not be reached ({exc})") from exc

    def _token(self) -> str:
        with self._lock:
            if (
                self._access_token is not None
                and time.time() < self._token_expiry - _TOKEN_SKEW_S
            ):
                return self._access_token
            try:
                keys = json.loads(self._keys_file.read_text())["installed"]
                refresh = json.loads(self._token_file.read_text())["refresh_token"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise GmailUnavailable(
                    "the Gmail connector is not authorized — authorize "
                    "[mcp.gmail] once and the tokens are picked up"
                ) from exc
            body = urllib.parse.urlencode({
                "client_id": keys["client_id"],
                "client_secret": keys["client_secret"],
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            }).encode("ascii")
            request = urllib.request.Request(
                keys.get("token_uri", "https://oauth2.googleapis.com/token"),
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
                    granted = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise GmailUnavailable(f"Gmail token refresh failed ({exc})") from exc
            self._access_token = granted["access_token"]
            self._token_expiry = time.time() + float(granted.get("expires_in", 3600))
            return self._access_token


__all__ = ["GmailSender", "GmailUnavailable"]
