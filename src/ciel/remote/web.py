"""The web lane: a local GUI onto the running assistant.

Codename: **Chart** — in the atlas sense: a local coordinate window onto
the same manifold. The typed lane (Isomorphism) maps a line on stdin onto
a spoken turn; this lane draws the whole surface into view: a small
loopback web server whose page shows the conversation live — every
lane's turns, the state pill, a mute switch — and takes typed turns of
its own for the rooms where talking is not an option (a class, a
library). The web page is merely the first chart; a native app later is
another chart of the same atlas, because the server speaks a small JSON
protocol and never knows what is drawing it.

**The protocol** (the contract any future client builds against):

* ``GET /`` serves the bundled page. A native client skips it entirely.
* ``GET /ws`` upgrades to a WebSocket. One socket per client; every
  frame each way is one JSON object with a ``type`` field.

  Server → client:

  * ``{"type": "hello", "acks": true, "muted": bool, "state": str,
    "agents": [agent…], "history": [row…]}`` — sent once on connect: the
    mute switch's position, the current indicator state, the
    active-agent roster, and the last ``history_lines`` transcript rows
    so a client opening mid-conversation shows the conversation.
    ``acks`` advertises the receipt lane (ack, pong, restart): the page
    is served from disk, so it can be newer than the process behind the
    socket — a client seeing no flag is talking to an older server and
    must fall back to fire-and-forget sends, no probe, no resend.
  * ``{"type": "row", "role": str, "text": str, "t": float}`` — one
    transcript row as it lands, every lane included. Roles are the
    transcript's speaker labels verbatim (``user``, ``user-web``,
    ``ciel``, ``ciel-thinking``, ``ciel-confirm``, ``event``, …); render
    unknown ones as event-style rows, because new lanes add new labels.
  * ``{"type": "state", "state": str}`` — the indicator vocabulary
    (``idle``/``listening``/``thinking``/``reasoning``/``speaking``/
    ``error``), deduplicated at the source.
  * ``{"type": "muted", "muted": bool}`` — the mute switch moved, by
    whoever moved it (this client, another client, ``touch ~/.ciel/mute``).
  * ``{"type": "agents", "agents": [agent…]}`` — the roster of active
    agents changed. An agent is everything working on the user's behalf
    right now: ``{"id": str, "kind": str, "label": str,
    "detail": str|null, "since": epoch|null, "until": epoch|null}``.
    Kinds today: ``deep`` (a deep-thought pass in flight), ``watch`` (a
    background completion watch), ``timer``/``alarm``. Render unknown
    kinds as plain rows — new agent sources add new kinds.
  * ``{"type": "confirm", "text": str}`` — a confirm-tier question is
    waiting on a yes or no *right now*; show it actionably. The question
    also arrives as a ``ciel-confirm`` row, which is the historical
    record — this frame is the live prompt.
  * ``{"type": "ack", "seq": int}`` — a delivery receipt, sent only to
    the client whose ``say`` carried that ``seq``.
  * ``{"type": "pong"}`` — the reply to a ``ping``, sent only to the
    asker.

  Client → server:

  * ``{"type": "say", "text": str, "seq": int}`` — one typed turn (or
    the answer to a pending confirmation; the pipeline tells the
    difference by timing, exactly as it does for the keyboard). ``seq``
    is optional; a say carrying an ``int`` one is acked back to its
    sender (any other type is ignored rather than echoed — one NaN
    would poison the receipt stream of a json-lenient client).
    The ack exists for the half-open socket a sleeping machine or a
    frozen tab leaves behind: the browser still reports OPEN after the
    server has long since dropped the connection, so a send can succeed
    locally and arrive nowhere. A client that cares holds each say
    until its ack and resends the unacked after a reconnect — late,
    never voided. At-least-once on purpose: the ack rides the outbound
    queue, so a connection dying between the two can double a line —
    a visible, cheap failure where the void was a silent one.
  * ``{"type": "ping"}`` — a liveness probe, answered with ``pong``.
    For the same half-open sockets: protocol-level pings are invisible
    to page script, so a page returning from a nap asks at the app
    layer and reconnects if nothing comes back.
  * ``{"type": "mute", "muted": bool}`` — move the mute switch.
  * ``{"type": "restart"}`` — ask for a clean restart: the same re-exec
    a typed "reload" queues, performed from idle, never mid-conversation.

Identity is the reach: the server binds loopback, so being able to open
the socket means being at the machine — the typed lane's own trust
model. The one browser-shaped hole is cross-site WebSocket access (any
web page may try ``ws://127.0.0.1``), so ``/ws`` refuses upgrades whose
Origin is present but not loopback; native clients send no Origin and
pass. This lane feeds text straight into a brain with tools — the check
is small, but it is the whole difference between "the user's page" and
"whatever tab is open".

Built hub-shaped like the Discord lane: the pipeline sees the same
queue-and-send surface — ``pending``/``peek``/``pop_batch`` inbound,
``send`` outbound — plus four taps (``note_row``/``note_state``/
``note_muted``/``note_agents``) that exist because a GUI is a *view*, not
just a lane.

Degrades to nothing, loudly-then-quietly: a missing dependency or a
taken port is one warning and a disabled lane; a slow or dead client is
dropped alone. The GUI must never be able to take Ciel down with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ciel.config import WebConfig

log = logging.getLogger(__name__)

_PAGE = Path(__file__).with_name("chart.html")
"""The bundled page, read per request on purpose: it is developer-editable
UI, and a browser refresh should show an edit without restarting Ciel."""

_CLIENT_QUEUE_MAX = 512
"""Outbound frames a client may fall behind before being dropped. A
browser that stops reading (a laptop lid, a wedged tab) must shed its
backlog by losing the connection, not by growing it in Ciel's memory —
it reconnects and the hello replay catches it up."""

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def origin_allowed(origin: str | None, port: int) -> bool:
    """Whether a WebSocket upgrade may proceed, judged by its Origin.

    No Origin passes — that is a native client (or curl), which reached a
    loopback-bound port and therefore *is* at the machine. An Origin that
    parses to loopback on our port is our own served page. Everything
    else is some other page a browser happens to have open, and is
    refused before a single frame is read.
    """
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    default_port = 443 if parts.scheme == "https" else 80
    return host in _LOOPBACK_HOSTS and (parts.port or default_port) == port


class WebLink:
    """The GUI's server half: a turn queue, a broadcast fan-out, a page."""

    def __init__(self, config: WebConfig) -> None:
        self._config = config
        self._queue: deque[tuple[float, str, None]] = deque()
        """Inbound turns awaiting the frame loop, oldest first:
        ``(arrival, text, None)`` — the Discord deque's shape, with the
        channel pinned to None because every reply broadcasts to every
        open chart: they are all the same user looking at the same
        conversation."""
        self._history: deque[dict[str, Any]] = deque(
            maxlen=max(0, config.history_lines)
        )
        self._clients: dict[Any, asyncio.Queue[str]] = {}
        """ws → its outbound frame queue. One writer task per client
        drains its queue in order — a shared broadcast that awaited every
        socket would let the slowest tab pace the pipeline."""
        self._writers: set[asyncio.Task[None]] = set()
        self._state = "idle"
        self._muted = False
        self._agents: list[dict[str, Any]] = []
        self._runner: Any = None
        self._site: Any = None
        self.on_mute: Callable[[bool], None] | None = None
        """Set by the pipeline: the GUI's mute switch calls here. The
        link never owns mute state — it only displays and relays it,
        because the thing being silenced (the speakers, the wake word)
        lives in the pipeline."""
        self.on_restart: Callable[[], None] | None = None
        """Set by the pipeline: the GUI's restart button calls here.
        The same ownership rule as on_mute — the re-exec belongs to the
        pipeline's frame loop; the link only relays the request."""

    # ── inbound: the turn queue ──────────────────────────────────────────────

    @property
    def pending(self) -> bool:
        return bool(self._queue)

    def peek(self) -> tuple[float, str, None] | None:
        return self._queue[0] if self._queue else None

    def pop(self) -> tuple[float, str, None] | None:
        return self._queue.popleft() if self._queue else None

    def pop_batch(self) -> tuple[str, None] | None:
        """Drain everything queued into one turn — the Discord lane's
        burst-coalescing without the channel split, since every chart is
        the same conversation."""
        if not self._queue:
            return None
        lines = [self._queue.popleft()[1]]
        while self._queue:
            lines.append(self._queue.popleft()[1])
        return "\n".join(lines), None

    # ── outbound: the view taps ──────────────────────────────────────────────

    @property
    def serving(self) -> bool:
        """Whether the server actually bound — the claim the ready line
        makes. False until start(), and after a failed bind."""
        return self._site is not None

    @property
    def can_send(self) -> bool:
        """Whether anyone is actually looking at a chart right now."""
        return bool(self._clients)

    async def send(self, text: str, channel: Any = None) -> None:
        """Deliver a live confirmation prompt (``confirm.remote``'s send).

        Never raises on an empty room, unlike the Discord link: the
        question is also a ``ciel-confirm`` transcript row, so a chart
        opened seconds later still shows it — and the broker's timeout
        already owns the nobody-answered case.
        """
        self._broadcast({"type": "confirm", "text": text})

    def note_row(self, speaker: str, text: str) -> None:
        """One transcript row, every lane included — the pipeline's
        ``_record`` tap. Synchronous and non-blocking: it is called from
        the frame loop, which must never wait on a UI."""
        row = {"type": "row", "role": speaker, "text": text, "t": time.time()}
        self._history.append(row)
        self._broadcast(row)

    def note_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._broadcast({"type": "state", "state": state})

    def note_muted(self, muted: bool) -> None:
        self._muted = muted
        self._broadcast({"type": "muted", "muted": muted})

    def note_agents(self, agents: list[dict[str, Any]]) -> None:
        """The active-agent roster tap — everything working on the user's
        behalf right now (a deep-thought pass, watches, timers).
        Deduplicated here like note_state, because the pipeline *polls*
        this once a second rather than wiring change hooks through every
        source that can move the roster — an unchanged poll must cost
        the sockets nothing."""
        if agents == self._agents:
            return
        self._agents = agents
        self._broadcast({"type": "agents", "agents": agents})

    def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        data = json.dumps(payload)
        for ws in list(self._clients):
            self._enqueue(ws, data)

    def _send_to(self, ws: Any, payload: dict[str, Any]) -> None:
        """One client's private frame — the ack and pong lane. A sender
        already dropped (or never known, or None) is a silent miss in
        ``_enqueue`` — its loss."""
        self._enqueue(ws, json.dumps(payload))

    def _enqueue(self, ws: Any, data: str) -> None:
        """One frame onto one client's queue — the fell-behind rule lives
        here alone: a full queue sheds the client rather than growing in
        Ciel's memory. Forgotten here, closed asynchronously: close() can
        block on the very socket that stopped reading, and callers run on
        the frame loop."""
        queue = self._clients.get(ws)
        if queue is None:
            return
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            log.debug("web client fell behind — dropping it")
            self._clients.pop(ws, None)
            asyncio.get_running_loop().create_task(self._drop(ws))

    async def _drop(self, ws: Any) -> None:
        with contextlib.suppress(Exception):
            await ws.close()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind and serve; never blocks startup on failure.

        A missing aiohttp or a taken port is one warning and a disabled
        lane, mirroring how the Discord lane degrades.
        """
        try:
            from aiohttp import web
        except ImportError:
            log.warning(
                "aiohttp is not installed — the web GUI is off "
                "(install with: uv sync --extra web)"
            )
            return

        app = web.Application()
        app.router.add_get("/", self._serve_page)
        app.router.add_get("/ws", self._serve_ws)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(
                self._runner, self._config.host, self._config.port
            )
            await self._site.start()
        except OSError as exc:
            log.warning(
                "web GUI could not bind %s:%d (%s) — the GUI is off",
                self._config.host, self._config.port, exc,
            )
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            return
        log.info(
            "web GUI at http://%s:%d", self._config.host, self._config.port
        )

    async def close(self) -> None:
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.close()
        self._clients.clear()
        for task in list(self._writers):
            task.cancel()
        if self._writers:
            await asyncio.gather(*self._writers, return_exceptions=True)
        self._writers.clear()
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
            self._site = None

    # ── handlers ─────────────────────────────────────────────────────────────

    async def _serve_page(self, request: Any) -> Any:
        from aiohttp import web

        try:
            body = _PAGE.read_text()
        except OSError:
            log.exception("could not read the GUI page")
            raise web.HTTPNotFound
        return web.Response(
            text=body,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def _serve_ws(self, request: Any) -> Any:
        from aiohttp import web

        if not origin_allowed(request.headers.get("Origin"), self._config.port):
            # Some other page in some browser tried its luck. Refused
            # before the upgrade: this socket is a straight line to a
            # brain with tools.
            log.warning(
                "refused a web GUI connection from origin %r",
                request.headers.get("Origin"),
            )
            raise web.HTTPForbidden

        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)

        # The hello rides the same queue as every later frame, so exactly
        # one task ever writes this socket (concurrent send_str on one
        # WebSocket is not allowed) — and the snapshot-then-register block
        # below is await-free, so no row can fall between the history the
        # hello carries and the live feed that follows it.
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)
        queue.put_nowait(json.dumps({
            "type": "hello",
            "acks": True,
            "muted": self._muted,
            "state": self._state,
            "agents": self._agents,
            "history": list(self._history),
        }))
        self._clients[ws] = queue
        writer = asyncio.create_task(self._write_frames(ws, queue))
        self._writers.add(writer)
        writer.add_done_callback(self._writers.discard)
        log.debug("web client connected (%d open)", len(self._clients))

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                self._on_frame(msg.data, ws)
        except Exception:  # noqa: BLE001 - one client's tantrum stays its own
            log.debug("web client errored", exc_info=True)
        finally:
            self._clients.pop(ws, None)
            writer.cancel()
            log.debug("web client left (%d open)", len(self._clients))
        return ws

    async def _write_frames(self, ws: Any, queue: asyncio.Queue[str]) -> None:
        """One client's outbound pump — in-order delivery, isolated death."""
        try:
            while True:
                await ws.send_str(await queue.get())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dead socket ends its own pump
            self._clients.pop(ws, None)

    def _on_frame(self, raw: str, ws: Any = None) -> None:
        """One inbound frame: total, like the Discord message handler —
        a malformed frame costs a debug line, never the socket loop.
        ``ws`` names the sender so the private replies (ack, pong) reach
        it alone; None means a sender already gone, or a caller with no
        use for receipts."""
        try:
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "say":
                seq = frame.get("seq")
                if isinstance(seq, int):
                    # The receipt half of the delivery guarantee (see the
                    # protocol notes). Acked before the blank check on
                    # purpose: received is received, and a page must not
                    # resend forever a line the server will never queue.
                    # Ints only, as the contract says: echoing whatever
                    # arrived would let one NaN (or one huge structure)
                    # poison the sender's own receipt stream.
                    self._send_to(ws, {"type": "ack", "seq": seq})
                text = str(frame.get("text", "")).strip()
                if not text:
                    return
                if len(text) > self._config.max_inbound_chars:
                    text = text[: self._config.max_inbound_chars]
                self._queue.append((time.monotonic(), text, None))
            elif kind == "ping":
                self._send_to(ws, {"type": "pong"})
            elif kind == "mute":
                if self.on_mute is not None:
                    self.on_mute(bool(frame.get("muted")))
            elif kind == "restart":
                if self.on_restart is not None:
                    self.on_restart()
            else:
                log.debug("web frame of unknown type %r ignored", kind)
        except Exception:  # noqa: BLE001
            log.debug("web frame handling failed", exc_info=True)


class WebIndicator:
    """The Indicator-protocol face of a :class:`WebLink`.

    start/close are no-ops on purpose: the pipeline owns the server's
    lifecycle the way it owns the Discord link's — the indicator tee
    must not be able to double-start or half-close the lane.
    """

    def __init__(self, link: WebLink) -> None:
        self._link = link

    async def start(self) -> None:
        return None

    def set_state(self, state: str) -> None:
        self._link.note_state(state)

    async def close(self) -> None:
        return None


__all__ = ["WebLink", "WebIndicator", "origin_allowed"]
