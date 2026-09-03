"""The spoke's socket to the hub: connect, hello, send, reconnect.

A small WebSocket client with the Chart page's habits, in Python: it
speaks first (the hello carries the role, the token, and the client
id), it holds every ``say`` until the hub's ack and resends the unacked
after a reconnect (a turn the user watched themselves speak must not
vanish into a half-open socket), and it backs off on failure rather
than hammering. Everything else — what the frames mean — belongs to
the frontend; this module only carries them.

Degrades to nothing, loudly-then-quietly: a hub that is down costs one
warning and a reconnect loop; the frontend is told on every transition
so it can say so once and chime after.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Any, Callable

from ciel import wire
from ciel.config import SpokeConfig

log = logging.getLogger(__name__)


class HubClient:
    """The socket, with reconnect and the say ledger."""

    def __init__(
        self,
        config: SpokeConfig,
        *,
        on_frame: Callable[[dict[str, Any]], None],
        on_connect: Callable[[dict[str, Any]], None],
        on_disconnect: Callable[[], None],
    ) -> None:
        self._config = config
        self._on_frame = on_frame
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._say_seq = 0
        self._unacked: dict[int, dict[str, Any]] = {}
        """seq → the say frame, until its ack; insertion order is send
        order, which is the resend order."""
        self.attempts = 0
        """Consecutive failed connection attempts — the frontend's cue
        for "say it once, then chime"."""

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        try:
            import aiohttp  # noqa: F401 - presence check
        except ImportError:
            log.error("aiohttp is not installed — the spoke cannot reach the hub "
                      "(install with: uv sync --extra web)")
            return
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    # ── outbound ─────────────────────────────────────────────────────────────

    def send(self, frame: dict[str, Any]) -> bool:
        """One frame up, fire-and-forget; False when not connected. The
        catalog is checked at the source."""
        wire.validate(frame, "c2h")
        ws = self._ws
        if ws is None or ws.closed:
            return False
        asyncio.get_running_loop().create_task(self._send_now(ws, frame))
        return True

    def say(self, text: str, lane: str = "voice") -> bool:
        """A turn: numbered, held until acked, resent after a reconnect.
        Returns whether it could be sent *now*; a held say still goes
        the moment the socket is back."""
        self._say_seq += 1
        frame = {"type": "say", "text": text, "lane": lane, "seq": self._say_seq}
        self._unacked[self._say_seq] = frame
        return self.send(frame)

    async def _send_now(self, ws: Any, frame: dict[str, Any]) -> None:
        try:
            await ws.send_str(json.dumps(frame))
        except Exception:  # noqa: BLE001 - the read loop notices the death
            log.debug("send failed", exc_info=True)

    # ── the loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        import aiohttp

        backoff = 0.5
        async with aiohttp.ClientSession() as session:
            while not self._closing:
                try:
                    await self._session(session)
                    backoff = 0.5
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - every failure is a retry
                    self.attempts += 1
                    if self.attempts == 1:
                        log.warning("hub unreachable at %s (%s)", self._config.hub, exc)
                    else:
                        log.debug("hub still unreachable (%s)", exc)
                await asyncio.sleep(backoff + random.uniform(0, backoff / 4))
                backoff = min(backoff * 2, self._config.reconnect_max_s)

    async def _session(self, session: Any) -> None:
        import aiohttp

        async with session.ws_connect(
            self._config.hub, heartbeat=30.0, autoping=True
        ) as ws:
            hello = {
                "type": "hello", "v": wire.WIRE_VERSION, "role": "spoke",
                "client_id": self._config.client_id,
                "caps": ["acks", "voice"],
            }
            token = self._config.current_token()
            if token:
                hello["token"] = token
            await ws.send_str(json.dumps(hello))
            msg = await asyncio.wait_for(
                ws.receive(), timeout=self._config.hello_timeout_s
            )
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError(f"hub closed during hello ({ws.close_code})")
            first = wire.decode(msg.data, "h2c")
            if first["type"] == "error":
                raise ConnectionError(
                    f"hub refused us: {first.get('reason')} ({first.get('code')})"
                )
            if first["type"] != "hello":
                raise ConnectionError(f"expected a hello, got {first['type']!r}")
            self._ws = ws
            if self.attempts:
                log.info("hub reachable again after %d attempt(s)", self.attempts)
            self.attempts = 0
            try:
                self._on_connect(first)
                for frame in list(self._unacked.values()):
                    # Settle the ledger: whatever the last socket
                    # swallowed rides again.
                    await ws.send_str(json.dumps(frame))
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        frame = wire.decode(msg.data, "h2c")
                    except wire.WireError as exc:
                        log.debug("hub frame refused by the catalog: %s", exc)
                        continue
                    if frame["type"] == "ack":
                        self._unacked.pop(frame["seq"], None)
                        continue
                    if frame["type"] == "pong":
                        continue
                    try:
                        self._on_frame(frame)
                    except Exception:  # noqa: BLE001 - the frontend's bug stays its own
                        log.exception("frame handling failed")
            finally:
                self._ws = None
                self._on_disconnect()
        raise ConnectionError(f"hub closed the socket ({ws.close_code})")


__all__ = ["HubClient"]
