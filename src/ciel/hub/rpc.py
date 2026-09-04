"""The hub's remote hands: the Mac's tools and senses, one hop away.

Every Mac-bound thing the brain's tools reach for — the screen, the
Messages database, the Wi-Fi and Find My position, the work watcher's
file-and-process polling, the shell and the files on the user's own
machine — lives behind a duck type the tool modules bind at startup
(``bind_screen``, ``bind_client``, ``bind_locator``, ``bind_watcher``).
In the single process those are the local implementations. On the hub
they are the classes here: the same methods, each one a ``tool.request``
to the spoke and its ``tool.result`` back, with the spoke's executor
doing the actual work on the machine the work is about.

The failure shape is words, not exceptions where a tool expects words:
a Mac that is not connected, or does not answer, comes back to the
model as "the Mac is not reachable right now", the same way a missing
permission does. The messages client keeps its own exception
(``MessagesUnavailable``) because its tools already translate that one.

Presence lives here too, for the same reason: the raw signals — screen
locked, seconds since input — are read on the Mac and arrive in the
spoke's heartbeat; :class:`PresenceView` folds them with the hub's own
knowledge (when the user last talked to Ciel) into the same frozen
``PresenceState`` the policy has always consumed. A heartbeat that goes
stale reads as away: never speak to a room that may be empty.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ciel.hub.server import HubServer, RpcUnavailable
from ciel.location import Fix
from ciel.messages import Contact, Message, MessagesUnavailable
from ciel.proactive.presence import PresenceState
from ciel.proactive.work import Watch

if TYPE_CHECKING:
    from ciel.config import Config, ProactiveConfig

log = logging.getLogger(__name__)

RPC_TIMEOUT_S = 30.0
"""The ordinary tool call's deadline. A screen capture is a second, a
contact lookup a few; a shell command gets its own, longer one."""

SHELL_TIMEOUT_S = 130.0
"""A shell command's deadline — the spoke's own command timeout plus a
margin, so the spoke reports "timed out" before the hub gives up."""


class _Remote:
    """One RPC surface, with the result unwrapped or the error raised."""

    def __init__(self, server: HubServer) -> None:
        self._server = server

    async def call(self, tool: str, timeout: float = RPC_TIMEOUT_S, **args: Any) -> Any:
        result = await self._server.rpc(tool, args, timeout)
        if not result.get("ok"):
            raise RpcUnavailable(str(result.get("error") or f"{tool} failed on the Mac"))
        return result.get("content")


class RemoteScreen(_Remote):
    """The screen tool's capturer: JPEG bytes per display, from the Mac."""

    async def capture(self, max_edge: int) -> list[bytes]:
        import base64

        content = await self.call("screen.capture", max_edge=max_edge)
        return [base64.b64decode(item) for item in content or []]


class RemoteMessagesClient(_Remote):
    """``MessagesClient``'s three methods, over the wire. Failures arrive
    as ``MessagesUnavailable`` so the tools' wording stays the same."""

    async def find_contacts(self, query: str) -> list[Contact]:
        try:
            rows = await self.call("messages.find_contacts", query=query)
        except RpcUnavailable as exc:
            raise MessagesUnavailable(str(exc)) from None
        return [Contact(name=r["name"], kind=r["kind"], handle=r["handle"]) for r in rows or []]

    async def recent(self, limit: int = 20, handle: str | None = None) -> list[Message]:
        try:
            rows = await self.call("messages.recent", limit=limit, handle=handle)
        except RpcUnavailable as exc:
            raise MessagesUnavailable(str(exc)) from None
        out: list[Message] = []
        for r in rows or []:
            when = datetime.fromisoformat(r["when"]) if r.get("when") else None
            out.append(Message(
                text=r["text"], from_me=r["from_me"], handle=r.get("handle"),
                chat=r.get("chat"), when=when, is_group=bool(r.get("is_group")),
            ))
        return out

    async def send(self, handle: str, text: str) -> None:
        try:
            await self.call("messages.send", handle=handle, text=text)
        except RpcUnavailable as exc:
            raise MessagesUnavailable(str(exc)) from None


class RemoteLocator(_Remote):
    """The location tool's reader: the spoke describes its own fix, so the
    words and the places config are the Mac's."""

    async def describe_now(self, max_age_s: float) -> str:
        try:
            return str(await self.call("location.describe", max_age_s=max_age_s))
        except RpcUnavailable as exc:
            return f"Location is not available right now — {exc}."

    async def current_fix(self, max_age_s: float) -> Fix | None:
        raw = await self.call("location.current", max_age_s=max_age_s)
        if not raw:
            return None
        return Fix(**raw)


def _watch_from(raw: dict[str, Any]) -> Watch:
    return Watch(
        id=str(raw["id"]), kind=str(raw["kind"]), target=str(raw["target"]),
        label=str(raw.get("label") or ""), created_at=float(raw["created_at"]),
        expires_at=float(raw["expires_at"]),
    )


