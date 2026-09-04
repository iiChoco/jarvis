"""The wire: the frames every client of the hub speaks, and their codec.

Codename: **Meridian** — the line every chart is drawn against. The
Chart protocol (``remote/web.py``) was written client-agnostic on
purpose: one socket, one JSON object per frame, a ``type`` field. The
hub-and-spoke split takes that promise literally — the Mac spoke, the
browser Chart, and a phone later are all *clients of the same socket*,
the spoke merely the most capable one. This module is that contract
written down once: the catalog of frame types in each direction, what
each must carry, a codec that refuses what the catalog doesn't name,
and the replay ring that lets a client that dropped pick up where it
left off.

**Versioning.** ``WIRE_VERSION`` rides in both hellos. A client older
than the hub keeps working as long as its frames are in the catalog
(fields are only ever added, never repurposed); a client newer than the
hub sees ``v`` in the hub's hello and falls back — the Chart page does
exactly this with the ``acks`` flag, and ``v`` generalizes it.

**Sequence and resume.** Every frame the hub *broadcasts* carries a
``seq`` — monotonic per hub process, under an ``epoch`` the hub mints
at start. A client that reconnects says ``resume: {epoch, seq}`` in its
hello; if the epoch matches and the ring still holds everything after
that seq, the hub replays just those frames and says ``resumed: true``
— the client keeps its screen. Any other case (a new hub process, a
ring that has moved on, a first connection) is a fresh hello with the
history window, which is what the Chart always got. Private frames —
``ack``, ``pong``, ``error`` — carry no seq: they exist only for the
socket they were sent on.

**What this module is not.** No transport, no socket, no async: pure
data and pure functions, probed by ``scripts/probe_wire.py`` with no
network at all. The server half lives in ``remote/web.py``.
"""

from __future__ import annotations

import json
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

WIRE_VERSION = 1
"""Bumped only for an incompatible change; adding a frame type or an
optional field is not one."""

Direction = Literal["c2h", "h2c", "both"]


class WireError(ValueError):
    """A frame the catalog refuses: not JSON, not an object, no ``type``,
    a type unknown in this direction, or a required field missing or of
    the wrong shape. The message says which, for the debug line."""


# ── the catalog ──────────────────────────────────────────────────────────────

# Field shapes. ``int`` deliberately excludes bool — JSON's ``true`` must
# never pass as a sequence number (the ack lane learned this one).
_SHAPES: dict[str, Any] = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "num": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
    "any": lambda v: True,
}


@dataclass(frozen=True, slots=True)
class FrameSpec:
    """One frame type: who sends it, what it must carry, what it may."""

    direction: Direction
    required: dict[str, str] = field(default_factory=dict)
    optional: dict[str, str] = field(default_factory=dict)

    def allows(self, direction: Direction) -> bool:
        return self.direction == "both" or self.direction == direction


