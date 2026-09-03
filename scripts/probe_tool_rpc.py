"""Probe the tool RPC — the hub's hands on the Mac, over the wire.

    uv run scripts/probe_tool_rpc.py

A loopback pair: the real ``HubServer`` with a planted spoke socket, a
responder task that feeds every ``tool.request`` to a real ``Executor``
whose components are fakes, and the executor's answers routed back as
``tool.result`` frames. Over that, the hub's remote duck types — the
screen, Messages, the locator, the work watcher, the calendar, the Mac's
shell and files — and the tools that bind them, end to end. Then the
edges: no spoke, a spoke that never answers (the deadline, the cancel),
the spoke leaving mid-call, an unknown tool, and the executor's own
guards — the deny tier refused whatever the hub says, the confirm tier
refused without the hub's word, the workspace guard on the files.
"""

import asyncio
import base64
import json
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ciel.brain.tools import mac as mac_tools
from ciel.brain.tools import watch as watch_tool
from ciel.brain.tools.screen import bind_screen, look_at_screen
from ciel.config import Config, FilesConfig, HubConfig, ShellConfig, WebConfig
from ciel.hub.rpc import RemoteBindings, RpcUnavailable
from ciel.hub.server import HubServer
from ciel.messages import Contact, Message, MessagesUnavailable
from ciel.proactive.work import Watch
from ciel.remote.web import Admission
from ciel.spoke.executor import Executor

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


# ── fakes for the executor ───────────────────────────────────────────────────


class FakeMessages:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def find_contacts(self, query):
        if query == "nobody":
            return []
        return [Contact(name="Sam Lee", kind="phone", handle="+15550001")]

    async def recent(self, limit=20, handle=None):
        return [Message(text="on my way", from_me=False, handle="+15550001", chat=None,
                        when=datetime(2026, 9, 2, 12, 30), is_group=False)]

    async def send(self, handle, text):
        if handle == "group":
            raise MessagesUnavailable("group sends are broken")
        self.sent.append((handle, text))


class FakeLocator:
    def current(self, max_age_s):
        from ciel.location import Fix
        return Fix(at=1000.0, source="wifi", network="HomeNet")

    def describe(self, fix):
        return "At home — this Mac's network, just now."


class FakeWatcher:
    def __init__(self):
        self.watches: list[Watch] = []

    def add_file(self, target, label, timeout_minutes):
        w = Watch(id=f"w{len(self.watches)+1}", kind="file", target=target, label=label,
                  created_at=1.0, expires_at=1.0 + timeout_minutes * 60)
        self.watches.append(w)
        return w

    def add_pid(self, pid, label, timeout_minutes):
        w = Watch(id=f"w{len(self.watches)+1}", kind="pid", target=str(pid), label=label,
                  created_at=1.0, expires_at=1.0 + timeout_minutes * 60)
        self.watches.append(w)
        return w

    def active(self):
        return list(self.watches)


class FakeCalendar:
    async def agenda_today(self):
        return ["10:00 Standup", "14:00 Dentist"]


def text_of(result: dict) -> str:
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


# ── the loopback pair ────────────────────────────────────────────────────────


class Pair:
    """A hub server, a seated fake spoke, and a real executor answering it."""

    def __init__(self, config: Config, *, answer: bool = True, executor_kwargs=None):
        self.server = HubServer(WebConfig(), config.hub)
        self.queue, _ = self.server._welcome("spoke", Admission(True, role="spoke", client_id="probe"))
        self.queue.get_nowait()  # the hello
        self.executor = Executor(
            config,
            lambda frame: self.server._on_frame(json.dumps(frame), "spoke") or True,
            **(executor_kwargs or {}),
        )
        self.seen: list[dict] = []
        self.answer = answer
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            f = json.loads(await self.queue.get())
            self.seen.append(f)
            if f["type"] in ("tool.request", "tool.cancel") and self.answer:
                self.executor.handle(f)

    def leave(self):
        self.server._clients.pop("spoke", None)
        self.server._client_left("spoke")

    async def close(self):
        self.task.cancel()
        await self.executor.close()


def make_config(tmp: Path) -> Config:
    return replace(
        Config(),
        state_dir=tmp,
        hub=replace(HubConfig(), speak_timeout_s=1.0),
        shell=replace(ShellConfig(), enabled=True, command_timeout_s=2.0),
        files=replace(FilesConfig(), enabled=True, workspace=tmp / "ws"),
    )


