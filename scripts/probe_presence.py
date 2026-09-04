"""Probe presence and the publishers — the Mac's senses reaching the hub.

    uv run scripts/probe_presence.py

Three pieces. ``PresenceView`` is the hub's presence: the spoke's raw
signals folded with the hub's own conversation recency into the same
frozen state the policy has always read — driven here as a table, with
the one rule the split adds pinned: a stale heartbeat is an empty room,
whatever else is true. ``PublishQueue`` is the watchers' queue on the
spoke, with the hub on the other side: the outbox, the ack, the resend
after a reconnect, the local dedupe that keeps a rescan off the wire.
``Heartbeat`` builds the presence frame from the probe's readers and
sends on change. Last, the hub's side of both, through the real
server: a published event lands in the real ``EventQueue`` and is
acked, a resend is absorbed, and a heartbeat updates the view and the
watch roster.
"""

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ciel.config import Config, HubConfig, ProactiveConfig, WebConfig
from ciel.hub.rpc import PresenceView, RemoteBindings
from ciel.hub.server import HubServer
from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.presence import PresenceProbe
from ciel.remote.web import Admission
from ciel.spoke.publisher import Heartbeat, PublishQueue

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


def beat(locked=False, idle=5.0, attended=True):
    return {"type": "presence", "node": "mac", "attended": attended, "locked": locked, "idle_s": idle}


def probe_view() -> None:
    print("the hub's presence view")
    cfg = replace(ProactiveConfig(), presence_idle_s=300.0, conversation_presence_s=180.0)
    v = PresenceView(cfg, stale_s=30.0)
    s = v.state(100.0)
    check("no heartbeat yet: locked, idle forever, not present",
          s.screen_locked and s.seconds_since_input == float("inf") and not s.present)
    v.observe(beat(), 100.0)
    s = v.state(101.0)
    check("a fresh attended beat: present at the keyboard",
          not s.screen_locked and s.seconds_since_input == 5.0 and s.present)
    v.observe(beat(locked=True, idle=2.0), 102.0)
    check("locked: not present", not v.state(103.0).present)
    v.observe(beat(idle=900.0, attended=False), 104.0)
    check("idle past the threshold: not present", not v.state(105.0).present)
    v.note_conversation(104.0)
    check("but a recent conversation counts (mode any)", v.state(105.0).present)
    check("the hid mode ignores the conversation",
          not PresenceView(replace(cfg, presence_mode="hid"), 30.0).state(0.0).present)
    v.observe(beat(idle=900.0, attended=False), 104.0)
    v.note_conversation(104.0)
    s = v.state(140.0)
    check("a stale heartbeat is an empty room even mid-conversation",
          s.screen_locked and not s.present and s.seconds_since_conversation == 36.0)
    v2 = PresenceView(replace(cfg, presence_mode="conversation"), 30.0)
    v2.observe(beat(locked=True), 0.0)
    v2.note_conversation(1.0)
    check("conversation mode: present on the conversation while the beat is fresh",
          v2.state(2.0).present)


def probe_publish_queue() -> None:
    print("\nthe publish queue")
    sent: list[dict] = []
    connected = [True]
    pq = PublishQueue(lambda f: (sent.append(f) if connected[0] else None) or connected[0], node="mac")
    ids = [pq.next_id() for _ in range(2)]
    check("ids are node-prefixed and unique", ids == ["mac-1", "mac-2"])
    ev = ProactiveEvent(id="mac-1", source="calendar", importance=2, created_at=10.0,
                        expires_at=20.0, summary="Standup in ten minutes.", dedupe_key="cal:1")
    check("a push publishes at once", pq.push(ev) and sent[-1]["type"] == "event.publish"
          and sent[-1]["event"]["summary"] == "Standup in ten minutes."
          and sent[-1]["event"]["dedupe_key"] == "cal:1")
    pid = sent[-1]["publish_id"]
    check("and waits in the outbox", pq.pending and pq.outstanding == 1)
    check("the same key again is a rescan: not re-published",
          not pq.push(replace(ev, id="mac-9")) and len(sent) == 1)
    pq.acked(pid)
    check("the ack clears it", not pq.pending)
    connected[0] = False
    ev2 = replace(ev, id="mac-3", dedupe_key="cal:2")
    check("with the hub down a push is held, not lost", pq.push(ev2) and pq.outstanding == 1)
    connected[0] = True
    check("a reconnect resends the outbox", pq.resend() == 1 and sent[-1]["event"]["dedupe_key"] == "cal:2")
    pq.acked(sent[-1]["publish_id"])
    check("unknown acks are harmless", (pq.acked("nope") or True) and not pq.pending)