CATALOG: dict[str, FrameSpec] = {
    # ── handshake, both ways ────────────────────────────────────────────
    # Client: who is calling, with what, and where it left off.
    # Hub: the current picture (the Chart's hello, plus seq/epoch/v).
    "hello": FrameSpec(
        "both",
        optional={
            "v": "int", "role": "str", "token": "str", "client_id": "str",
            "caps": "list", "resume": "dict",
            "seq": "int", "epoch": "str", "acks": "bool", "resumed": "bool",
            "muted": "bool", "state": "str", "agents": "list", "history": "list",
            "world": "dict",
        },
    ),
    "ping": FrameSpec("both", optional={"t_wall": "num"}),
    "pong": FrameSpec("both", optional={"t_wall": "num"}),
    # ── client → hub ────────────────────────────────────────────────────
    "say": FrameSpec(
        "c2h",
        required={"text": "str"},
        optional={"seq": "int", "lane": "str", "say_id": "str"},
    ),
    "mute": FrameSpec("c2h", required={"muted": "bool"}),
    "restart": FrameSpec("c2h"),
    "turn.cancel": FrameSpec(
        "c2h", required={"turn_id": "str"}, optional={"reason": "str"}
    ),
    # The spoke's playback receipt: sentence ``n`` of ``turn_id`` finished
    # (``completed``) or was stopped — barge-in, or a dead device. The
    # hub's voice sink awaits one per sentence, which is what keeps
    # generation paced to speech and lets a barge-in abort the stream.
    "turn.played": FrameSpec(
        "c2h", required={"turn_id": "str", "n": "int", "completed": "bool"}
    ),
    # The spoke's state machine, for the hub's ladder: whether a
    # listening window is open and whether speech is in hand right now.
    "voice.state": FrameSpec(
        "c2h", required={"listening": "bool", "speaking": "bool"}
    ),
    "confirm.answer": FrameSpec(
        "c2h", required={"confirm_id": "str", "text": "str"}
    ),
    "tool.result": FrameSpec(
        "c2h",
        required={"rpc_id": "str", "ok": "bool"},
        optional={"content": "any", "error": "str"},
    ),
    "event.publish": FrameSpec(
        "c2h", required={"publish_id": "str", "event": "dict"}
    ),
    # The spoke's heartbeat (~10 s, and on change): the raw presence
    # signals the hub folds into its own view, plus the roster of the
    # spoke's active work watches (they live on the machine being watched).
    "presence": FrameSpec(
        "c2h",
        required={"node": "str", "attended": "bool", "locked": "bool"},
        optional={"idle_s": "num", "active_watches": "int", "watches": "list"},
    ),
    "deliver.result": FrameSpec(
        "c2h", required={"event_id": "str", "ok": "bool"}, optional={"error": "str"}
    ),
    # One world fact from the spoke (Phase Space, ``world.py``): a reading
    # the Mac took — its place, the watched sections' spots — for the
    # hub's table. Last-value semantics: a resend after a reconnect
    # replaces, never duplicates, so no ack is needed.
    "fact": FrameSpec(
        "c2h",
        required={"name": "str", "value": "any", "observed_at": "num"},
        optional={"source": "str", "ttl_s": "num", "node": "str"},
    ),
    # ── hub → client ────────────────────────────────────────────────────
    "row": FrameSpec(
        "h2c", required={"role": "str", "text": "str", "t": "num"},
        optional={"seq": "int"},
    ),
    "state": FrameSpec("h2c", required={"state": "str"}, optional={"seq": "int"}),
    "muted": FrameSpec("h2c", required={"muted": "bool"}, optional={"seq": "int"}),
    "agents": FrameSpec("h2c", required={"agents": "list"}, optional={"seq": "int"}),
    "confirm": FrameSpec(
        "h2c", required={"text": "str"},
        optional={"seq": "int", "confirm_id": "str", "deadline_wall": "num"},
    ),
    "ack": FrameSpec("h2c", required={"seq": "int"}),
    "error": FrameSpec("h2c", required={"code": "int", "reason": "str"}),
    "turn.begin": FrameSpec(
        "h2c", required={"turn_id": "str", "lane": "str"},
        optional={"seq": "int", "kind": "str", "text": "str"},
    ),
    "turn.sentence": FrameSpec(
        "h2c", required={"turn_id": "str", "kind": "str", "text": "str"},
        optional={"seq": "int", "n": "int"},
    ),
    "turn.end": FrameSpec(
        "h2c", required={"turn_id": "str", "status": "str"},
        optional={"seq": "int"},
    ),
    "confirm.request": FrameSpec(
        "h2c", required={"confirm_id": "str", "text": "str", "deadline_wall": "num"},
        optional={"seq": "int", "listen": "bool"},
    ),
    "confirm.cancel": FrameSpec("h2c", required={"confirm_id": "str"}),
    "tool.request": FrameSpec(
        "h2c", required={"rpc_id": "str", "tool": "str", "args": "dict"},
        optional={"timeout_s": "num"},
    ),
    "tool.cancel": FrameSpec("h2c", required={"rpc_id": "str"}),
    # The receipt for a published event — the publisher's cue to drop it
    # from its outbox; unacked ones ride again after a reconnect.
    "event.ack": FrameSpec("h2c", required={"publish_id": "str"}),
    "deliver.speak": FrameSpec(
        "h2c", required={"event_id": "str", "text": "str"},
        optional={"meta": "dict", "seq": "int"},
    ),
    "timers.sync": FrameSpec("h2c", required={"timers": "list"}, optional={"seq": "int"}),
    # The hub's world table, whole, whenever it changes — the Chart's
    # readings strip. Whole rather than a delta because it is small and a
    # resume must not depend on having seen the previous one.
    "world": FrameSpec("h2c", required={"facts": "dict"}, optional={"seq": "int"}),
}

BROADCAST_TYPES = frozenset({
    "row", "state", "muted", "agents", "confirm", "timers.sync", "world",
})
"""The frames that carry a seq and live in the replay ring: everything
the hub says to *every* client, all of it idempotent to re-see. Private
replies (ack, pong, error) and addressed requests (turn.*, confirm.*,
tool.*, deliver.*) are not replayable — a sentence replayed to a spoke
that already spoke it would be said twice, a tool request replayed to
one that already answered would run the tool twice."""

# WebSocket close codes in the application range. A client reads the
# code to decide what to do next: 4401 means "ask the user for the
# token", the others mean "try again later".
CLOSE_BAD_HELLO = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_HELLO_TIMEOUT = 4408
CLOSE_VERSION = 4409


# ── codec ────────────────────────────────────────────────────────────────────


