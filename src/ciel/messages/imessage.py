"""Reading and sending iMessage on macOS.

Two completely different mechanisms, and it matters which is which:

* **Reading** goes straight at ``~/Library/Messages/chat.db``, the SQLite
  database Messages.app keeps. Opened read-only, over a URI connection, so no
  bug here can write to it. This needs Full Disk Access granted to whatever
  runs Ciel — the terminal, or the app bundle — and there is no way around
  that short of asking Apple.

* **Sending** goes through Messages.app's AppleScript surface, driven by
  ``osascript``. There is no supported send API; this is the only door.

Neither half is a stable contract. The schema is Apple's private business and
has changed shape across releases (message text moved into an archived
``attributedBody`` blob, timestamps switched from seconds to nanoseconds), and
the AppleScript surface has been quietly narrowing for years. Everything here
is defensive on purpose: a schema surprise should cost a missing message, not
a crashed assistant.

Group sends are refused outright. On macOS 26 they fail *silently* — the send
reports success and the message lands nowhere, or as an empty SMS row — which
is the one failure mode a voice assistant must never have. One-to-one sends
still work.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ciel.config import MessagesConfig

log = logging.getLogger(__name__)

# Apple's reference date: 2001-01-01 UTC, in Unix seconds.
_APPLE_EPOCH = 978_307_200

# Timestamps are nanoseconds on any recent macOS and plain seconds on older
# databases. Anything past this magnitude is unambiguously nanoseconds — a
# seconds-value that large would be tens of thousands of years from now.
_NANOSECOND_FLOOR = 1e11


class MessagesUnavailable(RuntimeError):
    """The Messages database can't be read — missing, or no Full Disk Access."""


@dataclass(frozen=True, slots=True)
class Contact:
    """One reachable address for one person."""

    name: str
    kind: str  # "phone" or "email"
    handle: str

    def describe(self) -> str:
        return f"{self.name} ({self.kind} {self.handle})"


@dataclass(frozen=True, slots=True)
class Message:
    text: str
    from_me: bool
    handle: str | None
    chat: str | None
    when: datetime | None
    is_group: bool

    def describe(self) -> str:
        who = "you" if self.from_me else (self.chat or self.handle or "unknown")
        stamp = self.when.strftime("%a %-I:%M %p") if self.when else "unknown time"
        return f"[{stamp}] {who}: {self.text}"


# ── text extraction ──────────────────────────────────────────────────────────

# Printable runs inside the archived blob: ASCII, or a UTF-8 lead byte followed
# by continuation bytes. Four characters minimum, to skip the class names and
# short type markers the archiver interleaves with the actual string.
_TEXT_RUN = re.compile(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf]{3,}")


def _decode_attributed(blob: bytes | None) -> str:
    """Best-effort text recovery from an ``attributedBody`` blob.

    Modern Messages stores the body as an NSKeyedArchiver typedstream rather
    than plain text, and Python has no decoder for that format in the standard
    library. Rather than take a dependency for one column, this scans for the
    longest printable run after the ``NSString`` marker, which is the message
    body in every sample checked.

    Heuristic, and honest about it: it can return "" for an exotic message, and
    "" is treated everywhere here as "nothing to show" rather than as an error.
    """
    if not blob:
        return ""
    try:
        data = bytes(blob)
        start = data.find(b"NSString")
        if start == -1:
            return ""
        window = data[start + 8 :]
        # Attribute dictionaries follow the body; cutting there keeps font
        # names and style keys out of the "longest run" comparison.
        stop = window.find(b"NSDictionary")
        if stop != -1:
            window = window[:stop]
        runs = _TEXT_RUN.findall(window)
        if not runs:
            return ""
        return max(runs, key=len).decode("utf-8", "ignore").strip()
    except Exception:  # noqa: BLE001 - a private format; never fail a read over it
        log.debug("could not decode attributedBody", exc_info=True)
        return ""


def _apple_time(raw: int | None) -> datetime | None:
    if not raw:
        return None
    seconds = raw / 1e9 if raw > _NANOSECOND_FLOOR else float(raw)
    try:
        return datetime.fromtimestamp(seconds + _APPLE_EPOCH)
    except (OverflowError, OSError, ValueError):
        return None


def _digits(handle: str) -> str:
    return re.sub(r"\D", "", handle)


# A group chat's identifier as it appears in chat.db: "chat" followed by a long
# decimal id. Distinct from a phone number, which never carries that prefix.
_GROUP_IDENTIFIER = re.compile(r"^chat\d+$", re.IGNORECASE)


