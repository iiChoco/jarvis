"""The spoke's executor: the hub's tool calls, done on the Mac.

Each ``tool.request`` names one of the handlers below and carries its
arguments; the answer goes back as ``tool.result`` with ``content`` or
``error``. The handlers are thin — the real implementations are the
same modules the single process uses (the screen capture, the Messages
client, the locator, the work watcher, the calendar store) — and the
executor's own job is the boundary: JSON in and out, one task per call
so a slow capture never blocks a fast lookup, ``tool.cancel`` honored,
every failure a sentence rather than a traceback on the wire.

The shell and the files are the one place this module has opinions.
The hub's guards already asked the user; this side does not trust that
alone. ``ShellGuard``'s classifier runs again here, from *this*
machine's config: a command the classifier would deny is refused
whatever the hub says, a command that needs a confirmation is refused
unless the hub says it asked, and only the quiet tier runs on the
hub's word alone. The file handlers run the workspace guard's path
check the same way. Defense in depth, because this socket is the whole
distance between "the user's assistant" and "whatever reached the port".
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ciel.brain.permissions import WorkspaceGuard
from ciel.brain.shellguard import classify

if TYPE_CHECKING:
    from ciel.config import Config
    from ciel.location import Locator
    from ciel.messages import MessagesClient
    from ciel.proactive.work import WorkWatcher

log = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 12000
"""Shell output past this is cut, with a note — the model reads it into
context, and a runaway `cat` must not cost a whole turn's window."""