def validate(frame: Any, direction: Direction) -> dict[str, Any]:
    """Check one decoded frame against the catalog for its direction and
    return it. Unknown *fields* pass (a newer peer may add them); unknown
    *types* and malformed required fields do not."""
    if not isinstance(frame, dict):
        raise WireError("frame is not an object")
    kind = frame.get("type")
    if not isinstance(kind, str) or not kind:
        raise WireError("frame has no type")
    spec = CATALOG.get(kind)
    if spec is None or not spec.allows(direction):
        raise WireError(f"unknown frame type {kind!r} for {direction}")
    for name, shape in spec.required.items():
        if name not in frame:
            raise WireError(f"{kind}: missing {name!r}")
        if not _SHAPES[shape](frame[name]):
            raise WireError(f"{kind}: {name!r} is not {shape}")
    for name, shape in spec.optional.items():
        if name in frame and frame[name] is not None and not _SHAPES[shape](frame[name]):
            raise WireError(f"{kind}: {name!r} is not {shape}")
    return frame


def decode(raw: str | bytes, direction: Direction) -> dict[str, Any]:
    """Parse and validate one inbound frame. ``direction`` is the way the
    frame travelled: a hub decodes with ``"c2h"``, a client with ``"h2c"``."""
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise WireError(f"frame is not JSON: {exc}") from None
    return validate(frame, direction)


def encode(frame: dict[str, Any], direction: Direction) -> str:
    """Validate and serialize one outbound frame — the sender's side of
    the same check, so a malformed frame fails at the source with a
    traceback instead of at the far end with a debug line."""
    return json.dumps(validate(frame, direction))


# ── the replay ring ──────────────────────────────────────────────────────────


def new_epoch() -> str:
    """A fresh ring identity — one per hub process. Random rather than a
    timestamp so two processes started in the same second (the
    autoreloader's re-exec) never share one."""
    return secrets.token_urlsafe(9)


@dataclass(frozen=True, slots=True)
class Resume:
    """What a client claims to have seen: its epoch and last seq."""

    epoch: str
    seq: int

    @classmethod
    def from_frame(cls, hello: dict[str, Any]) -> Resume | None:
        """The ``resume`` of a client hello, or None when absent or
        malformed — a bad claim is a fresh start, not a refusal."""
        raw = hello.get("resume")
        if not isinstance(raw, dict):
            return None
        epoch, seq = raw.get("epoch"), raw.get("seq")
        if not isinstance(epoch, str) or not _SHAPES["int"](seq) or seq < 0:
            return None
        return cls(epoch, seq)


class ReplayRing:
    """The hub's recent broadcast frames, seq-stamped, bounded.

    ``stamp`` numbers an outbound frame and remembers its encoded form;
    ``since`` answers a resume claim with the frames the client missed,
    or None when it cannot — a different epoch, or a seq the ring has
    already forgotten (the client fell behind more than ``maxlen``
    frames; it gets a fresh hello with the history window instead).

    ``grace_s`` is the Discord catch-up rule applied to live prompts: a
    ``confirm`` older than the grace is not replayed, because the
    broker has long since timed the question out and a stale banner
    would invite an answer to nothing. Every other frame replays at any
    age — a row is history whenever it arrives.
    """

    def __init__(self, maxlen: int = 1024, grace_s: float = 600.0) -> None:
        self.epoch = new_epoch()
        self._seq = 0
        self._grace_s = grace_s
        self._frames: deque[tuple[int, float, str, str]] = deque(
            maxlen=max(1, maxlen)
        )
        """(seq, wall time, type, encoded) — oldest first."""

    @property
    def seq(self) -> int:
        """The last seq handed out; 0 before any frame."""
        return self._seq

    def stamp(self, frame: dict[str, Any], now: float | None = None) -> str:
        """Number one broadcast frame in place, remember it, and return
        its encoded form for the sockets."""
        self._seq += 1
        frame["seq"] = self._seq
        data = json.dumps(frame)
        self._frames.append((self._seq, now or time.time(), frame["type"], data))
        return data

    def since(self, resume: Resume | None, now: float | None = None) -> list[str] | None:
        """The encoded frames after ``resume.seq``, or None for a fresh start."""
        if resume is None or resume.epoch != self.epoch or resume.seq > self._seq:
            return None
        if resume.seq == self._seq:
            return []
        if not self._frames or self._frames[0][0] > resume.seq + 1:
            # The first frame the client needs has already scrolled off.
            return None
        now = now or time.time()
        out: list[str] = []
        for seq, at, kind, data in self._frames:
            if seq <= resume.seq:
                continue
            if kind == "confirm" and now - at > self._grace_s:
                continue
            out.append(data)
        return out


__all__ = [
    "WIRE_VERSION",
    "BROADCAST_TYPES",
    "CATALOG",
    "CLOSE_BAD_HELLO",
    "CLOSE_HELLO_TIMEOUT",
    "CLOSE_UNAUTHORIZED",
    "CLOSE_VERSION",
    "Direction",
    "FrameSpec",
    "ReplayRing",
    "Resume",
    "WireError",
    "decode",
    "encode",
    "new_epoch",
    "validate",
]
