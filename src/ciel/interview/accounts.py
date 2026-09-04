"""Who may enter the room: accounts, passwords, and the login cookie.

The owner creates every account; there is no signup. Passwords are
generated, never chosen — four words and two digits, shown once — so the
room never holds a password anyone reuses elsewhere. What it stores is a
scrypt hash and a salt per user, in one JSON file under the room's
directory, written atomically and named on the personal brain's forbidden
list.

A login is a cookie: ``username|expiry|hmac``, signed with a secret the
room mints once (owner-only file, like the hub token). Stateless on
purpose — no session table to expire, and a restart logs nobody out.
Disabling an account still takes effect at once, because every request
looks the username up after verifying the signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ciel.memory.store import atomic_write

log = logging.getLogger(__name__)

ACCOUNTS_FILE = "interview-accounts.json"
SECRET_FILE = "interview.secret"

USERNAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
"""Lowercase, digits, underscore, dash; 2-32 characters. Usernames ride
in the cookie and name a directory, so they are kept boring."""

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

# Short, distinct, easy to say over the phone. Four of these plus two
# digits is ~55 bits, and nobody types it twice: the browser remembers.
_WORDS = (
    "amber", "birch", "cedar", "delta", "ember", "fjord", "glade", "harbor",
    "island", "juniper", "kestrel", "lantern", "meadow", "north", "orchid",
    "pebble", "quartz", "river", "summit", "timber", "umber", "valley",
    "willow", "yonder", "zenith", "anchor", "beacon", "canyon", "dune",
    "falcon", "garnet", "heron", "ivory", "jasper", "koi", "lumen", "marble",
    "nectar", "oak", "prairie", "quill", "ridge", "sable", "tundra", "violet",
)


@dataclass(frozen=True)
class Account:
    username: str
    role: str
    created: str
    disabled: bool = False
    last_seen: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


MIN_PASSWORD = 8
MAX_PASSWORD = 128


def check_password(password: str) -> None:
    """A chosen password must be at least eight characters. Nothing more
    clever: length is the rule that survives contact with people."""
    if not isinstance(password, str) or not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise ValueError(f"password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters")


def generate_password() -> str:
    words = "-".join(secrets.choice(_WORDS) for _ in range(4))
    return f"{words}-{secrets.randbelow(90) + 10}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


class Accounts:
    """The account file, read on demand and written atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"users": {}}
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s) — treating as empty", self._path, exc)
            return {"users": {}}
        users = data.get("users")
        return {"users": users if isinstance(users, dict) else {}}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            # Create owner-only before the first write lands.
            self._path.touch(mode=0o600)
        atomic_write(self._path, json.dumps(data, indent=2, sort_keys=True) + "\n")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _account(name: str, raw: dict[str, Any]) -> Account:
        return Account(
            username=name,
            role=str(raw.get("role", "user")),
            created=str(raw.get("created", "")),
            disabled=bool(raw.get("disabled", False)),
            last_seen=raw.get("last_seen"),
        )

    # ── reads ────────────────────────────────────────────────────────────────

    def get(self, username: str) -> Account | None:
        raw = self._load()["users"].get(username)
        return self._account(username, raw) if isinstance(raw, dict) else None

    def list(self) -> list[Account]:
        users = self._load()["users"]
        return sorted(
            (self._account(n, r) for n, r in users.items() if isinstance(r, dict)),
            key=lambda a: a.username,
        )

    @property
    def empty(self) -> bool:
        return not self._load()["users"]

    def verify(self, username: str, password: str) -> Account | None:
        """The account when the password matches and the account is live;
        None otherwise, with the same work done either way."""
        raw = self._load()["users"].get(username)
        if not isinstance(raw, dict):
            # Burn the same time as a real check so a probe can't tell a
            # missing user from a wrong password by the clock.
            _hash(password, b"\0" * 16)
            return None
        try:
            salt = base64.b64decode(raw["salt"])
            stored = base64.b64decode(raw["scrypt"])
        except (KeyError, ValueError):
            return None
        if not hmac.compare_digest(_hash(password, salt), stored):
            return None
        account = self._account(username, raw)
        return None if account.disabled else account

    # ── writes ───────────────────────────────────────────────────────────────

    def create(
        self, username: str, role: str = "user", password: str | None = None
    ) -> str:
        """Add an account; returns the generated password, the only time
        it is ever visible. ``password`` pins one — for the loopback dev
        account only; the room never lets anyone choose theirs."""
        if not USERNAME.match(username):
            raise ValueError(
                "username must be 2-32 characters: lowercase letters, digits, "
                "underscore, dash"
            )
        if role not in ("user", "admin"):
            raise ValueError("role must be user or admin")
        data = self._load()
        if username in data["users"]:
            raise ValueError(f"account {username!r} already exists")
        if password is not None and username != "dev":
            check_password(password)
        password = password or generate_password()
        salt = secrets.token_bytes(16)
        data["users"][username] = {
            "scrypt": base64.b64encode(_hash(password, salt)).decode(),
            "salt": base64.b64encode(salt).decode(),
            "role": role,
            "created": _now(),
            "disabled": False,
            "last_seen": None,
        }
        self._save(data)
        return password

    def reset(self, username: str, password: str | None = None) -> str:
        """A new password for an existing account — chosen when given
        (and long enough), generated otherwise."""
        data = self._load()
        raw = data["users"].get(username)
        if not isinstance(raw, dict):
            raise KeyError(username)
        if password:
            if username != "dev":  # the loopback dev account keeps its short one
                check_password(password)
        else:
            password = generate_password()
        salt = secrets.token_bytes(16)
        raw["scrypt"] = base64.b64encode(_hash(password, salt)).decode()
        raw["salt"] = base64.b64encode(salt).decode()
        self._save(data)
        return password

    def set_disabled(self, username: str, disabled: bool) -> Account:
        data = self._load()
        raw = data["users"].get(username)
        if not isinstance(raw, dict):
            raise KeyError(username)
        raw["disabled"] = bool(disabled)
        self._save(data)
        return self._account(username, raw)

    def delete(self, username: str) -> None:
        data = self._load()
        if username not in data["users"]:
            raise KeyError(username)
        del data["users"][username]
        self._save(data)

    def touch(self, username: str) -> None:
        """Note a login. Best effort: a failed write is not a failed login."""
        try:
            data = self._load()
            raw = data["users"].get(username)
            if isinstance(raw, dict):
                raw["last_seen"] = _now()
                self._save(data)
        except OSError:
            log.debug("could not record last_seen for %s", username, exc_info=True)


# ── the cookie ───────────────────────────────────────────────────────────────


def mint_secret(path: Path) -> str:
    """Read the signing secret, or mint one owner-only."""
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secret + "\n")
    log.info("interview cookie secret minted at %s", path)
    return secret


def sign_cookie(secret: str, username: str, expires: float) -> str:
    body = f"{username}|{int(expires)}"
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{mac}"


def read_cookie(secret: str, value: str, now: float | None = None) -> str | None:
    """The username a cookie names, when its signature holds and it has
    not expired; None for anything else."""
    parts = (value or "").split("|")
    if len(parts) != 3:
        return None
    username, expires_s, mac = parts
    if not USERNAME.match(username):
        return None
    try:
        expires = int(expires_s)
    except ValueError:
        return None
    expected = hmac.new(
        secret.encode(), f"{username}|{expires}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    if (now if now is not None else time.time()) >= expires:
        return None
    return username


__all__ = [
    "ACCOUNTS_FILE",
    "MIN_PASSWORD",
    "check_password",
    "Account",
    "Accounts",
    "SECRET_FILE",
    "USERNAME",
    "generate_password",
    "mint_secret",
    "read_cookie",
    "sign_cookie",
]