class RemoteWorkWatcher(_Remote):
    """The watch tool's registry, on the machine being watched. ``active``
    answers from the last heartbeat (the roster is polled once a second;
    a round trip per poll would be silly), the adds are calls."""

    def __init__(self, server: HubServer) -> None:
        super().__init__(server)
        self._watches: list[Watch] = []

    async def add_file(self, target: str, label: str, timeout_minutes: float) -> Watch:
        raw = await self.call("watch.add", kind="file", target=target, label=label,
                              timeout_minutes=timeout_minutes)
        return _watch_from(raw)

    async def add_pid(self, pid: int, label: str, timeout_minutes: float) -> Watch:
        raw = await self.call("watch.add", kind="pid", target=str(pid), label=label,
                              timeout_minutes=timeout_minutes)
        return _watch_from(raw)

    def active(self) -> list[Watch]:
        return list(self._watches)

    def observe(self, watches: list[dict[str, Any]]) -> None:
        """The heartbeat's roster."""
        parsed: list[Watch] = []
        for raw in watches:
            try:
                parsed.append(_watch_from(raw))
            except (KeyError, TypeError, ValueError):
                continue
        self._watches = parsed


class RemoteCalendar(_Remote):
    """The brief's agenda, from the Mac's EventKit store."""

    async def agenda_today(self) -> list[str]:
        try:
            rows = await self.call("calendar.agenda_today")
        except RpcUnavailable as exc:
            log.warning("agenda unavailable: %s", exc)
            return []
        return [str(r) for r in rows or []]


class RemoteMac(_Remote):
    """The user's machine, for the run-and-edit requests that name it:
    a shell and the files. The spoke re-runs the guards before touching
    anything; the hub's own guards ask the user first. ``confirmed`` tells
    the spoke the hub's gate already asked, for the tiers that need it."""

    async def run(self, command: str, *, confirmed: bool) -> dict[str, Any]:
        content = await self.call("shell.run", SHELL_TIMEOUT_S, command=command,
                                  confirmed=confirmed)
        return dict(content or {})

    async def read_file(self, path: str) -> str:
        return str(await self.call("files.read", path=path))

    async def write_file(self, path: str, content: str) -> str:
        return str(await self.call("files.write", path=path, content=content))

    async def list_dir(self, path: str) -> list[str]:
        return [str(r) for r in await self.call("files.list", path=path) or []]


class PresenceView:
    """Presence on the hub: the spoke's signals plus the hub's own.

    The same ``state(now)`` the policy calls on the in-process probe,
    with the Quartz reads replaced by the last heartbeat. Stale — no
    heartbeat within ``stale_s`` — reads as locked and idle forever, so
    the only route to "present" is a recent conversation, and even that
    is refused: a spoke that stopped reporting may have stopped
    listening, and speaking to it is speaking to nobody.
    """

    def __init__(self, config: "ProactiveConfig", stale_s: float = 30.0) -> None:
        self._config = config
        self._stale_s = stale_s
        self._last_conversation: float | None = None
        self._heard_at: float | None = None
        self._locked = True
        self._idle_s = float("inf")
        self._attended = False

    def note_conversation(self, now: float) -> None:
        self._last_conversation = now

    def observe(self, frame: dict[str, Any], now: float) -> None:
        """One heartbeat, monotonic ``now``."""
        self._heard_at = now
        self._locked = bool(frame.get("locked"))
        idle = frame.get("idle_s")
        self._idle_s = float(idle) if isinstance(idle, (int, float)) else float("inf")
        self._attended = bool(frame.get("attended"))

    @property
    def fresh(self) -> bool:
        return self._heard_at is not None and (time.monotonic() - self._heard_at) <= self._stale_s

    def state(self, now: float) -> PresenceState:
        stale = self._heard_at is None or (now - self._heard_at) > self._stale_s
        locked = True if stale else self._locked
        idle = float("inf") if stale else self._idle_s
        since_conv = (
            now - self._last_conversation if self._last_conversation is not None else None
        )
        at_keyboard = (not locked) and idle < self._config.presence_idle_s
        in_conversation = (
            since_conv is not None and since_conv < self._config.conversation_presence_s
        )
        mode = self._config.presence_mode
        if mode == "hid":
            present = at_keyboard
        elif mode == "conversation":
            present = in_conversation
        else:
            present = at_keyboard or in_conversation
        if stale:
            present = False
        return PresenceState(
            screen_locked=locked,
            seconds_since_input=idle,
            seconds_since_conversation=since_conv,
            present=present,
        )


class RemoteBindings:
    """Everything the tool registry binds on the hub instead of building
    locally — one object so ``build_tool_server`` takes one argument."""

    def __init__(self, server: HubServer, config: "Config") -> None:
        self.server = server
        self.screen = RemoteScreen(server)
        self.messages = RemoteMessagesClient(server)
        self.locator = RemoteLocator(server)
        self.watcher = RemoteWorkWatcher(server)
        self.calendar = RemoteCalendar(server)
        self.mac = RemoteMac(server)
        self.presence = PresenceView(config.proactive, config.hub.presence_stale_s)


__all__ = [
    "PresenceView",
    "RPC_TIMEOUT_S",
    "RemoteBindings",
    "RemoteCalendar",
    "RemoteLocator",
    "RemoteMac",
    "RemoteMessagesClient",
    "RemoteScreen",
    "RemoteWorkWatcher",
    "RpcUnavailable",
]