def is_group_identifier(value: str) -> bool:
    """Whether a recipient string is a group-chat identifier.

    Group ids (``chat523938370228380820``) otherwise pass ``looks_like_handle``
    on their digit count alone, and macOS group sends fail *silently*, so the
    send path must reject them explicitly rather than hand them to AppleScript.
    """
    return bool(_GROUP_IDENTIFIER.match(value.strip()))


def looks_like_handle(value: str) -> bool:
    """Whether a string is already an address rather than a person's name."""
    stripped = value.strip()
    if "@" in stripped:
        return True
    return len(_digits(stripped)) >= 7


# ── the client ───────────────────────────────────────────────────────────────

_RECENT_SQL = """
SELECT m.is_from_me, m.date, m.text, m.attributedBody,
       h.id AS handle, c.display_name, c.chat_identifier, c.room_name
FROM message m
LEFT JOIN handle h ON m.handle_id = h.ROWID
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
{where}
ORDER BY m.date DESC
LIMIT ?
"""

_CONTACTS_SCRIPT = """
on run argv
    set query to item 1 of argv
    set out to ""
    tell application "Contacts"
        repeat with p in (every person whose name contains query)
            set who to name of p
            repeat with ph in phones of p
                set out to out & who & tab & "phone" & tab & (value of ph) & linefeed
            end repeat
            repeat with em in emails of p
                set out to out & who & tab & "email" & tab & (value of em) & linefeed
            end repeat
        end repeat
    end tell
    return out
end run
"""

_SEND_SCRIPT = """
on run argv
    set target to item 1 of argv
    set body to item 2 of argv
    tell application "Messages"
        try
            set svc to 1st account whose service type = iMessage
            send body to participant target of svc
        on error
            set svc to 1st account whose service type = SMS
            send body to participant target of svc
        end try
    end tell
    return "sent"
end run
"""


