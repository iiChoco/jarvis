"""Probe the wire (Meridian) — the catalog, the ring, the door, the socket.

    uv run scripts/probe_wire.py

Four layers, no network until the last: the codec round-trips every
frame in the catalog and refuses what it doesn't name; the replay ring
numbers frames and answers resume claims (matching, stale, foreign,
scrolled-off, too old to replay); ``admit`` — the whole auth policy as
one pure function — is walked through every peer-and-frame combination;
and the link's welcome is exercised with hand-planted queues (probe_web's
trick). The last section binds a real server on an ephemeral loopback
port and drives it with aiohttp's client: the token refusal, the hello
timeout, a resume that replays exactly the rows a dropped socket missed,
and the pre-handshake page whose first frame is a say. Skipped, with a
note, when aiohttp is not installed.

If everything here passes but the phone can't connect, look at the
bind address, the token file, and the Tailscale ACLs — in that order.
"""

import asyncio
import contextlib
import json
import socket
import sys
import time

from ciel import wire
from ciel.config import HubConfig, WebConfig
from ciel.remote.web import (
    Admission,
    WebLink,
    admit,
    loopback_peer,
    origin_allowed,
)

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


def refused(raw: str, direction: wire.Direction) -> bool:
    try:
        wire.decode(raw, direction)
    except wire.WireError:
        return True
    return False


# ── the codec ────────────────────────────────────────────────────────────────

SAMPLES: dict[str, dict] = {
    "hello": {"v": 1, "role": "chart", "token": "t", "client_id": "c",
              "caps": ["acks"], "resume": {"epoch": "e", "seq": 3}},
    "ping": {}, "pong": {"t_wall": 1.5},
    "say": {"text": "hi", "seq": 1}, "mute": {"muted": True}, "restart": {},
    "turn.cancel": {"turn_id": "t1", "reason": "barge-in"},
    "confirm.answer": {"confirm_id": "c1", "text": "yes"},
    "tool.result": {"rpc_id": "r1", "ok": True, "content": {"x": 1}},
    "event.publish": {"publish_id": "p1", "event": {"kind": "calendar"}},
    "presence": {"node": "mac", "attended": True, "locked": False, "idle_s": 2},
    "deliver.result": {"event_id": "e1", "ok": False, "error": "no tts"},
    "row": {"role": "ciel", "text": "hello", "t": 1.0},
    "state": {"state": "idle"}, "muted": {"muted": False},
    "agents": {"agents": []}, "confirm": {"text": "Run it?"},
    "ack": {"seq": 4}, "error": {"code": 4401, "reason": "bad token"},
    "turn.begin": {"turn_id": "t1", "lane": "web"},
    "turn.sentence": {"turn_id": "t1", "kind": "say", "text": "One."},
    "turn.end": {"turn_id": "t1", "status": "done"},
    "confirm.request": {"confirm_id": "c1", "text": "Run it?", "deadline_wall": 9.0},
    "tool.request": {"rpc_id": "r1", "tool": "shell", "args": {"cmd": "ls"}},
    "tool.cancel": {"rpc_id": "r1"},
    "deliver.speak": {"event_id": "e1", "text": "Meeting in ten."},
    "timers.sync": {"timers": []},
}


