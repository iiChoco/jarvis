"""The Mac tools — the user's own machine, from a brain that lives elsewhere.

Registered only on the hub. In the single process the model's Bash and
file tools *are* the user's machine; on the hub they are the server's
workspace, and these four are how "run this" and "open that file" reach
the Mac the user is sitting at. Each one is a call to the spoke's
executor, which does the work with the same modules the single process
uses and re-runs the guards from its own config before touching
anything.

The gates live in the brain: ``run_on_mac`` sits behind a second
``ShellGuard`` matched on its name (read-only commands run at once,
anything with side effects is read to the user and waits for a spoken
yes, the deny tier is refused outright), and the file tools are in the
workspace guard's path map. What the hub's guard decided rides down as
``confirmed`` so the spoke can hold the line that a side-effectful
command never runs on the hub's word alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import tool

from ciel.brain.shellguard import classify

if TYPE_CHECKING:
    from ciel.config import ShellConfig
    from ciel.hub.rpc import RemoteMac

log = logging.getLogger(__name__)

_mac: "RemoteMac | None" = None
_shell: "ShellConfig | None" = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "run_on_mac",
    (
        "Run a shell command on the user's Mac — their own computer, not this "
        "server. Use it for anything the user asks you to run, check, open, or "
        "do on their machine. Read-only commands run immediately; anything "
        "with side effects is read to the user out loud and waits for their "
        "spoken yes before running (that happens automatically — never ask "
        "yourself). One command at a time, foreground only, nothing chained. "
        "Returns the exit code and output."
    ),
    {"command": str},
)
async def run_on_mac(args: dict[str, Any]) -> dict[str, Any]:
    if _mac is None:
        return _text("The Mac is not reachable right now.")
    command = str(args.get("command") or "").strip()
    if not command:
        return _text("A command is needed.")
    # What the hub's gate would have asked about is what the spoke is
    # told was asked: the quiet tier needs no yes, and the hook that
    # ran before this call is the only way a louder tier got here.
    tier = classify(command, _shell)[0] if _shell is not None else "deny"
    try:
        result = await _mac.run(command, confirmed=tier != "quiet")
    except Exception as exc:  # noqa: BLE001 - the Mac may be gone
        return _text(f"Could not run that on the Mac: {exc}")
    out = result.get("stdout") or ""
    err = result.get("stderr") or ""
    parts = [f"exit {result.get('exit')}"]
    if out.strip():
        parts.append(f"stdout:\n{out}")
    if err.strip():
        parts.append(f"stderr:\n{err}")
    return _text("\n".join(parts))


@tool(
    "mac_read_file",
    (
        "Read a file on the user's Mac. Paths are the Mac's; a relative path "
        "is inside the user's workspace there. Use this, not the local Read "
        "tool, for any file the user names on their machine."
    ),
    {"path": str},
)
async def mac_read_file(args: dict[str, Any]) -> dict[str, Any]:
    if _mac is None:
        return _text("The Mac is not reachable right now.")
    path = str(args.get("path") or "").strip()
    if not path:
        return _text("A path is needed.")
    try:
        return _text(await _mac.read_file(path))
    except Exception as exc:  # noqa: BLE001
        return _text(f"Could not read that on the Mac: {exc}")


@tool(
    "mac_write_file",
    (
        "Write a file on the user's Mac (creating or replacing it). Paths are "
        "the Mac's; a relative path is inside the user's workspace there. "
        "Refused outside the workspace."
    ),
    {"path": str, "content": str},
)
async def mac_write_file(args: dict[str, Any]) -> dict[str, Any]:
    if _mac is None:
        return _text("The Mac is not reachable right now.")
    path = str(args.get("path") or "").strip()
    if not path:
        return _text("A path is needed.")
    try:
        return _text(await _mac.write_file(path, str(args.get("content") or "")))
    except Exception as exc:  # noqa: BLE001
        return _text(f"Could not write that on the Mac: {exc}")


@tool(
    "mac_list_dir",
    "List a directory on the user's Mac. Paths are the Mac's; a relative path is inside the user's workspace there.",
    {"path": str},
)
async def mac_list_dir(args: dict[str, Any]) -> dict[str, Any]:
    if _mac is None:
        return _text("The Mac is not reachable right now.")
    path = str(args.get("path") or ".").strip() or "."
    try:
        names = await _mac.list_dir(path)
    except Exception as exc:  # noqa: BLE001
        return _text(f"Could not list that on the Mac: {exc}")
    return _text("\n".join(names) if names else "(empty)")


def bind_mac(mac: "RemoteMac", shell: "ShellConfig") -> None:
    """Attach the Mac these tools reach, and the shell config whose
    classifier decides which calls arrive pre-confirmed."""
    global _mac, _shell
    _mac = mac
    _shell = shell


MAC_TOOLS = [run_on_mac, mac_read_file, mac_write_file, mac_list_dir]

__all__ = ["MAC_TOOLS", "bind_mac", "mac_list_dir", "mac_read_file", "mac_write_file", "run_on_mac"]
