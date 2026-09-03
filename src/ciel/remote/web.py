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

  The client speaks first. Its hello — ``{"type": "hello", "v": 1,
  "role": str, "token": str, "client_id": str, "caps": [str…],
  "resume": {"epoch": str, "seq": int}}`` — says who is calling, proves
  it (the token, required off loopback), and where it left off. The
  server answers with its own hello, and only then does the feed start.
  A socket that says nothing for ``[hub].hello_timeout_s`` is closed; a
  loopback client whose first frame is not a hello is admitted as if it
  had sent an anonymous one (the pre-handshake page keeps working). The
  full catalog, the codec, and the close codes live in ``ciel.wire``.

  Server → client:

  * ``{"type": "hello", "v": 1, "epoch": str, "seq": int, "acks": true,
    "resumed": bool, "muted": bool, "state": str, "agents": [agent…],
    "history": [row…]}`` — the answer to the client's hello: the mute
    switch's position, the current indicator state, the active-agent
    roster, and either the last ``history_lines`` transcript rows (a
    fresh connection: ``resumed`` false) or nothing at all, followed by
    exactly the broadcast frames the client missed (``resumed`` true —
    its resume claim matched this ``epoch`` and the ring still held its
    ``seq``; the client keeps its screen). ``seq`` is the ring's current
    position either way. ``acks`` advertises the receipt lane (ack,
    pong, restart): the page is served from disk, so it can be newer
    than the process behind the socket — a client seeing no flag is
    talking to an older server and must fall back to fire-and-forget
    sends, no probe, no resend.
  * Every broadcast frame below carries a ``seq``: monotonic for this
    server process, the number a client hands back in its next resume
    claim. Private frames (ack, pong, error) carry none.
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
  * ``{"type": "error", "code": int, "reason": str}`` — the last word
    before a refusal closes the socket; ``code`` repeats as the close
    code (4401 unauthorized — ask the user for the token; 4400 a
    first frame that was not a hello; 4408 no hello in time).

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

Identity is the reach, on loopback: the server binds loopback by
default, so being able to open the socket means being at the machine —
the typed lane's own trust model. Bound to the tailnet (``[hub].bind``),
reach is no longer enough, and every non-loopback peer must present the
hub token in its hello — Tailscale keeps strangers off the wire, the
token keeps other tailnet devices off the brain. The one browser-shaped
hole is cross-site WebSocket access (any web page may try
``ws://127.0.0.1``), so ``/ws`` refuses upgrades whose Origin is present
but neither loopback nor the bind address (nor a configured MagicDNS
name) — deliberately not "whatever the Host header says", because a
DNS-rebinding page would say exactly that; native clients send no
Origin and pass. This lane feeds text straight into a brain with tools —
the checks are small, but they are the whole difference between "the
user's page" and "whatever tab is open".

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
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ciel import wire
from ciel.config import HubConfig, WebConfig

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


def origin_allowed(
    origin: str | None, port: int, hosts: tuple[str, ...] = ()
) -> bool:
    """Whether a WebSocket upgrade may proceed, judged by its Origin.

    No Origin passes — that is a native client (or curl), which reached
    the port and is judged by the hello instead. An Origin that parses to
    loopback on our port is our own served page; so is one on any of
    ``hosts`` — the bind address and the configured names, never the
    request's own Host header. Everything else is some other page a
    browser happens to have open, and is refused before a single frame
    is read.
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
    allowed = _LOOPBACK_HOSTS | {h.lower() for h in hosts if h}
    return host in allowed and (parts.port or default_port) == port


def loopback_peer(remote: str | None) -> bool:
    """Whether a socket's far end is this machine. Unknown (None, or not
    an address) is *not* loopback: trust is granted to a peer that is
    provably local, never to one that merely failed to identify."""
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Admission:
    """The verdict on a socket's first frame."""

    ok: bool
    code: int = 0
    reason: str = ""
    role: str = "chart"
    client_id: str = ""
    resume: wire.Resume | None = None
    implicit: bool = False
    """The first frame was not a hello and the peer was trusted anyway:
    the caller processes that frame after the welcome, so an old page's
    first say is not lost to the handshake it predates."""