def probe_codec() -> None:
    print("\nthe codec")
    check(
        "every catalog type has a sample here",
        set(SAMPLES) == set(wire.CATALOG),
    )
    for kind, spec in wire.CATALOG.items():
        direction = "c2h" if spec.direction == "c2h" else "h2c"
        frame = {"type": kind, **SAMPLES[kind]}
        raw = wire.encode(frame, direction)
        back = wire.decode(raw, direction)
        if back != frame:
            check(f"{kind} round-trips", False)
    check("every catalog type round-trips in its direction", True)

    check("a client frame is refused hub-bound-only", refused(
        json.dumps({"type": "row", "role": "x", "text": "y", "t": 1}), "c2h"))
    check("a hub frame is refused client-bound-only", refused(
        json.dumps({"type": "say", "text": "x"}), "h2c"))
    check("hello goes both ways", not refused(
        json.dumps({"type": "hello"}), "c2h") and not refused(
        json.dumps({"type": "hello"}), "h2c"))
    check("an unknown type is refused", refused(json.dumps({"type": "warp"}), "c2h"))
    check("no type is refused", refused(json.dumps({"text": "x"}), "c2h"))
    check("a non-object is refused", refused("[1,2]", "c2h"))
    check("non-JSON is refused", refused("{nope", "c2h"))
    check("a missing required field is refused", refused(
        json.dumps({"type": "say"}), "c2h"))
    check("a required field of the wrong shape is refused", refused(
        json.dumps({"type": "say", "text": 5}), "c2h"))
    check("a bool where an int belongs is refused", refused(
        json.dumps({"type": "ack", "seq": True}), "h2c"))
    check("an optional field of the wrong shape is refused", refused(
        json.dumps({"type": "say", "text": "x", "seq": "7"}), "c2h"))
    check("an optional field set to null passes", not refused(
        json.dumps({"type": "say", "text": "x", "seq": None}), "c2h"))
    check("unknown extra fields pass (a newer peer)", not refused(
        json.dumps({"type": "say", "text": "x", "mood": "bright"}), "c2h"))
    try:
        wire.encode({"type": "say"}, "c2h")
        check("encode refuses at the source", False)
    except wire.WireError:
        check("encode refuses at the source", True)
    check(
        "the broadcast set is a subset of the hub-bound catalog",
        all(
            wire.CATALOG[k].direction == "h2c" for k in wire.BROADCAST_TYPES
        ),
    )
    check(
        "addressed requests are not broadcast",
        not ({"tool.request", "tool.cancel", "ack", "pong", "error"}
             & wire.BROADCAST_TYPES),
    )


# ── the ring ─────────────────────────────────────────────────────────────────


def probe_ring() -> None:
    print("\nthe replay ring")
    ring = wire.ReplayRing(maxlen=4, grace_s=100.0)
    check("a fresh ring is at seq 0 with an epoch", ring.seq == 0 and len(ring.epoch) >= 8)
    check(
        "two rings never share an epoch",
        wire.ReplayRing().epoch != wire.ReplayRing().epoch,
    )
    f1 = {"type": "row", "role": "ciel", "text": "one", "t": 1.0}
    d1 = ring.stamp(f1, now=1000.0)
    check(
        "stamp numbers the frame in place and returns its encoding",
        f1["seq"] == 1 and json.loads(d1)["seq"] == 1 and ring.seq == 1,
    )
    check("no claim is a fresh start", ring.since(None) is None)
    check(
        "a claim at the current seq replays nothing",
        ring.since(wire.Resume(ring.epoch, 1)) == [],
    )
    d2 = ring.stamp({"type": "state", "state": "thinking"}, now=1001.0)
    d3 = ring.stamp({"type": "confirm", "text": "Run it?"}, now=1002.0)
    check(
        "a claim one behind replays exactly what followed",
        ring.since(wire.Resume(ring.epoch, 1), now=1003.0) == [d2, d3],
    )
    check(
        "a foreign epoch is a fresh start",
        ring.since(wire.Resume("elsewhere", 1)) is None,
    )
    check(
        "a claim ahead of the ring is a fresh start",
        ring.since(wire.Resume(ring.epoch, 9)) is None,
    )
    check(
        "a confirm past the grace is not replayed; the rest is",
        ring.since(wire.Resume(ring.epoch, 1), now=1002.0 + 101.0) == [d2],
    )
    d4 = ring.stamp({"type": "row", "role": "ciel", "text": "four", "t": 4.0}, now=1004.0)
    d5 = ring.stamp({"type": "row", "role": "ciel", "text": "five", "t": 5.0}, now=1005.0)
    check(
        "a claim whose next frame scrolled off is a fresh start",
        ring.since(wire.Resume(ring.epoch, 0)) is None
        and ring.since(wire.Resume(ring.epoch, 1), now=1006.0) == [d2, d3, d4, d5],
    )
    check(
        "Resume.from_frame reads a well-formed claim",
        wire.Resume.from_frame({"resume": {"epoch": "e", "seq": 2}})
        == wire.Resume("e", 2),
    )
    check(
        "and shrugs at a malformed one",
        wire.Resume.from_frame({"resume": {"epoch": "e", "seq": True}}) is None
        and wire.Resume.from_frame({"resume": {"epoch": "e", "seq": -1}}) is None
        and wire.Resume.from_frame({"resume": "e:2"}) is None
        and wire.Resume.from_frame({}) is None,
    )