class Executor:
    """The handler registry and the in-flight table."""

    def __init__(
        self,
        config: "Config",
        send: Callable[[dict[str, Any]], bool],
        *,
        messages: "MessagesClient | None" = None,
        locator: "Locator | None" = None,
        watcher: "WorkWatcher | None" = None,
        calendar: Any | None = None,
    ) -> None:
        self._config = config
        self._send = send
        self._messages = messages
        self._locator = locator
        self._watcher = watcher
        self._calendar = calendar
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._workspace: WorkspaceGuard | None = (
            WorkspaceGuard(config.files.workspace, config.files.read_only_outside)
            if config.files.enabled
            else None
        )
        self._handlers: dict[str, Callable[..., Awaitable[Any]]] = {
            "screen.capture": self._screen_capture,
            "messages.find_contacts": self._messages_find,
            "messages.recent": self._messages_recent,
            "messages.send": self._messages_send,
            "location.describe": self._location_describe,
            "location.current": self._location_current,
            "watch.add": self._watch_add,
            "watch.active": self._watch_active,
            "calendar.agenda_today": self._calendar_agenda,
            "shell.run": self._shell_run,
            "files.read": self._files_read,
            "files.write": self._files_write,
            "files.list": self._files_list,
        }

    # ── the wire's side ──────────────────────────────────────────────────────

    def handle(self, frame: dict[str, Any]) -> None:
        """A ``tool.request`` or ``tool.cancel``; returns at once."""
        if frame["type"] == "tool.cancel":
            task = self._tasks.pop(frame["rpc_id"], None)
            if task is not None and not task.done():
                task.cancel()
            return
        rpc_id = frame["rpc_id"]
        self._tasks[rpc_id] = asyncio.create_task(
            self._run(rpc_id, frame["tool"], frame.get("args") or {},
                      float(frame.get("timeout_s") or 30.0))
        )

    async def _run(self, rpc_id: str, tool: str, args: dict[str, Any], timeout: float) -> None:
        handler = self._handlers.get(tool)
        try:
            if handler is None:
                raise ValueError(f"the Mac does not know the tool {tool!r}")
            content = await asyncio.wait_for(handler(**args), timeout)
            result = {"type": "tool.result", "rpc_id": rpc_id, "ok": True, "content": content}
        except asyncio.CancelledError:
            return
        except asyncio.TimeoutError:
            result = {"type": "tool.result", "rpc_id": rpc_id, "ok": False,
                      "error": f"{tool} took longer than {timeout:.0f}s on the Mac"}
        except Exception as exc:  # noqa: BLE001 - every failure is a sentence on the wire
            log.debug("tool %s failed", tool, exc_info=True)
            result = {"type": "tool.result", "rpc_id": rpc_id, "ok": False, "error": str(exc)}
        finally:
            self._tasks.pop(rpc_id, None)
        self._send(result)

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    # ── the senses ───────────────────────────────────────────────────────────

    async def _screen_capture(self, max_edge: int = 1568) -> list[str]:
        from ciel.brain.tools.screen import capture_displays

        images = await capture_displays(int(max_edge))
        return [base64.b64encode(data).decode("ascii") for data in images]

    async def _messages_find(self, query: str) -> list[dict[str, str]]:
        if self._messages is None:
            raise RuntimeError("Messages access is off on the Mac")
        return [
            {"name": c.name, "kind": c.kind, "handle": c.handle}
            for c in await self._messages.find_contacts(query)
        ]

    async def _messages_recent(self, limit: int = 20, handle: str | None = None) -> list[dict[str, Any]]:
        if self._messages is None:
            raise RuntimeError("Messages access is off on the Mac")
        return [
            {
                "text": m.text, "from_me": m.from_me, "handle": m.handle,
                "chat": m.chat, "when": m.when.isoformat() if m.when else None,
                "is_group": m.is_group,
            }
            for m in await self._messages.recent(limit=int(limit), handle=handle)
        ]

    async def _messages_send(self, handle: str, text: str) -> bool:
        if self._messages is None:
            raise RuntimeError("Messages access is off on the Mac")
        await self._messages.send(handle, text)
        return True

    async def _location_describe(self, max_age_s: float = 120.0) -> str:
        if self._locator is None:
            raise RuntimeError("location is off on the Mac")
        fix = await asyncio.to_thread(self._locator.current, float(max_age_s))
        return self._locator.describe(fix)

    async def _location_current(self, max_age_s: float = 120.0) -> dict[str, Any] | None:
        if self._locator is None:
            raise RuntimeError("location is off on the Mac")
        fix = await asyncio.to_thread(self._locator.current, float(max_age_s))
        if fix is None:
            return None
        return {
            "at": fix.at, "source": fix.source, "latitude": fix.latitude,
            "longitude": fix.longitude, "accuracy_m": fix.accuracy_m,
            "network": fix.network, "device": fix.device,
        }

    async def _watch_add(self, kind: str, target: str, label: str,
                         timeout_minutes: float = 60.0) -> dict[str, Any]:
        if self._watcher is None:
            raise RuntimeError("background watching is off on the Mac")
        if kind == "file":
            watch = self._watcher.add_file(str(target), str(label), float(timeout_minutes))
        elif kind == "pid":
            watch = self._watcher.add_pid(int(target), str(label), float(timeout_minutes))
        else:
            raise ValueError(f"unknown watch kind {kind!r}")
        return _watch_dict(watch)

    async def _watch_active(self) -> list[dict[str, Any]]:
        if self._watcher is None:
            return []
        return [_watch_dict(w) for w in self._watcher.active()]

    async def _calendar_agenda(self) -> list[str]:
        if self._calendar is None:
            return []
        return list(await self._calendar.agenda_today())

    # ── the machine ──────────────────────────────────────────────────────────

    async def _shell_run(self, command: str, confirmed: bool = False) -> dict[str, Any]:
        if not self._config.shell.enabled:
            raise RuntimeError("the shell is off on the Mac")
        tier, reason = classify(command, self._config.shell)
        if tier == "deny":
            raise RuntimeError(f"refused on the Mac — {reason}")
        if tier != "quiet" and not confirmed:
            raise RuntimeError("refused on the Mac — that command needs the user's yes, and none was given")
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._config.files.workspace.expanduser()) if self._config.files.enabled else None,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), self._config.shell.command_timeout_s
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise RuntimeError(
                f"the command ran past {self._config.shell.command_timeout_s:.0f}s and was killed"
            ) from None
        return {
            "exit": proc.returncode,
            "stdout": _cut(out.decode(errors="replace")),
            "stderr": _cut(err.decode(errors="replace")),
        }

    def _path(self, raw: str, *, write: bool) -> Path:
        if self._workspace is None:
            raise RuntimeError("file access is off on the Mac")
        verdict = self._workspace._check(raw, is_write=write)  # noqa: SLF001 - the one check, reused
        if verdict is not None:
            raise RuntimeError(f"refused on the Mac — {verdict}")
        path = Path(raw).expanduser()
        return path if path.is_absolute() else self._workspace.workspace / path

    async def _files_read(self, path: str) -> str:
        target = self._path(path, write=False)
        return _cut(await asyncio.to_thread(target.read_text, "utf-8", "replace"))

    async def _files_write(self, path: str, content: str) -> str:
        target = self._path(path, write=True)

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")

        await asyncio.to_thread(write)
        return f"wrote {len(str(content))} characters to {target}"

    async def _files_list(self, path: str = ".") -> list[str]:
        target = self._path(path, write=False)
        names = await asyncio.to_thread(lambda: sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir()))
        return names[:500]


def _watch_dict(watch: Any) -> dict[str, Any]:
    return {
        "id": watch.id, "kind": watch.kind, "target": watch.target,
        "label": watch.label, "created_at": watch.created_at,
        "expires_at": watch.expires_at,
    }


def _cut(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n… [{len(text) - _MAX_OUTPUT_CHARS} more characters cut]"


__all__ = ["Executor"]
