"""The hub's wire server: the Chart's link, plus the spoke's seat.

``WebLink`` already serves every client of the socket — the browser
Chart, and whatever native chart comes next — with a turn queue, a
broadcast fan-out, and the door (hello, token, resume). This is the
same server with one client singled out: the **spoke**, the process
that owns the microphone and the speakers. The spoke's frames are not
a chart's — its ``say`` is a *spoken* turn (label ``user``, presence
evidence), its ``turn.played`` receipts pace the hub's voice sink,
its ``confirm.answer`` is the user's voice answering a question the
hub asked through it — so they are routed here, to the pipeline's
voice-lane hooks, instead of into the Chart's queue.

One seat. A second spoke that connects takes the seat from the first
(one mouth, one ear — the endgame's per-device agents come later, with
their own roles). A spoke that drops fails every wait the hub had open
on it, so a turn mid-sentence abandons instead of hanging, and the
pipeline is told so it can cancel a pending confirmation the way it
does on barge-in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable

from ciel import wire
from ciel.config import HubConfig, WebConfig
from ciel.remote.web import Admission, WebLink

log = logging.getLogger(__name__)


class RpcUnavailable(RuntimeError):
    """A tool call could not reach the Mac, or the Mac did not answer."""


class HubServer(WebLink):
    """``WebLink`` with the spoke seat."""

    def __init__(self, config: WebConfig, hub: HubConfig | None = None) -> None:
        super().__init__(config, hub)
        self._spoke: Any = None
        """The seated spoke's socket, or None."""
        self._voice: deque[tuple[float, str, None]] = deque()
        """Spoken turns the spoke sent up, awaiting the arbiter — the
        Chart queue's shape, so the pipeline pops it the same way."""
        self._played: dict[tuple[str, int], asyncio.Future[bool]] = {}
        """(turn_id, n) → the sentence's playback receipt."""
        self._delivered: dict[str, asyncio.Future[bool]] = {}
        """event_id → the delivery's receipt."""
        self._rpc: dict[str, asyncio.Future[dict[str, Any]]] = {}
        """rpc_id → the tool call's result frame."""
        self._rpc_seq = 0
        self.on_event: Callable[[dict[str, Any]], bool] | None = None
        """A published event from the spoke's watchers: the queue's push.
        Returns whether it was new; either way the publish is acked."""
        self.on_presence: Callable[[dict[str, Any]], None] | None = None
        """The spoke's heartbeat, raw — the presence view folds it in."""
        self.spoke_listening = False
        """The spoke's last reported listening state — a window is open."""
        self.spoke_speaking = False
        """The spoke's last reported endpointer state — speech in hand."""
        self.on_confirm_answer: Callable[[str], bool] | None = None
        """The broker's ``answer``: a spoken yes or no came back."""
        self.on_turn_cancel: Callable[[str, str], None] | None = None
        """The spoke barged in between sentences: (turn_id, reason)."""
        self.on_spoke_mute: Callable[[bool], None] | None = None
        """The spoke's mute switch moved (its sentinel, its own poll)."""
        self.on_spoke_change: Callable[[bool], None] | None = None
        """A spoke took the seat (True) or left it (False)."""

    # ── the seat ─────────────────────────────────────────────────────────────

    @property
    def spoke_connected(self) -> bool:
        return self._spoke is not None and self._spoke in self._clients

    @property
    def voice_pending(self) -> bool:
        return bool(self._voice)

    def peek_voice(self) -> tuple[float, str, None] | None:
        return self._voice[0] if self._voice else None

    def pop_voice(self) -> tuple[float, str, None] | None:
        return self._voice.popleft() if self._voice else None

    def send_spoke(self, frame: dict[str, Any]) -> bool:
        """One private frame to the seated spoke; False when the seat is
        empty. Validated against the catalog at the source."""
        if not self.spoke_connected:
            return False
        self._send_to(self._spoke, wire.validate(frame, "h2c"))
        return True

    async def speak(
        self, turn_id: str, n: int, kind: str, text: str, timeout: float
    ) -> bool:
        """Send sentence ``n`` of a turn down and await its receipt:
        True when it played to completion, False when it was stopped
        (barge-in, a dead device), the spoke left, or nothing came back
        in ``timeout`` — every False means "abandon the turn"."""
        return await self._await_receipt(
            self._played, (turn_id, n),
            {"type": "turn.sentence", "turn_id": turn_id, "n": n,
             "kind": kind, "text": text},
            timeout,
        )

    async def deliver(
        self, event_id: str, text: str, meta: dict[str, Any], timeout: float
    ) -> bool:
        """An unprompted line — a timer, a Vigil nudge — spoken at the
        spoke's idle, with its ring if ``meta`` asks. True when the spoke
        reports it played; False is "nothing reached the room"."""
        return await self._await_receipt(
            self._delivered, event_id,
            {"type": "deliver.speak", "event_id": event_id, "text": text,
             "meta": meta},
            timeout,
        )

    async def rpc(
        self, tool: str, args: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """One tool call on the spoke: ``tool.request`` down, its
        ``tool.result`` back. Returns the result frame (``ok``, and
        ``content`` or ``error``); raises :class:`RpcUnavailable` when
        there is no spoke, it left mid-call, or nothing came back in
        ``timeout`` — the caller turns that into words for the model."""
        if not self.spoke_connected:
            raise RpcUnavailable("the Mac is not connected")
        self._rpc_seq += 1
        rpc_id = f"r{self._rpc_seq}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._rpc[rpc_id] = fut
        try:
            self.send_spoke({
                "type": "tool.request", "rpc_id": rpc_id, "tool": tool,
                "args": args, "timeout_s": timeout,
            })
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                self.send_spoke({"type": "tool.cancel", "rpc_id": rpc_id})
                raise RpcUnavailable(f"the Mac did not answer in {timeout:.0f}s")
        finally:
            self._rpc.pop(rpc_id, None)

    async def _await_receipt(
        self, table: dict, key: Any, frame: dict[str, Any], timeout: float
    ) -> bool:
        if not self.spoke_connected:
            return False
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        table[key] = fut
        try:
            if not self.send_spoke(frame):
                return False
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            log.warning("no receipt from the spoke for %s in %.0fs", key, timeout)
            return False
        finally:
            table.pop(key, None)

    def _fail_waits(self) -> None:
        """Every open wait resolves: the spoke is gone."""
        for fut in list(self._played.values()) + list(self._delivered.values()):
            if not fut.done():
                fut.set_result(False)
        for fut in list(self._rpc.values()):
            if not fut.done():
                fut.set_exception(RpcUnavailable("the Mac disconnected mid-call"))

    # ── the door, extended ───────────────────────────────────────────────────

    def _welcome(
        self, ws: Any, verdict: Admission
    ) -> tuple[asyncio.Queue[str], bool]:
        queue, resumed = super()._welcome(ws, verdict)
        if verdict.role == "spoke":
            old = self._spoke
            self._spoke = ws
            self.spoke_listening = False
            self.spoke_speaking = False
            if old is not None and old is not ws and old in self._clients:
                # The newer spoke takes the seat; the older one is shed
                # the way a fallen-behind chart is — forgotten here,
                # closed asynchronously.
                log.warning("a second spoke connected — replacing the first")
                self._clients.pop(old, None)
                self._peers.pop(old, None)
                asyncio.get_running_loop().create_task(self._drop(old))
            log.info("spoke seated (%s)", verdict.client_id or "-")
            if self.on_spoke_change is not None:
                self.on_spoke_change(True)
        return queue, resumed

    def _client_left(self, ws: Any) -> None:
        if ws is self._spoke:
            self._spoke = None
            self.spoke_listening = False
            self.spoke_speaking = False
            self._fail_waits()
            log.warning("spoke left the seat")
            if self.on_spoke_change is not None:
                self.on_spoke_change(False)
        self.stir.set()

    # ── inbound ──────────────────────────────────────────────────────────────

    def _on_frame(self, raw: str, ws: Any = None) -> None:
        if ws is None or ws is not self._spoke:
            super()._on_frame(raw, ws)
            return
        try:
            frame = wire.decode(raw, "c2h")
        except wire.WireError as exc:
            log.debug("spoke frame refused by the catalog: %s", exc)
            return
        self.stir.set()
        kind = frame["type"]
        try:
            if kind == "say":
                seq = frame.get("seq")
                if isinstance(seq, int):
                    self._send_to(ws, {"type": "ack", "seq": seq})
                text = frame["text"].strip()
                if not text:
                    return
                lane = frame.get("lane") or "voice"
                if lane != "voice":
                    log.debug("spoke say on lane %r ignored (voice only)", lane)
                    return
                if len(text) > self._config.max_inbound_chars:
                    text = text[: self._config.max_inbound_chars]
                self._voice.append((time.monotonic(), text, None))
            elif kind == "turn.played":
                fut = self._played.get((frame["turn_id"], frame["n"]))
                if fut is not None and not fut.done():
                    fut.set_result(frame["completed"])
            elif kind == "deliver.result":
                fut = self._delivered.get(frame["event_id"])
                if fut is not None and not fut.done():
                    fut.set_result(frame["ok"])
            elif kind == "tool.result":
                fut = self._rpc.get(frame["rpc_id"])
                if fut is not None and not fut.done():
                    fut.set_result(frame)
            elif kind == "event.publish":
                # At-least-once from the publisher: the queue's own dedupe
                # absorbs a resend, and the ack goes back either way so
                # the outbox can let it go.
                if self.on_event is not None:
                    try:
                        self.on_event(frame["event"])
                    except Exception:  # noqa: BLE001 - a bad event is logged, still acked
                        log.exception("published event refused")
                self._send_to(ws, {"type": "event.ack", "publish_id": frame["publish_id"]})
            elif kind == "presence":
                if self.on_presence is not None:
                    self.on_presence(frame)
            elif kind == "turn.cancel":
                if self.on_turn_cancel is not None:
                    self.on_turn_cancel(frame["turn_id"], frame.get("reason") or "")
            elif kind == "confirm.answer":
                if self.on_confirm_answer is not None:
                    self.on_confirm_answer(frame["text"])
            elif kind == "voice.state":
                self.spoke_listening = frame["listening"]
                self.spoke_speaking = frame["speaking"]
            elif kind == "mute":
                if self.on_spoke_mute is not None:
                    self.on_spoke_mute(frame["muted"])
            elif kind == "ping":
                self._send_to(ws, {"type": "pong"})
            elif kind == "hello":
                return
            else:
                log.debug("spoke frame of type %r ignored", kind)
        except Exception:  # noqa: BLE001 - one frame's failure stays its own
            log.debug("spoke frame handling failed", exc_info=True)


__all__ = ["HubServer", "RpcUnavailable"]