# ── the door ─────────────────────────────────────────────────────────────────


def probe_admit() -> None:
    print("\nthe door (admit)")
    hello = {"type": "hello", "role": "spoke", "token": "s3cret",
             "client_id": "mac", "resume": {"epoch": "e", "seq": 5}}
    say = {"type": "say", "text": "hi"}

    v = admit(None, loopback=True, token="", require_token=False)
    check("silence is a timeout close", not v.ok and v.code == wire.CLOSE_HELLO_TIMEOUT)

    v = admit(say, loopback=True, token="", require_token=False)
    check(
        "a loopback peer's first say is an implicit hello",
        v.ok and v.implicit and v.role == "chart" and v.resume is None,
    )
    v = admit(say, loopback=False, token="s3cret", require_token=False)
    check("a remote peer must hello first", not v.ok and v.code == wire.CLOSE_BAD_HELLO)

    v = admit({"type": "hello", "role": "chart"}, loopback=False,
              token="s3cret", require_token=False)
    check("a remote hello without a token is refused",
          not v.ok and v.code == wire.CLOSE_UNAUTHORIZED)
    v = admit({**hello, "token": "s3cret "}, loopback=False,
              token="s3cret", require_token=False)
    check("a wrong token is refused", not v.ok and v.code == wire.CLOSE_UNAUTHORIZED)
    v = admit({**hello, "token": 123}, loopback=False, token="s3cret", require_token=False)
    check("a non-string token is refused", not v.ok and v.code == wire.CLOSE_UNAUTHORIZED)
    v = admit(hello, loopback=False, token="", require_token=False)
    check("an unset server token admits nobody off loopback",
          not v.ok and v.code == wire.CLOSE_UNAUTHORIZED)

    v = admit(hello, loopback=False, token="s3cret", require_token=False)
    check(
        "the right token admits, with role, id, and the resume claim",
        v.ok and not v.implicit and v.role == "spoke" and v.client_id == "mac"
        and v.resume == wire.Resume("e", 5),
    )
    v = admit({"type": "hello"}, loopback=True, token="s3cret", require_token=False)
    check("a loopback hello needs no token", v.ok and v.role == "chart")
    v = admit({"type": "hello"}, loopback=True, token="s3cret", require_token=True)
    check("unless the hub requires one", not v.ok and v.code == wire.CLOSE_UNAUTHORIZED)
    v = admit(say, loopback=True, token="s3cret", require_token=True)
    check("and then a first say is no longer an implicit hello",
          not v.ok and v.code == wire.CLOSE_BAD_HELLO)
    v = admit({"type": "hello", "role": "", "client_id": 7}, loopback=True,
              token="", require_token=False)
    check("blank or odd identity fields fall back quietly",
          v.ok and v.role == "chart" and v.client_id == "")

    print("\nthe peer and the Origin, off loopback")
    check("127.0.0.1 is loopback", loopback_peer("127.0.0.1"))
    check("::1 is loopback", loopback_peer("::1"))
    check("a tailnet address is not", not loopback_peer("100.101.102.103"))
    check("an unknown peer is not", not loopback_peer(None) and not loopback_peer("x"))
    check(
        "the bind address is an allowed Origin",
        origin_allowed("http://100.101.102.103:8765", 8765, ("100.101.102.103",)),
    )
    check(
        "a configured MagicDNS name is an allowed Origin",
        origin_allowed("http://ciel-hub.tail1.ts.net:8765", 8765,
                       ("100.101.102.103", "ciel-hub.tail1.ts.net")),
    )
    check(
        "an unlisted name is refused even on our port (DNS rebinding)",
        not origin_allowed("http://attacker.example:8765", 8765, ("100.101.102.103",)),
    )
    check(
        "loopback still passes with hosts set",
        origin_allowed("http://127.0.0.1:8765", 8765, ("100.101.102.103",)),
    )


