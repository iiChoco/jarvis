"""Probe the web GUI (Chart) — scripted by default, live on request.

    uv run scripts/probe_web.py            # scripted checks, no server
    uv run scripts/probe_web.py --live     # serve the real page, echo turns

The scripted checks drive the link's queue mechanics, the inbound frame
handling, and the Origin gate with no network at all, so a regression
shows up here before it shows up as a silent page. The live mode starts
the actual server with an echo responder in place of the brain: open the
printed URL, type, watch it come back — the GUI, the WebSocket protocol,
the mute switch, and the confirm banner all exercised without a
microphone or a model. Type "confirm test" in the page to see the
confirmation banner; type "deep" to toggle a deep-thought entry in the
agents chip; type "world" to toggle the Mac away and a section open in
the readings strip; flip the mute switch or arm the restart chip and the probe
reports it (the probe only reports a restart — nothing relaunches here).

If the page works here but not under Ciel, the lane is fine and the
pipeline wiring is the place to look; if nothing works here, it is the
port, the dependency, or the page itself.
"""

import argparse
import asyncio
import contextlib
import json
import sys
import time
from dataclasses import replace

from ciel.config import HubConfig, WebConfig, load_config
from ciel.remote.web import WebIndicator, WebLink, origin_allowed

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


# ── scripted ─────────────────────────────────────────────────────────────────


def probe_origin_gate() -> None:
    print("\nthe Origin gate")
    check("no Origin (native client) passes", origin_allowed(None, 8765))
    check("empty Origin passes", origin_allowed("", 8765))
    check(
        "our own page passes",
        origin_allowed("http://127.0.0.1:8765", 8765),
    )
    check(
        "localhost spelling passes",
        origin_allowed("http://localhost:8765", 8765),
    )
    check(
        "another local port is refused",
        not origin_allowed("http://127.0.0.1:9999", 8765),
    )
    check(
        "a foreign site is refused",
        not origin_allowed("https://evil.example", 8765),
    )
    check(
        "a foreign site claiming our port is refused",
        not origin_allowed("https://evil.example:8765", 8765),
    )
    check(
        "a non-http scheme is refused",
        not origin_allowed("file://x", 8765),
    )


def probe_queue() -> None:
    print("\nthe turn queue")
    link = WebLink(WebConfig())
    from ciel.remote.lane import Lane

    check("WebLink conforms to the Lane protocol", isinstance(link, Lane))
    check("starts empty", not link.pending and link.pop_batch() is None)

    link._on_frame(json.dumps({"type": "say", "text": "  hello  "}))
    check("a say frame queues, stripped", link.pending and link.peek()[1] == "hello")

    link._on_frame(json.dumps({"type": "say", "text": "quick question"}))
    link._on_frame(json.dumps({"type": "say", "text": "the question"}))
    batch = link.pop_batch()
    check(
        "bursts coalesce into one turn",
        batch == ("hello\nquick question\nthe question", None) and not link.pending,
    )

    link._on_frame(json.dumps({"type": "say", "text": ""}))
    link._on_frame(json.dumps({"type": "say", "text": "   "}))
    check("blank says are dropped", not link.pending)

    link._on_frame("not json at all")
    link._on_frame(json.dumps({"type": "mystery"}))
    link._on_frame(json.dumps(["wrong", "shape"]))
    check("malformed frames cost nothing", not link.pending)

    long = WebLink(replace(WebConfig(), max_inbound_chars=10))
    long._on_frame(json.dumps({"type": "say", "text": "x" * 100}))
    check("oversized says are truncated", long.peek()[1] == "x" * 10)

    stamped = WebLink(WebConfig())
    before = time.monotonic()
    stamped._on_frame(json.dumps({"type": "say", "text": "when"}))
    check(
        "arrival is monotonic-stamped (the confirm predating rule)",
        before <= stamped.peek()[0] <= time.monotonic(),
    )


