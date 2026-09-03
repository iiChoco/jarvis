"""The look-at-screen tool — Ciel's eyes, strictly on demand.

A voice assistant lives next to a screen the user is usually looking at, and
natural speech points at it constantly: "put this event on my calendar",
"reply to that email", "what am I looking at". Those references cannot be
resolved from audio. This tool captures the displays so the model can resolve
them itself — called when the user points at something, never on a schedule.

macOS gates capture behind Screen Recording permission, and the failure mode
without it is nasty: ``screencapture`` still exits zero and produces an
image, just with every window silently missing. Trusting that output would
have Ciel confidently describing an empty desktop. So the permission is
preflighted via Quartz, and a missing grant comes back as words the model can
relay — including the one-time system prompt the preflight request triggers.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from ciel.config import ScreenConfig

log = logging.getLogger(__name__)

# Bound at startup, same pattern as the memory store: the SDK's @tool
# decorator wants plain module-level functions.
_config: ScreenConfig | None = None
_capturer: Any | None = None
"""On the hub: the Mac's screen, over the wire (``RemoteScreen``). None
means capture here — the single process, or the spoke's executor."""


class _PermissionMissing(Exception):
    """Screen Recording permission has not been granted to this process."""


async def capture_displays(max_edge: int) -> list[bytes]:
    """Every display as JPEG bytes, on a worker thread — the spoke's
    executor calls this on the hub's behalf. Raises the permission
    error as a sentence, so it crosses the wire as words."""
    try:
        return await asyncio.to_thread(_capture_sync, max_edge)
    except _PermissionMissing:
        raise RuntimeError(
            "macOS Screen Recording permission is not granted on the Mac"
        ) from None


def _capture_sync(max_edge: int) -> list[bytes]:
    """Capture every display as JPEG bytes, scaled to ``max_edge``.

    Runs in a worker thread: two subprocess calls and an image resample have
    no business blocking the audio loop.
    """
    import Quartz

    if not Quartz.CGPreflightScreenCaptureAccess():
        # Ask the OS to put up its grant dialog (a no-op if it already asked
        # once) so the user can fix this without hunting through Settings.
        Quartz.CGRequestScreenCaptureAccess()
        raise _PermissionMissing

    from AppKit import NSScreen

    displays = max(1, len(NSScreen.screens()))

    with tempfile.TemporaryDirectory(prefix="ciel-screen-") as td:
        paths = [Path(td) / f"display{i}.jpg" for i in range(displays)]
        # -x: no camera-shutter sound in the user's speakers mid-conversation.
        # Absolute paths: under launchd the PATH is minimal and doesn't
        # include /usr/sbin, where screencapture lives.
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", "jpg", *map(str, paths)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        images: list[bytes] = []
        for path in paths:
            if not path.exists():
                continue  # fewer displays than NSScreen reported — fine
            subprocess.run(
                ["/usr/bin/sips", "--resampleHeightWidthMax", str(max_edge), str(path)],
                check=True,
                capture_output=True,
                timeout=15,
            )
            images.append(path.read_bytes())
        return images


@tool(
    "look_at_screen",
    (
        "Look at the user's screen. Returns a screenshot of every connected "
        "display.\n\n"
        "Call this whenever the user refers to something they can see and "
        "you cannot — 'this event', 'that email', 'the error on my screen', "
        "'what am I looking at' — BEFORE asking them what they mean. If the "
        "screen doesn't show what they're referring to, then ask. Do not "
        "call it when the request is already fully specified without it, and "
        "never look unprompted."
    ),
    {},
)
async def look_at_screen(args: dict[str, Any]) -> dict[str, Any]:
    max_edge = _config.max_edge if _config else 1568

    try:
        if _capturer is not None:
            images = await _capturer.capture(max_edge)
        else:
            images = await asyncio.to_thread(_capture_sync, max_edge)
    except _PermissionMissing:
        return {"content": [{"type": "text", "text": (
            "You cannot see the screen: macOS Screen Recording permission is "
            "not granted to the app running Ciel. A system permission prompt "
            "may have just appeared on the user's screen. Tell them to allow "
            "it — in System Settings, under Privacy and Security, Screen "
            "Recording — and that it takes effect after Ciel restarts. For "
            "now, ask them to describe what they're looking at."
        )}]}
    except Exception as exc:  # noqa: BLE001 - a failure is a sentence for the model
        log.warning("screen capture failed: %s", exc)
        return {"content": [{"type": "text", "text": (
            f"The screen capture failed ({exc}). Ask the user to describe "
            "what they're looking at instead."
        )}]}

    if not images:
        return {"content": [{"type": "text", "text": (
            "No displays were captured — the screen may be locked or asleep. "
            "Ask the user to describe what they're looking at instead."
        )}]}

    content: list[dict[str, Any]] = [{"type": "text", "text": (
        f"The user's screen right now ({len(images)} display"
        f"{'s' if len(images) != 1 else ''}; the first is the main one)."
    )}]
    content.extend(
        {
            "type": "image",
            "data": base64.b64encode(data).decode("ascii"),
            "mimeType": "image/jpeg",
        }
        for data in images
    )
    return {"content": content}


def bind_screen(config: ScreenConfig, capturer: Any | None = None) -> None:
    """Attach the config this tool captures with — and, on the hub, the
    remote capturer that does it on the Mac."""
    global _config, _capturer
    _config = config
    _capturer = capturer


SCREEN_TOOLS = [look_at_screen]

__all__ = ["SCREEN_TOOLS", "bind_screen", "capture_displays", "look_at_screen"]