# ── the welcome, with planted queues ─────────────────────────────────────────


def probe_welcome() -> None:
    print("\nthe welcome")
    link = WebLink(WebConfig(), HubConfig(resume_frames=8))
    link.note_row("user-web", "one")
    link.note_row("ciel", "two")
    link.note_state("thinking")
    check("broadcast frames are numbered even into an empty room", link._ring.seq == 3)
    check("history rows carry their seq", [r["seq"] for r in link._history] == [1, 2])

    queue, resumed = link._welcome("fresh", Admission(True))
    hello = json.loads(queue.get_nowait())
    check(
        "a fresh client gets the history window and the ring position",
        not resumed and hello["resumed"] is False and hello["seq"] == 3
        and hello["epoch"] == link.epoch and hello["v"] == wire.WIRE_VERSION
        and [r["text"] for r in hello["history"]] == ["one", "two"]
        and hello["state"] == "thinking" and queue.qsize() == 0,
    )

    queue, resumed = link._welcome(
        "back", Admission(True, resume=wire.Resume(link.epoch, 1))
    )
    hello = json.loads(queue.get_nowait())
    replay = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    check(
        "a resuming client gets no history and exactly the frames it missed",
        resumed and hello["resumed"] is True and hello["history"] == []
        and [(f["type"], f["seq"]) for f in replay] == [("row", 2), ("state", 3)],
    )

    queue, resumed = link._welcome(
        "stale", Admission(True, resume=wire.Resume("another-process", 1))
    )
    check(
        "a claim from another epoch is a fresh hello",
        not resumed and json.loads(queue.get_nowait())["resumed"] is False,
    )

    link.note_row("ciel", "three")
    check(
        "a live row reaches every registered client, numbered",
        all(
            json.loads(link._clients[k].get_nowait())["seq"] == 4
            for k in ("fresh", "back", "stale")
        ),
    )

    for _ in range(10):
        link.note_row("ciel", "spam")
    queue, resumed = link._welcome(
        "behind", Admission(True, resume=wire.Resume(link.epoch, 3))
    )
    check(
        "a client that fell off the ring gets a fresh hello",
        not resumed and json.loads(queue.get_nowait())["resumed"] is False,
    )

    wide = WebLink(WebConfig(history_lines=5), HubConfig(resume_frames=4096))
    for i in range(700):
        wide.note_row("ciel", str(i))
    queue, resumed = wide._welcome(
        "flood", Admission(True, resume=wire.Resume(wide.epoch, 0))
    )
    hello = json.loads(queue.get_nowait())
    check(
        "a replay too long for the outbound queue is a fresh hello instead",
        not resumed and hello["resumed"] is False and len(hello["history"]) == 5,
    )