# ── the probes ───────────────────────────────────────────────────────────────


async def probe_senses(config: Config) -> None:
    print("the senses, over the wire")
    pair = Pair(config, executor_kwargs=dict(
        messages=FakeMessages(), locator=FakeLocator(), watcher=FakeWatcher(),
        calendar=FakeCalendar(),
    ))
    remote = RemoteBindings(pair.server, config)

    contacts = await remote.messages.find_contacts("sam")
    check("find_contacts crosses the wire as Contact objects",
          contacts == [Contact(name="Sam Lee", kind="phone", handle="+15550001")])
    msgs = await remote.messages.recent(limit=5)
    check("recent messages come back as Message objects with their time",
          len(msgs) == 1 and msgs[0].text == "on my way" and msgs[0].when == datetime(2026, 9, 2, 12, 30))
    await remote.messages.send("+15550001", "hi")
    check("send reaches the Mac's client", pair.executor._messages.sent == [("+15550001", "hi")])
    try:
        await remote.messages.send("group", "hi")
        check("a refused send raises MessagesUnavailable", False)
    except MessagesUnavailable as exc:
        check("a refused send raises MessagesUnavailable", "group sends" in str(exc))

    check("the locator describes itself on the Mac",
          await remote.locator.describe_now(120) == "At home — this Mac's network, just now.")
    fix = await remote.locator.current_fix(120)
    check("a fix crosses as a Fix", fix is not None and fix.network == "HomeNet")

    w = await remote.watcher.add_file("/tmp/out.mp4", "The export has finished.", 30)
    check("a file watch is armed on the Mac and comes back as a Watch",
          w.id == "w1" and w.kind == "file" and w.expires_at == 1.0 + 1800)
    w2 = await remote.watcher.add_pid(4242, "The build is done.", 10)
    check("a pid watch too", w2.kind == "pid" and w2.target == "4242")
    check("active() answers from the heartbeat, empty until one arrives", remote.watcher.active() == [])
    remote.watcher.observe([{"id": "w1", "kind": "file", "target": "/tmp/out.mp4",
                             "label": "x", "created_at": 1.0, "expires_at": 2.0}, {"bad": 1}])
    check("a heartbeat roster parses, skipping malformed entries",
          [x.id for x in remote.watcher.active()] == ["w1"])

    check("the brief's agenda comes from the Mac's store",
          await remote.calendar.agenda_today() == ["10:00 Standup", "14:00 Dentist"])

    print("\nthe screen")
    bind_screen(config.screen, remote.screen)

    async def fake_capture(max_edge):
        return [b"\xff\xd8jpeg-bytes"]

    import ciel.brain.tools.screen as screen_mod
    original = screen_mod.capture_displays
    screen_mod.capture_displays = fake_capture
    try:
        result = await look_at_screen.handler({})
        images = [c for c in result["content"] if c["type"] == "image"]
        check("look_at_screen captures on the Mac and returns the image",
              len(images) == 1 and base64.b64decode(images[0]["data"]) == b"\xff\xd8jpeg-bytes")
    finally:
        screen_mod.capture_displays = original
        bind_screen(config.screen, None)

    print("\nthe watch tool, bound to the wire")
    watch_tool.bind_watcher(remote.watcher)
    result = await watch_tool.watch_for_completion.handler(
        {"path": str(Path(tempfile.gettempdir())), "label": "The thing is done.", "timeout_minutes": 5}
    )
    check("the tool arms a watch on the Mac even for a path that exists here",
          "Watching for" in text_of(result) and "id w3" in text_of(result))
    await pair.close()