def probe_delivery_receipts() -> None:
    print("\nthe delivery receipts")
    link = WebLink(WebConfig())
    # Hand-planted client queues stand in for sockets (probe_agents'
    # trick): what lands in them is what each page would receive.
    asker: asyncio.Queue = asyncio.Queue()
    bystander: asyncio.Queue = asyncio.Queue()
    link._clients["asker"] = asker
    link._clients["bystander"] = bystander

    link._on_frame(json.dumps({"type": "ping"}), "asker")
    check(
        "a ping pongs to the asker alone, and queues no turn",
        json.loads(asker.get_nowait()) == {"type": "pong"}
        and bystander.qsize() == 0
        and not link.pending,
    )

    link._on_frame(json.dumps({"type": "say", "text": "hi", "seq": 7}), "asker")
    check("a seq'd say queues its turn", link.pending and link.peek()[1] == "hi")
    check(
        "and acks the seq to its sender alone",
        json.loads(asker.get_nowait()) == {"type": "ack", "seq": 7}
        and bystander.qsize() == 0,
    )

    link._on_frame(json.dumps({"type": "say", "text": "", "seq": 8}), "asker")
    check(
        "a blank say is dropped but still acked — never resent forever",
        json.loads(asker.get_nowait()) == {"type": "ack", "seq": 8}
        and link.pop_batch() == ("hi", None),
    )

    link._on_frame(json.dumps({"type": "say", "text": "plain"}), "asker")
    check(
        "a say without a seq queues, unacked",
        link.peek()[1] == "plain" and asker.qsize() == 0,
    )

    link._on_frame(json.dumps({"type": "say", "text": "gone", "seq": 9}))
    link._on_frame(json.dumps({"type": "ping"}), "never-registered")
    check(
        "a sender already dropped (or never known) still gets its turn "
        "queued, just no receipt",
        link.pop_batch() == ("plain\ngone", None)
        and asker.qsize() == 0
        and bystander.qsize() == 0,
    )


def probe_mute_relay() -> None:
    print("\nthe mute relay")
    link = WebLink(WebConfig())
    flips: list[bool] = []
    link.on_mute = flips.append
    link._on_frame(json.dumps({"type": "mute", "muted": True}))
    link._on_frame(json.dumps({"type": "mute", "muted": False}))
    check("mute frames reach the pipeline hook", flips == [True, False])

    orphan = WebLink(WebConfig())
    orphan._on_frame(json.dumps({"type": "mute", "muted": True}))
    check("a mute frame with no hook is survived", True)

    link.note_muted(True)
    check("note_muted with no clients is survived", True)


def probe_restart_relay() -> None:
    print("\nthe restart relay")
    link = WebLink(WebConfig())
    calls: list[bool] = []
    link.on_restart = lambda: calls.append(True)
    link._on_frame(json.dumps({"type": "restart"}))
    check("restart frames reach the pipeline hook", calls == [True])
    check("a restart frame queues no turn", not link.pending)

    orphan = WebLink(WebConfig())
    orphan._on_frame(json.dumps({"type": "restart"}))
    check(
        "a restart frame with no hook is survived, and queues nothing",
        calls == [True] and not orphan.pending,
    )


def probe_view() -> None:
    print("\nthe view taps")
    link = WebLink(replace(WebConfig(), history_lines=3))
    for i in range(5):
        link.note_row("ciel", f"sentence {i}")
    check(
        "history keeps the newest history_lines rows",
        [r["text"] for r in link._history] == ["sentence 2", "sentence 3", "sentence 4"],
    )
    check(
        "rows carry role and wall time",
        link._history[-1]["role"] == "ciel" and link._history[-1]["t"] <= time.time(),
    )

    indicator = WebIndicator(link)
    indicator.set_state("thinking")
    check("the indicator adapter feeds the link", link._state == "thinking")


def probe_agents() -> None:
    print("\nthe agent roster")
    link = WebLink(WebConfig())
    # A hand-planted client queue stands in for a socket: _broadcast only
    # ever put_nowaits, so what lands here is what a page would receive.
    queue: asyncio.Queue = asyncio.Queue()
    link._clients["fake"] = queue

    roster = [
        {
            "id": "w1", "kind": "watch",
            "label": "The export has finished.",
            "detail": "file /tmp/out.mp4", "since": 1.0, "until": 2.0,
        }
    ]
    link.note_agents(roster)
    check(
        "a roster change broadcasts and is stored for the hello",
        link._agents == roster and queue.qsize() == 1,
    )
    check(
        "the frame carries type, roster, and its seq",
        json.loads(queue.get_nowait())
        == {"type": "agents", "agents": roster, "seq": 1},
    )

    link.note_agents(list(roster))
    check("an unchanged roster costs the sockets nothing", queue.qsize() == 0)

    link.note_agents([])
    check(
        "an emptied roster broadcasts",
        link._agents == [] and queue.qsize() == 1,
    )


# ── live ─────────────────────────────────────────────────────────────────────


