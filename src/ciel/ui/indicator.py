"""Status indicators — telling you what Ciel is doing without the terminal.

Same protocol shape as the rest of the system, for the same reason: the
pipeline announces state transitions and does not care whether that lights a
pill, prints a line, or goes nowhere.

Every method here is failure-tolerant on purpose. An indicator is a
convenience; it must never be able to break a working assistant. A HUD that
crashes, or a pipe that closes, degrades to "no indicator" rather than to "no
Ciel".
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Protocol, runtime_checkable

from ciel.config import UIConfig

log = logging.getLogger(__name__)

# The vocabulary shared with the HUD subprocess. "thinking" is the model
# working silently; "reasoning" is Ciel speaking its reasoning aloud —
# distinct so the listener can tell deliberation from the answer.
STATES = ("idle", "listening", "thinking", "reasoning", "speaking", "error")


@runtime_checkable
class Indicator(Protocol):
    """Displays Ciel's current state."""

    async def start(self) -> None:
        ...

    def set_state(self, state: str) -> None:
        """Announce a state change.

        Deliberately synchronous and non-blocking: it's called from the audio
        frame loop, which must never wait on a UI.
        """
        ...

    async def close(self) -> None:
        ...


class NullIndicator:
    """Does nothing. The fallback whenever a real indicator is unavailable."""

    async def start(self) -> None:
        return None

    def set_state(self, state: str) -> None:
        return None

    async def close(self) -> None:
        return None


class TerminalIndicator:
    """Prints state changes to the terminal.

    Useful when running headless or over SSH, where a floating window is
    either impossible or would appear on the wrong machine.
    """

    _SYMBOLS = {
        "idle": "\033[90m○ idle\033[0m",
        "listening": "\033[92m● listening\033[0m",
        "thinking": "\033[93m● thinking\033[0m",
        "reasoning": "\033[95m● reasoning\033[0m",
        "speaking": "\033[94m● speaking\033[0m",
        "error": "\033[91m● error\033[0m",
    }

    def __init__(self) -> None:
        self._last: str | None = None

    async def start(self) -> None:
        return None

    def set_state(self, state: str) -> None:
        if state == self._last:
            return
        self._last = state
        sys.stdout.write(f"\r\033[K  {self._SYMBOLS.get(state, state)}  ")
        sys.stdout.flush()

    async def close(self) -> None:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


class HudIndicator:
    """A floating pill, driven as a child process.

    The HUD owns an AppKit run loop, which permanently occupies whichever
    thread it starts on — irreconcilable with the asyncio loop running the
    pipeline. Giving it its own process sidesteps that completely, and buys
    fault isolation for free.

    Communication is one state word per line on the child's stdin. A pipe is
    enough: the payload is a single short token, and there is nothing to read
    back.
    """

    def __init__(self, config: UIConfig) -> None:
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._last: str | None = None

    async def start(self) -> None:
        payload = json.dumps({
            "position": self._config.position,
            "margin": self._config.margin,
            "opacity": self._config.opacity,
            "scale": self._config.scale,
            "hide_when_idle": self._config.hide_when_idle,
        })

        try:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "ciel.ui.hud",
                payload,
                stdin=asyncio.subprocess.PIPE,
                # The child's own output would interleave with Ciel's
                # transcript and make both unreadable.
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("could not start the HUD (%s) — continuing without it", exc)
            self._proc = None
            return

        log.debug("hud started (pid %s)", self._proc.pid)
        self.set_state("idle")

    def set_state(self, state: str) -> None:
        if state == self._last:
            return  # the common case: don't wake the pipe 33 times a second
        self._last = state

        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            return

        try:
            proc.stdin.write(f"{state}\n".encode())
            # Deliberately not awaiting drain(): this is called from the audio
            # loop, and a blocked UI pipe must not be able to stall audio. The
            # payload is a few bytes, so the buffer will not realistically fill.
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            log.debug("hud pipe closed; disabling indicator")
            self._proc = None

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return

        try:
            if proc.stdin is not None:
                proc.stdin.write(b"quit\n")
                await proc.stdin.drain()
                proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            # It had its chance to exit politely.
            try:
                proc.kill()
            except ProcessLookupError:
                pass


class TeeIndicator:
    """Fans one state announcement out to several indicators.

    Exists for the web GUI: its state pill wants the same transitions the
    HUD gets, and teaching the pipeline to announce twice would spread
    the fan-out across every call site. Each target keeps the protocol's
    failure tolerance for itself; the tee adds none of its own beyond
    guarding the synchronous set_state, which is called from the audio
    frame loop and must survive any one target misbehaving.
    """

    def __init__(self, *targets: Indicator) -> None:
        self._targets = targets

    async def start(self) -> None:
        await asyncio.gather(*(t.start() for t in self._targets))

    def set_state(self, state: str) -> None:
        for target in self._targets:
            try:
                target.set_state(state)
            except Exception:  # noqa: BLE001 - one bad light must not dim the rest
                log.debug("indicator set_state failed", exc_info=True)

    async def close(self) -> None:
        await asyncio.gather(
            *(t.close() for t in self._targets), return_exceptions=True
        )


def build_indicator(config: UIConfig) -> Indicator:
    """Pick an indicator from config."""
    if config.indicator == "none":
        return NullIndicator()
    if config.indicator == "terminal":
        return TerminalIndicator()
    if config.indicator == "hud":
        if sys.platform != "darwin":
            log.warning("the HUD is macOS-only — using the terminal indicator")
            return TerminalIndicator()
        return HudIndicator(config)
    return NullIndicator()


__all__ = [
    "Indicator",
    "HudIndicator",
    "TerminalIndicator",
    "NullIndicator",
    "TeeIndicator",
    "build_indicator",
    "STATES",
]