# ── the socket ───────────────────────────────────────────────────────────────


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def probe_socket() -> None:
    print("\nthe socket (real aiohttp, loopback)")
    try:
        import aiohttp
    except ImportError:
        print("  skip aiohttp is not installed (uv sync --extra web)")
        return

    port = free_port()
    web_cfg = WebConfig(enabled=True, port=port)
    url = f"ws://127.0.0.1:{port}/ws"

    async def recv(ws: aiohttp.ClientWebSocketResponse, timeout: float = 2.0) -> dict:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
        if msg.type != aiohttp.WSMsgType.TEXT:
            return {"type": "<closed>", "code": ws.close_code}
        return json.loads(msg.data)

    # A hub that asks even loopback for the token — the only way to
    # exercise the refusal path without a second machine.
    link = WebLink(web_cfg, HubConfig(token="s3cret", require_token=True))
    await link.start()
    check("the server bound", link.serving and link.url.endswith(str(port)))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                await ws.send_str(json.dumps({"type": "hello", "role": "chart"}))
                err = await recv(ws)
                closed = await recv(ws)
                check(
                    "a hello without the token is told why, then closed 4401",
                    err == {"type": "error", "code": 4401, "reason": "bad token"}
                    and closed["type"] == "<closed>" and ws.close_code == 4401,
                )
            async with session.ws_connect(url) as ws:
                await ws.send_str(json.dumps({"type": "say", "text": "sneak"}))
                await recv(ws)
                closed = await recv(ws)
                check(
                    "a first say off the trusted path is closed 4400, unqueued",
                    ws.close_code == 4400 and not link.pending,
                )

            hello_ok = {"type": "hello", "v": 1, "role": "chart",
                        "token": "s3cret", "client_id": "probe"}
            async with session.ws_connect(url) as ws:
                await ws.send_str(json.dumps(hello_ok))
                hello = await recv(ws)
                check(
                    "the right token gets the hello",
                    hello["type"] == "hello" and hello["resumed"] is False
                    and hello["epoch"] == link.epoch,
                )
                epoch, seq = hello["epoch"], hello["seq"]
                link.note_row("ciel", "one")
                row = await recv(ws)
                check("a live row arrives numbered", row["text"] == "one" and row["seq"] == seq + 1)
                seq = row["seq"]
                await ws.send_str(json.dumps({"type": "say", "text": "typed", "seq": 1}))
                ack = await recv(ws)
                check(
                    "a say is acked and queued",
                    ack == {"type": "ack", "seq": 1} and link.pop_batch() == ("typed", None),
                )
            # The socket is gone; the room moves on without it.
            link.note_row("ciel", "two")
            link.note_state("speaking")
            async with session.ws_connect(url) as ws:
                await ws.send_str(json.dumps({
                    **hello_ok, "resume": {"epoch": epoch, "seq": seq},
                }))
                hello = await recv(ws)
                f1 = await recv(ws)
                f2 = await recv(ws)
                check(
                    "a resume after a drop replays exactly the missed frames",
                    hello["resumed"] is True and hello["history"] == []
                    and (f1["type"], f1["text"]) == ("row", "two")
                    and (f2["type"], f2["state"]) == ("state", "speaking")
                    and f2["seq"] == hello["seq"],
                )
    finally:
        await link.close()

    # The default door: loopback trusted by reach, a short hello timeout.
    link = WebLink(web_cfg, HubConfig(hello_timeout_s=0.3))
    await link.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                await ws.send_str(json.dumps({"type": "say", "text": "old page"}))
                hello = await recv(ws)
                check(
                    "a loopback page whose first frame is a say is welcomed, "
                    "and the say is kept",
                    hello["type"] == "hello" and link.pop_batch() == ("old page", None),
                )
            async with session.ws_connect(url) as ws:
                started = time.monotonic()
                err = await recv(ws, timeout=3.0)
                closed = await recv(ws, timeout=3.0)
                check(
                    "a silent socket is told why, then closed 4408, "
                    "after the hello timeout",
                    err == {"type": "error", "code": 4408, "reason": "no hello"}
                    and closed["type"] == "<closed>" and ws.close_code == 4408
                    and 0.2 < time.monotonic() - started < 2.0,
                )
            try:
                async with session.ws_connect(url, origin="https://evil.example"):
                    check("a foreign Origin is refused at the upgrade", False)
            except aiohttp.WSServerHandshakeError as exc:
                check("a foreign Origin is refused at the upgrade", exc.status == 403)
            async with session.ws_connect(url, origin=f"http://127.0.0.1:{port}") as ws:
                await ws.send_str(json.dumps({"type": "hello"}))
                check("our own page's Origin passes", (await recv(ws))["type"] == "hello")
    finally:
        await link.close()


# ── entry ────────────────────────────────────────────────────────────────────


def main() -> None:
    probe_codec()
    probe_ring()
    probe_admit()
    probe_welcome()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(probe_socket())
    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    main()