class FakeWatch:
    def __init__(self, id):
        self.id, self.kind, self.target, self.label = id, "file", "/tmp/x", "Done."
        self.created_at, self.expires_at = 1.0, 2.0


class FakeWatcher:
    def __init__(self):
        self.watches = []

    def active(self):
        return list(self.watches)


def probe_heartbeat() -> None:
    print("\nthe heartbeat")
    cfg = replace(ProactiveConfig(), presence_idle_s=300.0)
    signals = {"locked": False, "idle": 3.0}
    probe = PresenceProbe(cfg, lock_reader=lambda: signals["locked"], idle_reader=lambda: signals["idle"])
    watcher = FakeWatcher()
    sent: list[dict] = []
    hb = Heartbeat(lambda f: sent.append(f) or True, probe, watcher, node="mac",
                   interval_s=10.0, idle_threshold_s=300.0)
    f = hb.frame()
    check("the frame carries the raw signals and the roster",
          f["node"] == "mac" and f["attended"] and not f["locked"] and f["idle_s"] == 3.0
          and f["watches"] == [] and f["active_watches"] == 0)
    check("the first beat sends", hb.beat() and len(sent) == 1)
    signals["idle"] = 4.0
    check("idle alone changing does not", not hb.beat() and len(sent) == 1)
    signals["locked"] = True
    check("a lock sends at once", hb.beat() and sent[-1]["locked"] and not sent[-1]["attended"])
    watcher.watches.append(FakeWatch("w1"))
    check("a new watch sends the roster", hb.beat() and sent[-1]["watches"][0]["id"] == "w1")
    check("force sends regardless", hb.beat(force=True) and len(sent) == 4)
    signals["idle"] = float("inf")
    check("an unreadable idle is left out rather than sent as infinity",
          "idle_s" not in hb.frame())


def probe_hub_side() -> None:
    print("\nthe hub's side, through the real server")
    tmp = Path(tempfile.mkdtemp())
    cfg = replace(Config(), state_dir=tmp)
    server = HubServer(WebConfig(), HubConfig())
    remote = RemoteBindings(server, cfg)
    queue = EventQueue(tmp / "proactive.json", 3600.0)
    landed: list[str] = []

    def on_event(raw):
        ev = ProactiveEvent(**{k: raw[k] for k in ("id", "source", "importance", "created_at",
                                                    "expires_at", "summary", "dedupe_key", "payload",
                                                    "deliver_after")})
        new = queue.push(ev)
        if new:
            landed.append(ev.summary)
        return new

    server.on_event = on_event
    server.on_presence = lambda frame: (remote.presence.observe(frame, 100.0),
                                        remote.watcher.observe(frame.get("watches") or []))
    q, _ = server._welcome("spoke", Admission(True, role="spoke", client_id="mac"))
    q.get_nowait()

    pq = PublishQueue(lambda f: server._on_frame(json.dumps(f), "spoke") or True, node="mac")
    ev = ProactiveEvent(id=pq.next_id(), source="work", importance=2, created_at=5.0,
                        expires_at=None, summary="The export has finished.", dedupe_key="work:w1")
    pq.push(ev)
    ack = json.loads(q.get_nowait())
    pq.acked(ack["publish_id"])
    check("a published event lands in the real queue and is acked",
          landed == ["The export has finished."] and queue.pending
          and ack["type"] == "event.ack" and not pq.pending)
    pq._pushed.clear()  # forget it locally, as a fresh spoke process would
    pq.push(replace(ev, id="mac-2"))
    ack2 = json.loads(q.get_nowait())
    check("a resend is absorbed by the queue's dedupe and still acked",
          landed == ["The export has finished."] and ack2["type"] == "event.ack")

    server._on_frame(json.dumps({**beat(), "watches": [
        {"id": "w7", "kind": "file", "target": "/tmp/x", "label": "Done.", "created_at": 1.0, "expires_at": 2.0}
    ]}), "spoke")
    check("a heartbeat updates the view and the roster",
          remote.presence.state(101.0).present and [w.id for w in remote.watcher.active()] == ["w7"])


async def main() -> None:
    probe_view()
    probe_publish_queue()
    probe_heartbeat()
    probe_hub_side()
    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