async def probe_mac(config: Config) -> None:
    print("\nthe Mac's shell and files")
    (config.files.workspace).mkdir(parents=True, exist_ok=True)
    (config.files.workspace / "notes.txt").write_text("hello from the mac\n")
    pair = Pair(config)
    remote = RemoteBindings(pair.server, config)
    mac_tools.bind_mac(remote.mac, config.shell)

    out = await mac_tools.run_on_mac.handler({"command": "echo hi"})
    check("a quiet command runs and reports exit and stdout",
          "exit 0" in text_of(out) and "hi" in text_of(out))
    seen = [f for f in pair.seen if f["type"] == "tool.request" and f["tool"] == "shell.run"]
    check("the quiet tier rides down unconfirmed", seen[-1]["args"]["confirmed"] is False)

    out = await mac_tools.run_on_mac.handler({"command": "sudo rm -rf /"})
    check("the deny tier is refused on the Mac regardless",
          "refused on the Mac" in text_of(out))

    out = await mac_tools.run_on_mac.handler({"command": "touch newfile"})
    check("a side-effect command from the tool arrives marked confirmed (the hook asked)",
          [f for f in pair.seen if f["type"] == "tool.request"][-1]["args"]["confirmed"] is True
          and "exit 0" in text_of(out))

    res = await pair.server.rpc("shell.run", {"command": "touch other", "confirmed": False}, 5)
    check("without the hub's word, a side-effect command is refused by the spoke",
          res["ok"] is False and "needs the user's yes" in res["error"])

    res = await pair.server.rpc("shell.run", {"command": "sleep 5", "confirmed": True}, 5)
    check("a command past the Mac's timeout is killed and reported",
          res["ok"] is False and "ran past" in res["error"])

    out = await mac_tools.mac_read_file.handler({"path": "notes.txt"})
    check("a relative read resolves inside the Mac's workspace",
          text_of(out) == "hello from the mac\n")
    out = await mac_tools.mac_write_file.handler({"path": "sub/new.txt", "content": "written"})
    check("a write lands, directories made", (config.files.workspace / "sub/new.txt").read_text() == "written")
    out = await mac_tools.mac_list_dir.handler({"path": "."})
    names = set(text_of(out).split())
    check("a listing marks directories (and shows the file the confirmed command made)",
          {"notes.txt", "sub/", "newfile"} <= names and "sub" not in names)
    out = await mac_tools.mac_write_file.handler({"path": "/etc/hosts", "content": "x"})
    check("a write outside the workspace is refused on the Mac",
          "refused on the Mac" in text_of(out))
    out = await mac_tools.mac_read_file.handler({"path": "~/.ssh/id_rsa"})
    check("a forbidden name is refused on the Mac", "refused on the Mac" in text_of(out))
    await pair.close()


async def probe_edges(config: Config) -> None:
    print("\nthe edges")
    server = HubServer(WebConfig(), config.hub)
    remote = RemoteBindings(server, config)
    try:
        await remote.locator.current_fix(1)
        check("no spoke: RpcUnavailable", False)
    except RpcUnavailable as exc:
        check("no spoke: RpcUnavailable", "not connected" in str(exc))
    check("the location tool's wording survives it",
          "not available" in await remote.locator.describe_now(1))
    mac_tools.bind_mac(remote.mac, config.shell)
    check("the Mac tools say so in words",
          "Could not run that on the Mac" in text_of(await mac_tools.run_on_mac.handler({"command": "ls"})))

    pair = Pair(config, answer=False)
    remote = RemoteBindings(pair.server, config)
    try:
        await pair.server.rpc("screen.capture", {"max_edge": 100}, 0.2)
        check("a spoke that never answers hits the deadline", False)
    except RpcUnavailable as exc:
        check("a spoke that never answers hits the deadline", "did not answer" in str(exc))
    await asyncio.sleep(0.01)
    check("and is sent a cancel", any(f["type"] == "tool.cancel" for f in pair.seen))
    await pair.close()

    pair = Pair(config, answer=False)
    call = asyncio.create_task(pair.server.rpc("screen.capture", {"max_edge": 100}, 5))
    await asyncio.sleep(0.02)
    pair.leave()
    try:
        await call
        check("the spoke leaving mid-call fails the call at once", False)
    except RpcUnavailable as exc:
        check("the spoke leaving mid-call fails the call at once", "disconnected" in str(exc))
    await pair.close()

    pair = Pair(config)
    res = await pair.server.rpc("warp.drive", {}, 2)
    check("an unknown tool is a sentence, not a hang", res["ok"] is False and "does not know" in res["error"])
    res = await pair.server.rpc("messages.recent", {}, 2)
    check("a sense the Mac has off says so", res["ok"] is False and "off on the Mac" in res["error"])
    await pair.close()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    config = make_config(tmp)
    await probe_senses(config)
    await probe_mac(config)
    await probe_edges(config)
    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