def admit(
    first: dict[str, Any] | None,
    *,
    loopback: bool,
    token: str,
    require_token: bool,
) -> Admission:
    """Judge a socket by its first frame — the whole auth policy, pure.

    ``first`` is the decoded frame, or None when none arrived in time.
    A loopback peer is trusted by reach unless ``require_token``; every
    other peer must send a hello carrying ``token``. The comparison is
    constant-time; an empty server token admits nobody off loopback,
    which is the fail-closed reading of "not configured".
    """
    if first is None:
        return Admission(False, wire.CLOSE_HELLO_TIMEOUT, "no hello")
    trusted = loopback and not require_token
    if first.get("type") != "hello":
        if trusted:
            return Admission(True, implicit=True)
        return Admission(False, wire.CLOSE_BAD_HELLO, "hello first")
    if not trusted:
        presented = first.get("token")
        if (
            not token
            or not isinstance(presented, str)
            or not hmac.compare_digest(presented.encode(), token.encode())
        ):
            return Admission(False, wire.CLOSE_UNAUTHORIZED, "bad token")
    role = first.get("role")
    client_id = first.get("client_id")
    return Admission(
        True,
        role=role if isinstance(role, str) and role else "chart",
        client_id=client_id if isinstance(client_id, str) else "",
        resume=wire.Resume.from_frame(first),
    )


def _mint_token(path: Path) -> str:
    """A fresh hub token, written owner-only. The user copies it into a
    client once; nothing else ever reads the file — it is on the model's
    forbidden list by name."""
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token