async def live(port: int | None = None, require_token: str | None = None) -> None:
    cfg = load_config()
    web_cfg = replace(cfg.web, enabled=True)
    if port is not None:
        # A running Ciel usually owns the configured port; the probe can
        # serve beside it rather than demand it.
        web_cfg = replace(web_cfg, port=port)
    hub_cfg = replace(cfg.hub, bind="")  # loopback here, whatever the config binds
    if require_token is not None:
        # The door the phone sees, on loopback: the page must show the
        # token form on 4401 and connect once the token is pasted.
        hub_cfg = replace(hub_cfg, token=require_token, require_token=True)
    link = WebLink(web_cfg, hub_cfg)
    muted = False

    def on_mute(value: bool) -> None:
        nonlocal muted
        muted = value
        print(f"  [mute -> {value}]")
        link.note_muted(value)
        link.note_row("event", "muted" if value else "unmuted")

    link.on_mute = on_mute

    def on_restart() -> None:
        print("  [restart requested]")
        link.note_row("event", "restart requested — Ciel restarts when idle")

    link.on_restart = on_restart

    await link.start()
    if not link.serving:
        print("could not serve — see the warning above")
        return
    print(f"open {link.url} — echoes until Ctrl-C")
    if require_token is not None:
        print(f"  the page will ask for the token: {require_token}")

    # A sample roster so the agents chip has something to count; typing
    # "deep" toggles a deep-thought entry on top of it, the way a real
    # escalation would appear and clear.
    sample = [
        {
            "id": "w1", "kind": "watch",
            "label": "The video export has finished.",
            "detail": "file ~/Movies/export.mp4",
            "since": time.time() - 300, "until": time.time() + 1500,
        },
        {
            "id": "t1", "kind": "timer", "label": "10 minute timer",
            "detail": None, "since": None, "until": time.time() + 480,
        },
    ]
    deep_on = False
    link.note_agents(list(sample))

    # A sample world table (world.py) so the readings strip has chips to
    # draw; typing "world" toggles the Mac away and a section open, the
    # way a spoke drop and a spot opening would move the strip.
    now = time.time()
    world_on = True

    def world_facts() -> dict:
        return {
            "spoke": {"value": {"connected": world_on, "node": "mac"},
                      "observed_at": now, "source": "hub"},
            "presence": {"value": {"present": True, "locked": False, "idle_s": 4.0,
                                   "since_conversation_s": 30.0},
                         "observed_at": time.time(), "source": "spoke:mac", "ttl_s": 30.0},
            "place": {"value": {"place": "home", "via": "wifi", "network": "Nest",
                                "device": None, "at": now - 1500},
                      "observed_at": now, "source": "mac"},
            "timers": {"value": [{"kind": "timer", "label": "", "due_at": now + 480,
                                  "duration_s": 600, "pending": False}],
                       "observed_at": now, "source": "hub"},
            "agenda": {"value": {"day": time.strftime("%Y-%m-%d"),
                                 "lines": ["5:00 PM — Standup", "7:30 PM — Dinner"]},
                       "observed_at": now - 600, "source": "calendar"},
            "oura": {"value": {"day": time.strftime("%Y-%m-%d"), "readiness": 71,
                               "sleep_score": 80, "sleep_s": 24000},
                     "observed_at": now - 7200, "source": "oura"},
            "sections": {"value": {"spots": {"12": 0, "31": 2 if not world_on else 0}},
                         "observed_at": now, "source": "sections@mac", "ttl_s": 120.0},
        }

    link.note_world(world_facts())

    try:
        while True:
            await asyncio.sleep(0.2)
            batch = link.pop_batch()
            if batch is None:
                continue
            text, _channel = batch
            print(f"  you: {text!r}")
            link.note_row("user-web", text)
            link.note_state("thinking")
            await asyncio.sleep(0.6)
            if text.lower().startswith("deep"):
                deep_on = not deep_on
                deep = [{
                    "id": "deep-thought", "kind": "deep",
                    "label": "Deep thought",
                    "detail": "reasoning through the current question",
                    "since": time.time(), "until": None,
                }] if deep_on else []
                link.note_agents(deep + sample)
                link.note_row(
                    "event",
                    "deep thought engaged" if deep_on else "deep thought done",
                )
                link.note_state("idle")
            elif text.lower().startswith("world"):
                world_on = not world_on
                link.note_world(world_facts())
                link.note_row("event", "spoke connected" if world_on else "spoke disconnected")
                link.note_state("idle")
            elif text.lower().startswith("confirm"):
                # Exercise the banner: the question frame plus its row,
                # the same pair the broker produces. The state stays
                # "thinking" — a real ask holds its turn open, and the
                # page clears the banner when the turn ends.
                link.note_row("ciel-confirm", "Run: echo test — okay?")
                await link.send("Run: echo test — okay?")
            else:
                link.note_row("ciel", f"(echo{' — muted' if muted else ''}) {text}")
                link.note_state("idle")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await link.close()


# ── entry ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="serve the page and echo")
    parser.add_argument(
        "--port", type=int, default=None,
        help="serve on this port (the configured one may belong to a running Ciel)",
    )
    parser.add_argument(
        "--require-token", metavar="TOKEN", default=None,
        help="with --live: ask even loopback pages for this hub token",
    )
    args = parser.parse_args()

    if args.live:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(live(args.port, args.require_token))
        return

    probe_origin_gate()
    probe_queue()
    probe_delivery_receipts()
    probe_mute_relay()
    probe_restart_relay()
    probe_view()
    probe_agents()
    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    main()