class MessagesClient:
    """Read and send iMessage. All blocking work runs off the event loop."""

    def __init__(self, config: MessagesConfig) -> None:
        self._config = config

    @property
    def db_path(self) -> Path:
        return self._config.db_path.expanduser()

    # ── reading ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        if not path.exists():
            raise MessagesUnavailable(
                f"No Messages database at {path}. Messages may never have been "
                "set up on this Mac."
            )
        try:
            # immutable=1 rather than mode=ro: Messages holds the database open
            # with WAL, and a plain read-only connection wants to create the
            # -shm/-wal sidecars, which fails on a database we must not touch.
            connection = sqlite3.connect(
                f"file:{path}?immutable=1", uri=True, timeout=5.0
            )
        except sqlite3.OperationalError as exc:
            raise MessagesUnavailable(
                "Could not open the Messages database. This almost always means "
                "Full Disk Access has not been granted to whatever is running "
                "Ciel — grant it in System Settings, Privacy and Security, then "
                "restart."
            ) from exc
        connection.row_factory = sqlite3.Row
        return connection

    def _recent_sync(self, limit: int, handle: str | None) -> list[Message]:
        where, params = "", []
        if handle:
            where = "WHERE h.id = ? OR c.chat_identifier = ?"
            params = [handle, handle]
        sql = _RECENT_SQL.format(where=where)

        # ``closing``, not ``with connection``: the connection context manager
        # only wraps a transaction, and these run on a background thread often
        # enough that leaking one per call would matter.
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(sql, [*params, limit]).fetchall()
        except sqlite3.Error as exc:
            # An immutable=1 read of a live WAL database can tear mid-query and
            # raise "database disk image is malformed". It is transient, not
            # fatal — surface it as unavailability so the turn ends gracefully
            # instead of crashing with an unhandled DatabaseError.
            raise MessagesUnavailable(
                "The Messages database was busy or mid-write. Try again in a moment."
            ) from exc

        messages = []
        for row in rows:
            text = (row["text"] or "").strip() or _decode_attributed(
                row["attributedBody"]
            )
            if not text:
                continue
            room = row["room_name"]
            messages.append(
                Message(
                    text=text,
                    from_me=bool(row["is_from_me"]),
                    handle=row["handle"],
                    chat=row["display_name"] or row["chat_identifier"],
                    when=_apple_time(row["date"]),
                    is_group=bool(room),
                )
            )
        messages.reverse()  # oldest first reads like a conversation
        return messages

    async def recent(self, limit: int = 20, handle: str | None = None) -> list[Message]:
        """The most recent messages, oldest first, optionally from one handle."""
        limit = max(1, min(limit, self._config.max_history))
        return await asyncio.to_thread(self._recent_sync, limit, handle)

    def _known_handles_sync(self, query: str) -> list[Contact]:
        """Handles seen in the message history, matched loosely against a name.

        The fallback when Contacts is unavailable or the person isn't in the
        address book: a chat's display name is often the only name we have.
        """
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT DISTINCT c.chat_identifier, c.display_name "
                    "FROM chat c WHERE c.room_name IS NULL OR c.room_name = ''"
                ).fetchall()
        except sqlite3.Error as exc:
            # See _recent_sync: a torn immutable read is transient, not fatal.
            raise MessagesUnavailable(
                "The Messages database was busy or mid-write. Try again in a moment."
            ) from exc

        needle = query.strip().lower()
        found = []
        for row in rows:
            identifier = row["chat_identifier"] or ""
            display = row["display_name"] or ""
            haystack = f"{display} {identifier}".lower()
            if needle and needle not in haystack:
                continue
            found.append(
                Contact(
                    name=display or identifier,
                    kind="email" if "@" in identifier else "phone",
                    handle=identifier,
                )
            )
        return found

    # ── contacts ─────────────────────────────────────────────────────────────

    def _contacts_framework_sync(self, query: str) -> list[Contact]:
        """Resolve a name through the Contacts framework, in-process.

        The AppleScript route below launches Contacts.app if it isn't running
        and walks the address book over the Apple Event bridge — one to four
        seconds on a real address book, spent between "send it to Sam" and
        the confirmation prompt. CNContactStore reads the same data through
        an indexed native query, typically well under 50 ms, and never
        launches anything. Raises :class:`MessagesUnavailable` when the
        framework, its permission, or the query is unavailable, so the
        caller can fall back to AppleScript.
        """
        try:
            import Contacts
        except ImportError as exc:
            raise MessagesUnavailable(
                "The Contacts framework bindings are not installed."
            ) from exc

        store = Contacts.CNContactStore.alloc().init()
        status = Contacts.CNContactStore.authorizationStatusForEntityType_(
            Contacts.CNEntityTypeContacts
        )
        if status == Contacts.CNAuthorizationStatusNotDetermined:
            # First use: put the system permission dialog up and wait like a
            # person would. The completion handler fires on a private queue;
            # this runs on a to_thread worker, so blocking on it is fine.
            answered = threading.Event()
            verdict: list[bool] = []

            def _answer(granted: bool, _error: object) -> None:
                verdict.append(bool(granted))
                answered.set()

            store.requestAccessForEntityType_completionHandler_(
                Contacts.CNEntityTypeContacts, _answer
            )
            if not answered.wait(self._config.script_timeout_s) or not (
                verdict and verdict[0]
            ):
                raise MessagesUnavailable(
                    "Contacts access was not granted. It may be showing a "
                    "permission prompt — check the screen."
                )
        elif status in (
            Contacts.CNAuthorizationStatusRestricted,
            Contacts.CNAuthorizationStatusDenied,
        ):
            raise MessagesUnavailable("Contacts access is denied.")

        keys = [
            Contacts.CNContactFormatter.descriptorForRequiredKeysForStyle_(
                Contacts.CNContactFormatterStyleFullName
            ),
            Contacts.CNContactPhoneNumbersKey,
            Contacts.CNContactEmailAddressesKey,
        ]
        people, error = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            Contacts.CNContact.predicateForContactsMatchingName_(query), keys, None
        )
        if error is not None:
            raise MessagesUnavailable(f"Contacts query failed: {error}")

        found: list[Contact] = []
        for person in people or []:
            name = str(
                Contacts.CNContactFormatter.stringFromContact_style_(
                    person, Contacts.CNContactFormatterStyleFullName
                )
                or query
            ).strip()
            for labeled in person.phoneNumbers():
                value = str(labeled.value().stringValue()).strip()
                if value:
                    found.append(Contact(name=name, kind="phone", handle=value))
            for labeled in person.emailAddresses():
                value = str(labeled.value()).strip()
                if value:
                    found.append(Contact(name=name, kind="email", handle=value))
        return found

    async def _osascript(self, script: str, *args: str) -> str:
        """Run an AppleScript with arguments passed as argv.

        The script goes in on stdin and the arguments arrive as ``argv``, so no
        user-supplied text is ever interpolated into source. A message
        containing a quote is then just a message, not a syntax error or an
        injection.
        """
        process = await asyncio.create_subprocess_exec(
            "osascript",
            "-",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(script.encode()),
                timeout=self._config.script_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            raise MessagesUnavailable(
                "Messages did not respond. It may be showing a permission "
                "prompt — check the screen."
            ) from exc

        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise MessagesUnavailable(detail or "AppleScript failed with no message.")
        return stdout.decode(errors="replace").strip()

    async def find_contacts(self, query: str) -> list[Contact]:
        """Resolve a spoken name to the addresses it could mean.

        Deliberately returns *every* match rather than guessing. Speech is
        lossy and address books are full of near-collisions; picking one and
        sending is how a message reaches the wrong person.
        """
        query = query.strip()
        if not query:
            return []
        if looks_like_handle(query):
            kind = "email" if "@" in query else "phone"
            return [Contact(name=query, kind=kind, handle=query)]

        contacts: list[Contact] = []
        consulted = False
        try:
            contacts = await asyncio.to_thread(self._contacts_framework_sync, query)
            consulted = True
        except Exception as exc:  # noqa: BLE001 - the fallback must be load-bearing
            # Framework unavailable (bindings missing, permission denied) — or
            # any pyobjc-level surprise (objc.error, a missing symbol on older
            # bindings). All of them mean the same thing here: this route
            # failed, and the AppleScript route reads the same address book
            # under its own separate Automation permission, so it may still
            # succeed. A narrow catch would let a bridge quirk kill the whole
            # turn to protect a fallback that exists for exactly that case.
            log.debug("Contacts framework failed (%s) — trying AppleScript", exc)

        if not consulted:
            try:
                raw = await self._osascript(_CONTACTS_SCRIPT, query)
                for line in raw.splitlines():
                    parts = line.split("\t")
                    if len(parts) != 3:
                        continue
                    name, kind, handle = (p.strip() for p in parts)
                    if handle:
                        contacts.append(Contact(name=name, kind=kind, handle=handle))
            except MessagesUnavailable as exc:
                # Contacts access is a separate permission from Full Disk
                # Access, and it being missing shouldn't take out name lookup
                # entirely — the chat history knows plenty of names on its own.
                log.debug(
                    "Contacts lookup failed (%s) — falling back to chat history", exc
                )

        if not contacts:
            contacts = await asyncio.to_thread(self._known_handles_sync, query)

        # Same person, same number, listed twice in different places. Emails key
        # on the full address, never on their digits: "sam2@x.com" and
        # "bob2@y.com" share the digit "2", and collapsing them would silently
        # drop a distinct person. Only phone-style handles dedup by digits, so
        # +1 (555) 010 and 15550010 count as one.
        seen, unique = set(), []
        for contact in contacts:
            handle = contact.handle.strip()
            if "@" in handle:
                key = handle.lower()
            else:
                key = _digits(handle) or handle.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(contact)
        return unique

    # ── sending ──────────────────────────────────────────────────────────────

    async def send(self, handle: str, text: str) -> None:
        """Send one message to one address.

        Raises rather than returning a status, because the interesting failures
        here are all things the user needs told out loud.
        """
        if not self._config.allow_send:
            raise MessagesUnavailable(
                "Sending is turned off. Set allow_send in the messages section "
                "of the config to enable it."
            )

        handle = handle.strip()
        text = text.strip()
        if not handle:
            raise MessagesUnavailable("No recipient.")
        if not text:
            raise MessagesUnavailable("No message body.")
        if is_group_identifier(handle):
            # Refused outright: a group send reports success while the message
            # lands nowhere on current macOS — the one failure a voice assistant
            # must never have. This must precede looks_like_handle, which a
            # group id passes on its digit count.
            raise MessagesUnavailable(
                f"'{handle}' is a group chat, and group sends fail silently on "
                "this macOS. Tell the user to send it from Messages themselves."
            )
        if not looks_like_handle(handle):
            # A name reaching this far means contact resolution was skipped.
            # Messages would interpret it as a literal address and fail
            # obscurely, or worse, match something unintended.
            raise MessagesUnavailable(
                f"'{handle}' is a name, not a phone number or email. Resolve it "
                "with find_contact first."
            )
        if len(text) > self._config.max_message_chars:
            raise MessagesUnavailable(
                f"Message is {len(text)} characters; the limit is "
                f"{self._config.max_message_chars}."
            )

        await self._osascript(_SEND_SCRIPT, handle, text)
        log.info("sent %d chars to %s", len(text), handle)


__all__ = [
    "Contact",
    "Message",
    "MessagesClient",
    "MessagesUnavailable",
    "is_group_identifier",
    "looks_like_handle",
]