class WebLink:
    """The GUI's server half: a turn queue, a broadcast fan-out, a page."""

    def __init__(self, config: WebConfig, hub: HubConfig | None = None) -> None:
        self._config = config
        self._hub = hub or HubConfig()
        self._token = ""
        """Resolved at start(): the configured token, or the minted one."""
        self._ring = wire.ReplayRing(
            maxlen=self._hub.resume_frames, grace_s=self._hub.resume_grace_s
        )
        """Every broadcast frame, seq-stamped, for resume — the ring is
        this process's memory of what it said, so it numbers frames even
        into an empty room: a client that reconnects after the room was
        empty for a while still resumes."""
        self._peers: dict[Any, tuple[str, str]] = {}
        """ws → (role, client_id), for the log lines."""
        self.stir = asyncio.Event()
        """Set on every inbound frame and every client change — the
        hub's arbiter waits on it instead of polling the queues."""
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
    def bind_host(self) -> str:
        """Where the socket listens: ``[hub].bind``, else ``[web].host``."""
        return self._hub.bind or self._config.host

    @property
    def url(self) -> str:
        """The page's address as the ready line prints it."""
        return f"http://{self.bind_host}:{self._config.port}"

    @property
    def epoch(self) -> str:
        """This process's ring identity, as the hello reports it."""
        return self._ring.epoch

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
        """Every client, in order — and the ring, always: a frame said
        to an empty room is still a frame the next resume must not skip."""
        if payload.get("type") in wire.BROADCAST_TYPES:
            data = self._ring.stamp(payload)
        else:
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

        host = self.bind_host
        self._token = self._hub.current_token()
        if not self._token and (
            self._hub.require_token or host not in _LOOPBACK_HOSTS
        ):
            # Off loopback, reach is not identity — and an unset token
            # would admit nobody, which reads as "the phone can't
            # connect" with no clue why. Mint one and say where it is.
            try:
                self._token = _mint_token(self._hub.token_file)
                log.warning(
                    "hub token minted at %s — paste it into the Chart once",
                    self._hub.token_file,
                )
            except OSError as exc:
                log.warning(
                    "could not write the hub token to %s (%s) — "
                    "non-loopback clients will be refused",
                    self._hub.token_file, exc,
                )

        app = web.Application()
        app.router.add_get("/", self._serve_page)
        app.router.add_get("/ws", self._serve_ws)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, host, self._config.port)
            await self._site.start()
        except OSError as exc:
            log.warning(
                "web GUI could not bind %s:%d (%s) — the GUI is off",
                host, self._config.port, exc,
            )
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            return
        log.info("web GUI at %s", self.url)

    async def close(self) -> None:
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.close()
        self._clients.clear()
        self._peers.clear()
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

        origins = (self.bind_host,) + tuple(self._hub.origins)
        if not origin_allowed(
            request.headers.get("Origin"), self._config.port, origins
        ):
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
        peer = request.remote

        # The client speaks first: its hello carries the token and the
        # resume claim. Nothing is queued, nothing is registered, until
        # the verdict — a refused socket never sees a row.
        first: dict[str, Any] | None = None
        first_raw: str | None = None
        try:
            msg = await asyncio.wait_for(
                ws.receive(), timeout=self._hub.hello_timeout_s
            )
        except asyncio.TimeoutError:
            msg = None
        if msg is not None:
            if msg.type != web.WSMsgType.TEXT:
                return ws  # closed, or a binary frame nobody speaks
            first_raw = msg.data
            try:
                first = wire.decode(first_raw, "c2h")
            except wire.WireError as exc:
                log.debug("first frame refused by the catalog: %s", exc)
                first = {"type": "malformed"}
                first_raw = None
        verdict = admit(
            first,
            loopback=loopback_peer(peer),
            token=self._token,
            require_token=self._hub.require_token,
        )
        if not verdict.ok:
            log.warning(
                "refused a wire client from %s: %s (%d)",
                peer, verdict.reason, verdict.code,
            )
            with contextlib.suppress(Exception):
                await ws.send_str(wire.encode(
                    {"type": "error", "code": verdict.code,
                     "reason": verdict.reason},
                    "h2c",
                ))
                await ws.close(
                    code=verdict.code, message=verdict.reason.encode()
                )
            return ws

        queue, resumed = self._welcome(ws, verdict)
        self.stir.set()
        writer = asyncio.create_task(self._write_frames(ws, queue))
        self._writers.add(writer)
        writer.add_done_callback(self._writers.discard)
        log.debug(
            "wire client connected: role=%s id=%s from %s%s (%d open)",
            verdict.role, verdict.client_id or "-", peer,
            " (resumed)" if resumed else "", len(self._clients),
        )
        if verdict.implicit and first_raw is not None:
            # An older page's first say, honored after its welcome.
            self._on_frame(first_raw, ws)

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                self._on_frame(msg.data, ws)
        except Exception:  # noqa: BLE001 - one client's tantrum stays its own
            log.debug("web client errored", exc_info=True)
        finally:
            self._clients.pop(ws, None)
            self._peers.pop(ws, None)
            writer.cancel()
            self._client_left(ws)
            log.debug("web client left (%d open)", len(self._clients))
        return ws

    def _client_left(self, ws: Any) -> None:
        """A socket is gone — the hub server's hook (the spoke seat)."""
        return None

    def _welcome(
        self, ws: Any, verdict: Admission
    ) -> tuple[asyncio.Queue[str], bool]:
        """Register an admitted client and queue its hello — plus the
        frames it missed, when its resume claim holds. Returns the
        client's queue and whether the claim held.

        The hello rides the same queue as every later frame, so exactly
        one task ever writes this socket (concurrent send_str on one
        WebSocket is not allowed) — and this method is await-free, so no
        row can fall between the picture the hello carries and the live
        feed that follows it. A replay too long for the outbound queue
        is a fresh hello instead: the history window is the older, and
        the bounded, catch-up.
        """
        replay = self._ring.since(verdict.resume)
        if replay is not None and len(replay) >= _CLIENT_QUEUE_MAX - 1:
            replay = None
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)
        queue.put_nowait(json.dumps({
            "type": "hello",
            "v": wire.WIRE_VERSION,
            "epoch": self._ring.epoch,
            "seq": self._ring.seq,
            "acks": True,
            "resumed": replay is not None,
            "muted": self._muted,
            "state": self._state,
            "agents": self._agents,
            "history": [] if replay is not None else list(self._history),
        }))
        for data in replay or ():
            queue.put_nowait(data)
        self._clients[ws] = queue
        self._peers[ws] = (verdict.role, verdict.client_id)
        return queue, replay is not None

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
            try:
                frame = wire.decode(raw, "c2h")
            except wire.WireError as exc:
                log.debug("web frame refused by the catalog: %s", exc)
                return
            self.stir.set()
            kind = frame.get("type")
            if kind == "hello":
                # A second hello mid-session: nothing to renegotiate —
                # the socket already has its welcome. Ignored, not
                # refused, so a client that re-sends on a hiccup is fine.
                return
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
                    self.on_mute(frame["muted"])
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


__all__ = [
    "Admission",
    "WebIndicator",
    "WebLink",
    "admit",
    "loopback_peer",
    "origin_allowed",
]
