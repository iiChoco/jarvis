"""The room's socket protocol — small, and its own.

Ciel's wire (``ciel/wire.py``) carries the Chart and the spoke: rows,
turns, confirmations, tool calls. None of that is an interview. The room's
socket carries one live interview between one browser and one session,
and it needs two things the main wire refuses on principle — binary
frames (the recording comes up in chunks) and audio going down (the
interviewer's voice, one sentence per frame). So it is a separate
catalog with the same discipline: every text frame is one JSON object
with a ``type``, validated at both ends against the shapes below, and a
malformed frame costs a log line, never the socket.

Client → hub
    hello          v, caps{stt, tts, record}           first frame
    speech.partial text                                 the recogniser's interim text (replaces)
    speech.final   text, t                              a finalised segment (appends)
    answer.done                                         "I'm done answering"
    barge          t                                    the candidate spoke while the room was talking
    typed          text                                 the text fallback: a whole answer
    played         turn                                 the browser finished playing a turn's audio
    code.snapshot  lang, code                           the editor, while they work (technical)
    code.submit    lang, code                           the editor, when they say they're done
    end                                                 end the interview
    ping
    <binary>       4-byte big-endian seq + webm bytes   one recording chunk

Hub → client
    hello          v, state, tts, turn, elapsed_s, history
    state          state, hold_ms?                      listening | thinking | speaking | paused | ended
    say            n, turn, text, audio?                one sentence; audio is base64 WAV when tts=piper
    turn.end       turn
    exhibit        id, title, kind, columns, rows, note
    problem        id, title, statement, examples, constraints, starter, difficulty, topic
    apology        text
    transcript     t, speaker, text
    debrief.ready  session_id
    error          code, reason
    pong
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

WIRE_VERSION = 1

STATES = ("listening", "thinking", "speaking", "paused", "ended")


@dataclass(frozen=True)
class FrameSpec:
    direction: str  # "c2h" or "h2c"
    required: dict[str, str]
    optional: dict[str, str]


_S, _I, _F, _B, _L, _O, _A = "str", "int", "float", "bool", "list", "dict", "any"

CATALOG: dict[str, FrameSpec] = {
    # client → hub
    "hello": FrameSpec("c2h", {}, {"v": _I, "caps": _O}),
    "speech.partial": FrameSpec("c2h", {"text": _S}, {}),
    "speech.final": FrameSpec("c2h", {"text": _S}, {"t": _F}),
    "answer.done": FrameSpec("c2h", {}, {}),
    "barge": FrameSpec("c2h", {}, {"t": _F}),
    "typed": FrameSpec("c2h", {"text": _S}, {}),
    "played": FrameSpec("c2h", {"turn": _I}, {}),
    "code.snapshot": FrameSpec("c2h", {"lang": _S, "code": _S}, {}),
    "code.submit": FrameSpec("c2h", {"lang": _S, "code": _S}, {}),
    "end": FrameSpec("c2h", {}, {}),
    "ping": FrameSpec("c2h", {}, {}),
    # hub → client
    "hello.ok": FrameSpec("h2c", {"v": _I, "state": _S, "tts": _S, "turn": _I, "elapsed_s": _F, "history": _L}, {}),
    "state": FrameSpec("h2c", {"state": _S}, {"hold_ms": _I}),
    "say": FrameSpec("h2c", {"n": _I, "turn": _I, "text": _S}, {"audio": _S}),
    "turn.end": FrameSpec("h2c", {"turn": _I}, {}),
    "exhibit": FrameSpec("h2c", {"id": _S, "title": _S, "kind": _S}, {"columns": _L, "rows": _L, "note": _S}),
    "problem": FrameSpec("h2c", {"id": _S, "title": _S, "statement": _S}, {
        "examples": _L, "constraints": _L, "starter": _O, "difficulty": _S, "topic": _S,
    }),
    "apology": FrameSpec("h2c", {"text": _S}, {}),
    "transcript": FrameSpec("h2c", {"t": _F, "speaker": _S, "text": _S}, {}),
    "debrief.ready": FrameSpec("h2c", {"session_id": _S}, {}),
    "error": FrameSpec("h2c", {"code": _S, "reason": _S}, {}),
    "pong": FrameSpec("h2c", {}, {}),
}

_SHAPES = {
    _S: lambda v: isinstance(v, str),
    _I: lambda v: isinstance(v, int) and not isinstance(v, bool),
    _F: lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    _B: lambda v: isinstance(v, bool),
    _L: lambda v: isinstance(v, list),
    _O: lambda v: isinstance(v, dict),
    _A: lambda v: True,
}


class WireError(ValueError):
    pass


def validate(frame: Any, direction: str) -> dict[str, Any]:
    """The frame, when it is one of ours and shaped right; WireError otherwise."""
    if not isinstance(frame, dict):
        raise WireError("frame is not an object")
    kind = frame.get("type")
    if not isinstance(kind, str):
        raise WireError("frame has no type")
    # The hub's hello is spelled "hello" on the wire too; the catalog
    # keys it "hello.ok" only to hold a different shape.
    key = "hello.ok" if (direction == "h2c" and kind == "hello") else kind
    spec = CATALOG.get(key)
    if spec is None or spec.direction != direction:
        raise WireError(f"unknown {direction} frame {kind!r}")
    for name, shape in spec.required.items():
        if name not in frame:
            raise WireError(f"{kind}: missing {name}")
        if not _SHAPES[shape](frame[name]):
            raise WireError(f"{kind}: {name} should be {shape}")
    for name, shape in spec.optional.items():
        if name in frame and frame[name] is not None and not _SHAPES[shape](frame[name]):
            raise WireError(f"{kind}: {name} should be {shape}")
    return frame


def decode(raw: str, direction: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WireError(f"not JSON: {exc}") from exc
    return validate(data, direction)


def encode(frame: dict[str, Any], direction: str = "h2c") -> str:
    return json.dumps(validate(frame, direction), ensure_ascii=False)


def unpack_audio(data: bytes) -> tuple[int, bytes]:
    """(seq, chunk) from a binary frame; WireError when too short."""
    if len(data) < 4:
        raise WireError("audio frame too short")
    (seq,) = struct.unpack(">I", data[:4])
    return seq, data[4:]


__all__ = [
    "CATALOG",
    "FrameSpec",
    "STATES",
    "WIRE_VERSION",
    "WireError",
    "decode",
    "encode",
    "unpack_audio",
    "validate",
]
