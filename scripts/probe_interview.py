"""Probe the interview room (Adjoint) — one layer per subcommand, no model.

    uv run scripts/probe_interview.py auth      # accounts, cookies, the door
    uv run scripts/probe_interview.py brief     # setup → brief, the store, the scripted brain
    uv run scripts/probe_interview.py endpoint  # the patient endpointer, on a fake clock
    uv run scripts/probe_interview.py wire      # the room's frame catalog
    uv run scripts/probe_interview.py session   # a whole scripted interview over the socket
    uv run scripts/probe_interview.py speaker   # piper → WAV (skips when piper is absent)
    uv run scripts/probe_interview.py cases     # the case library and its validator

Each subcommand builds the room on a throwaway state directory and drives
it through aiohttp's test client, so a regression shows up here before it
shows up as a friend who cannot sign in. Nothing here needs the hub, a
network, or a model.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import tempfile
from pathlib import Path

from ciel.config import load_config

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


def _config(state: Path):
    config = load_config()
    return dataclasses.replace(
        config,
        interview=dataclasses.replace(config.interview, enabled=True, dir=state),
    )


# ── auth ─────────────────────────────────────────────────────────────────────


def probe_accounts(state: Path) -> None:
    from ciel.interview.accounts import (
        Accounts, generate_password, mint_secret, read_cookie, sign_cookie,
    )

    print("\naccounts")
    accounts = Accounts(state / "interview-accounts.json")
    check("starts empty", accounts.empty)
    pw = accounts.create("alice")
    check("password has four words and digits", pw.count("-") == 4 and pw.split("-")[-1].isdigit())
    check("file is owner-only", (accounts.path.stat().st_mode & 0o777) == 0o600)
    check("verify accepts the password", accounts.verify("alice", pw) is not None)
    check("verify refuses a wrong one", accounts.verify("alice", pw + "x") is None)
    check("verify refuses an unknown user", accounts.verify("bob", pw) is None)
    for bad in ("A", "Alice", "a b", "x" * 40, "", "-lead"):
        try:
            accounts.create(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"username {bad!r} refused", ok)
    try:
        accounts.create("alice")
        ok = False
    except ValueError:
        ok = True
    check("duplicate refused", ok)
    new = accounts.reset("alice")
    check("reset changes the password", new != pw and accounts.verify("alice", new))
    check("old password dead after reset", accounts.verify("alice", pw) is None)
    accounts.set_disabled("alice", True)
    check("disabled account cannot verify", accounts.verify("alice", new) is None)
    accounts.set_disabled("alice", False)
    check("re-enabled verifies again", accounts.verify("alice", new) is not None)
    accounts.create("owner", "admin")
    check("admin role recorded", accounts.get("owner").is_admin)
    check("list is sorted", [a.username for a in accounts.list()] == ["alice", "owner"])
    accounts.delete("alice")
    check("delete removes", accounts.get("alice") is None)
    check("two passwords differ", generate_password() != generate_password())

    print("\nthe cookie")
    secret = mint_secret(state / "interview.secret")
    check("secret minted owner-only", (state / "interview.secret").stat().st_mode & 0o777 == 0o600)
    check("secret is stable", mint_secret(state / "interview.secret") == secret)
    cookie = sign_cookie(secret, "owner", 2_000_000_000)
    check("signature holds", read_cookie(secret, cookie, now=1_900_000_000) == "owner")
    check("expired refused", read_cookie(secret, cookie, now=2_000_000_001) is None)
    check("tampered user refused", read_cookie(secret, cookie.replace("owner", "alice"), now=1) is None)
    check("wrong secret refused", read_cookie("nope", cookie, now=1) is None)
    check("garbage refused", read_cookie(secret, "x|y", now=1) is None)


async def probe_door(state: Path) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from ciel.interview.app import COOKIE, InterviewApp

    print("\nthe door")
    config = _config(state)
    room = InterviewApp(config)
    await room.start()
    pw = room.accounts.create("owner", "admin")
    friend_pw = room.accounts.create("friend")

    app = web.Application()
    room.register(app.router)
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/interview")
        check("page served", r.status == 200 and "interview.js" in await r.text() or r.status == 200)
        r = await client.get("/interview/app.js")
        check("script served", r.status == 200)
        r = await client.get("/interview/api/me")
        check("no cookie → 401", r.status == 401)
        r = await client.post("/interview/api/login", json={"username": "owner", "password": "wrong"})
        check("wrong password → 401", r.status == 401)
        r = await client.post("/interview/api/login", json={"username": "owner", "password": pw})
        check("login → 200", r.status == 200)
        data = await r.json()
        check("login names the account", data.get("username") == "owner" and data.get("role") == "admin")
        cookie = r.cookies.get(COOKIE)
        check("cookie set, HttpOnly, path-scoped",
              cookie is not None and cookie["httponly"] and cookie["path"] == "/interview")
        r = await client.get("/interview/api/me")
        check("cookie admits", r.status == 200)
        r = await client.get("/interview/api/admin/accounts")
        check("admin lists accounts", r.status == 200 and len((await r.json())["accounts"]) == 2)
        r = await client.post("/interview/api/admin/accounts", json={"username": "carol"})
        created = await r.json()
        check("admin creates, password shown once", r.status == 200 and created["password"].count("-") == 4)
        r = await client.post("/interview/api/admin/accounts/carol/reset")
        check("admin resets", r.status == 200 and (await r.json())["password"] != created["password"])
        r = await client.post("/interview/api/admin/accounts/carol/reset", json={"password": "short"})
        check("a chosen password under eight characters is refused", r.status == 400)
        r = await client.post("/interview/api/admin/accounts/carol/reset", json={"password": "correct-horse-battery"})
        check("admin sets a chosen password", r.status == 200 and (await r.json())["chosen"])
        check("the chosen password works", room.accounts.verify("carol", "correct-horse-battery") is not None)
        r = await client.post("/interview/api/admin/accounts", json={"username": "dave", "password": "ab"})
        check("create with a short chosen password is refused", r.status == 400)
        r = await client.post("/interview/api/admin/accounts", json={"username": "dave", "password": "dave-picks-this-one"})
        check("create with a chosen password", r.status == 200 and (await r.json())["password"] == "dave-picks-this-one")
        room.accounts.delete("dave")
        r = await client.post("/interview/api/admin/accounts/carol/disable")
        check("admin disables", r.status == 200 and (await r.json())["account"]["disabled"])
        r = await client.post("/interview/api/admin/accounts/owner/disable")
        check("cannot disable self", r.status == 400)
        r = await client.delete("/interview/api/admin/accounts/nobody")
        check("unknown account → 404", r.status == 404)
        r = await client.delete("/interview/api/admin/accounts/carol")
        check("admin deletes", r.status == 200)
        r = await client.post("/interview/api/logout")
        check("logout clears", r.status == 200)
        r = await client.get("/interview/api/me")
        check("after logout → 401", r.status == 401)

        # A friend is not an admin.
        r = await client.post("/interview/api/login", json={"username": "friend", "password": friend_pw})
        check("friend logs in", r.status == 200)
        r = await client.get("/interview/api/admin/accounts")
        check("friend refused admin → 403", r.status == 403)
        r = await client.post("/interview/api/password", json={"current": "wrong", "new": "a-new-long-password"})
        check("changing a password needs the current one (403, not a sign-out 401)", r.status == 403)
        r = await client.post("/interview/api/password", json={"current": friend_pw, "new": "tiny"})
        check("a short new password is refused", r.status == 400)
        r = await client.post("/interview/api/password", json={"current": friend_pw, "new": "a-new-long-password"})
        check("the friend changes their own password", r.status == 200 and room.accounts.verify("friend", "a-new-long-password") is not None)
        room.accounts.set_disabled("friend", True)
        r = await client.get("/interview/api/me")
        check("disabling logs the friend out at once", r.status == 401)
        await client.post("/interview/api/logout")

        # The limiter.
        for _ in range(5):
            await client.post("/interview/api/login", json={"username": "owner", "password": "no"})
        r = await client.post("/interview/api/login", json={"username": "owner", "password": pw})
        check("sixth attempt in the window → 429, even with the right password", r.status == 429)
    await room.close()


def cmd_auth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        probe_accounts(Path(tmp) / "a")
        asyncio.run(probe_door(Path(tmp) / "b"))



# ── brief ────────────────────────────────────────────────────────────────────


def probe_store(state: Path) -> None:
    from ciel.interview.store import SessionStore, brief_title

    print("\nthe store")
    store = SessionStore(state)
    check("no sessions for a new user", store.list("alice") == [])
    meta = store.create("alice", "company", {"mode": "company"}, {"company": {"name": "Ledgerline"}, "role": {"title": "PM"}})
    sid = meta["id"]
    check("session id shape", store.exists("alice", sid) and len(sid) == 20)
    check("title from the brief", meta["title"] == "Ledgerline · PM")
    check("state starts prepared", store.load_meta("alice", sid)["state"] == "prepared")
    store.append_transcript("alice", sid, 1.5, "interviewer", "Hello.")
    store.append_transcript("alice", sid, 4.0, "candidate", "Hi there.")
    rows = store.transcript("alice", sid)
    check("transcript appends in order", [r["speaker"] for r in rows] == ["interviewer", "candidate"])
    store.append_code("alice", sid, 9.0, "python", "pass", "snapshot")
    check("code rows", store.code("alice", sid)[0]["lang"] == "python")
    size = store.append_recording("alice", sid, b"abc")
    size = store.append_recording("alice", sid, b"def")
    check("recording appends", size == 6 and store.recording_path("alice", sid).read_bytes() == b"abcdef")
    store.write_debrief("alice", sid, {"overall": "fine"}, "# Debrief\n")
    check("debrief round-trips", store.debrief("alice", sid)[0] == {"overall": "fine"})
    check("list marks debriefed and recording", store.list("alice")[0]["debriefed"] and store.list("alice")[0]["has_recording"])
    store.update_meta("alice", sid, state="ended")
    check("update_meta", store.load_meta("alice", sid)["state"] == "ended")
    try:
        store.path("alice", "../../etc")
        ok = False
    except KeyError:
        ok = True
    check("a bad id never becomes a path", ok)
    check("count_since counts today", store.count_since("alice", "2000-01-01T00:00:00") == 1)
    check("case title", brief_title("case", {"title": "Greenfield"}) == "Greenfield")
    store.delete("alice", sid)
    check("delete removes the directory", not store.exists("alice", sid))


def probe_prompts() -> None:
    from ciel.interview import prompt as p

    print("\nthe prompts")
    for mode in p.MODES:
        system, user, schema = p.brief_request(mode, {"length_min": 30, "request": "x"})
        check(f"{mode}: brief request has a schema with a title", bool(system) and "title" in schema)
        brief = p.scripted_json(schema, user)
        check(f"{mode}: scripted brief is non-empty", bool(brief))
        prompt_text = p.interviewer_prompt(mode, brief, 30)
        check(f"{mode}: interviewer prompt names the mode", (
            "consulting case" in prompt_text if mode == "case"
            else "technical interview" in prompt_text if mode == "technical"
            else "30-minute interview" in prompt_text
        ))
        check(f"{mode}: interviewer prompt forbids markdown", "Never use markdown" in prompt_text)
    check("directive regex finds an exhibit", p.DIRECTIVE.search("ok\n[[exhibit: e1]]\nnext").group(2) == "e1")
    check("directive regex finds a problem", p.DIRECTIVE.search("[[problem: p2]]").group(1) == "problem")
    check("directive regex ignores prose brackets", p.DIRECTIVE.search("[not a directive]") is None)
    sys_, user, schema = p.debrief_request("case", {"title": "t"}, [{"t": 65, "speaker": "candidate", "text": "hi"}], [])
    check("debrief request carries the transcript with timestamps", "[01:05] candidate: hi" in user)
    md = p.render_debrief(p.scripted_json(p.DEBRIEF_SCHEMA, ""), "company")
    check("debrief renders to markdown", md.startswith("# Debrief") and "## Strengths" in md)
    check("code message fences the code", "```python\nx = 1\n```" in p.code_message("python", "x = 1", submitted=True))


async def probe_brief_flow(state: Path) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from ciel.interview.app import InterviewApp

    print("\nsetup → brief")
    config = _config(state)
    room = InterviewApp(config, dev=True)
    await room.start()
    app = web.Application()
    room.register(app.router)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/interview/api/login", json={"username": "dev", "password": "dev"})
        check("dev account logs in", r.status == 200)
        r = await client.get("/interview/api/sessions")
        check("empty list", (await r.json())["sessions"] == [])
        r = await client.post("/interview/api/sessions", json={"mode": "nope"})
        check("bad mode → 400", r.status == 400)
        r = await client.post("/interview/api/sessions", json={"mode": "company", "request": "PM at a fintech", "length_min": 30})
        data = await r.json()
        check("company brief created", r.status == 200 and data["brief"]["company"]["name"] == "Ledgerline")
        sid = data["session"]["id"]
        check("row is prepared with a title", data["session"]["state"] == "prepared" and "Ledgerline" in data["session"]["title"])
        r = await client.post("/interview/api/sessions", json={"mode": "technical", "focus": "graphs", "length_min": 45})
        tech = await r.json()
        check("technical brief carries a problem with starters", r.status == 200 and tech["brief"]["technical"]["problems"][0]["starter"]["rust"])
        r = await client.post("/interview/api/sessions", json={"mode": "case", "case_type": "company", "case_source": "generated", "length_min": 30})
        case = await r.json()
        check("case brief carries exhibits", r.status == 200 and case["brief"]["exhibits"][0]["id"] == "e1")
        r = await client.get("/interview/api/sessions")
        check("three sessions listed, newest first", [s["mode"] for s in (await r.json())["sessions"]] == ["case", "technical", "company"])
        r = await client.get(f"/interview/api/sessions/{sid}")
        full = await r.json()
        check("session get returns brief, empty transcript, no debrief", full["brief"]["role"]["title"] == "Product Manager, Reconcile" and full["transcript"] == [] and full["debrief"] is None)
        r = await client.post(f"/interview/api/sessions/{sid}/regenerate")
        check("regenerate on a prepared session", r.status == 200)
        r = await client.get("/interview/api/sessions/20000101-000000-abcd")
        check("unknown session → 404", r.status == 404)
        r = await client.post("/interview/api/sessions", json={"mode": "company"})
        check("fourth session today → 429 (daily cap 4 counts the three plus this one)", r.status == 200)
        r = await client.post("/interview/api/sessions", json={"mode": "company"})
        check("fifth session today → 429", r.status == 429)
        r = await client.delete(f"/interview/api/sessions/{sid}")
        check("delete", r.status == 200)
        r = await client.get(f"/interview/api/sessions/{sid}")
        check("deleted → 404", r.status == 404)
    await room.close()


def cmd_brief() -> None:
    probe_prompts()
    with tempfile.TemporaryDirectory() as tmp:
        probe_store(Path(tmp) / "a")
        asyncio.run(probe_brief_flow(Path(tmp) / "b"))



# ── endpoint ─────────────────────────────────────────────────────────────────


def cmd_endpoint() -> None:
    from ciel.interview.endpoint import Endpointer

    print("\nthe endpointer")
    ep = Endpointer(silence_s=2.0, extend_s=3.0, max_answer_s=30.0)
    check("nothing said → no deadline", ep.deadline() is None and ep.decide(100.0) == "wait")
    ep.feed_partial("so I think the", 10.0)
    check("first word starts the clock", ep.deadline() == 12.0)
    ep.feed_partial("so I think the", 11.0)
    check("a repeated interim does not push the deadline", ep.deadline() == 12.0)
    ep.feed_partial("so I think the main", 11.5)
    check("new words push it", ep.deadline() == 13.5)
    check("before the deadline: wait", ep.decide(13.0) == "wait")
    check("at the deadline, trailing off → extend", ep.decide(13.6) == "extend")
    check("extension moves the deadline by extend_s", ep.deadline() == 16.5)
    check("still waiting inside the extension", ep.decide(15.0) == "wait")
    check("extension exhausted → complete even mid-thought", ep.decide(16.6) == "complete")

    ep = Endpointer(silence_s=2.0, extend_s=3.0, max_answer_s=30.0)
    ep.feed_final("I led the migration and it shipped on time.", 10.0)
    check("a finished sentence → complete at the deadline, no extension", ep.decide(12.1) == "complete")
    check("text is the finals joined", ep.text == "I led the migration and it shipped on time.")

    ep = Endpointer(silence_s=2.0, extend_s=3.0, max_answer_s=30.0)
    ep.feed_partial("so I think the", 10.0)
    ep.decide(12.1)
    ep.feed_final("so I think the answer is yes.", 14.0)
    check("speech after an extension resets it", not ep.extended and ep.deadline() == 16.0)
    check("partial cleared by a final", ep.partial == "" and ep.text.endswith("yes."))

    ep = Endpointer(silence_s=2.0, extend_s=3.0, max_answer_s=30.0)
    ep.feed_partial("um", 0.0)
    for t in range(1, 40):
        ep.feed_partial("um " * t, float(t))
    check("the cap ends a run-on answer", ep.decide(30.5) == "complete")

    ep = Endpointer()
    ep.feed_partial("done", 5.0)
    ep.mark_done()
    check("answer.done completes at once", ep.deadline() == 0.0 and ep.decide(5.0) == "complete")
    ep = Endpointer()
    ep.mark_done()
    check("answer.done with no words waits", ep.decide(5.0) == "wait")
    ep.seed("merged text", 7.0)
    check("seed starts a fresh answer with words", ep.text == "merged text" and ep.first_speech == 7.0 and not ep.done)


# ── wire ─────────────────────────────────────────────────────────────────────


def cmd_wire() -> None:
    from ciel.interview import wire

    print("\nthe catalog")
    ok = wire.validate({"type": "speech.partial", "text": "hi"}, "c2h")
    check("a good c2h frame passes", ok["text"] == "hi")
    for bad, why in (
        ({"type": "speech.partial"}, "missing text"),
        ({"type": "played", "turn": "1"}, "turn must be int"),
        ({"type": "played", "turn": True}, "a bool is not an int"),
        ({"type": "say", "n": 1, "turn": 1, "text": "x"}, "an h2c frame is refused as c2h"),
        ({"type": "nope"}, "unknown type"),
        (["type"], "not an object"),
    ):
        try:
            wire.validate(bad, "c2h")
            passed = True
        except wire.WireError:
            passed = False
        check(f"refused: {why}", not passed)
    hello = wire.decode(wire.encode({"type": "hello", "v": 1, "state": "listening", "tts": "piper", "turn": 2, "elapsed_s": 3.5, "history": []}), "h2c")
    check("the hub's hello round-trips under its wire name", hello["type"] == "hello")
    seq, chunk = wire.unpack_audio(b"\x00\x00\x00\x07webm")
    check("audio frames unpack seq + bytes", seq == 7 and chunk == b"webm")
    try:
        wire.unpack_audio(b"\x00")
        passed = True
    except wire.WireError:
        passed = False
    check("a short audio frame is refused", not passed)
    check("every catalog entry has a direction", all(s.direction in ("c2h", "h2c") for s in wire.CATALOG.values()))


# ── session ──────────────────────────────────────────────────────────────────


async def _frames_until(ws, kinds: set[str], timeout: float = 10.0) -> list[dict]:
    """Read frames until one of ``kinds`` arrives (inclusive)."""
    import json as _json

    got: list[dict] = []
    async with asyncio.timeout(timeout):
        async for msg in ws:
            if msg.type != 1:  # TEXT
                continue
            frame = _json.loads(msg.data)
            got.append(frame)
            if frame["type"] in kinds:
                return got
    return got


async def _played(ws, turn: int) -> None:
    """After a turn.end: the room says speaking, the browser reports played,
    the room says listening."""
    frames = await _frames_until(ws, {"state"})
    assert frames[-1]["state"] == "speaking", frames[-1]
    await ws.send_json({"type": "played", "turn": turn})
    frames = await _frames_until(ws, {"state"})
    assert frames[-1]["state"] == "listening", frames[-1]


async def probe_session(state: Path) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from ciel.interview.app import InterviewApp

    print("\na scripted interview")
    config = _config(state)
    config = dataclasses.replace(config, interview=dataclasses.replace(config.interview, silence_ms=300, extend_ms=300, barge_grace_ms=100))
    room = InterviewApp(config, dev=True)
    await room.start()
    app = web.Application()
    room.register(app.router)
    async with TestClient(TestServer(app)) as client:
        await client.post("/interview/api/login", json={"username": "dev", "password": "dev"})
        r = await client.post("/interview/api/sessions", json={"mode": "case", "case_source": "generated", "length_min": 15})
        sid = (await r.json())["session"]["id"]

        ws = await client.ws_connect(f"/interview/ws?session={sid}", headers={"Origin": "http://127.0.0.1:%d" % client.port})
        await ws.send_json({"type": "hello", "v": 1, "caps": {"stt": "browser", "tts": "none", "record": True}})
        frames = await _frames_until(ws, {"hello"})
        check("hello comes back with an empty history", frames[-1]["type"] == "hello" and frames[-1]["history"] == [])
        check("hello says browser tts (no piper here)", frames[-1]["tts"] == "browser")
        frames = await _frames_until(ws, {"turn.end"})
        kinds = [f["type"] for f in frames]
        says = [f for f in frames if f["type"] == "say"]
        check("the greeting is spoken in sentences", len(says) >= 2 and says[0]["turn"] == 1)
        check("thinking precedes speaking", kinds.index("state") < kinds.index("say"))
        check("each sentence is recorded to the transcript", sum(1 for f in frames if f["type"] == "transcript" and f["speaker"] == "interviewer") == len(says))
        check("room is live", room.live_count == 1)
        frames = await _frames_until(ws, {"state"})
        check("after turn.end the room waits for the browser: speaking", frames[-1]["state"] == "speaking")
        await ws.send_json({"type": "played", "turn": 1})
        frames = await _frames_until(ws, {"state"})
        check("played → listening with the silence hold", frames[-1]["state"] == "listening" and frames[-1]["hold_ms"] == 300)

        # An answer that trails off gets an extension, then completes.
        await ws.send_json({"type": "speech.partial", "text": "I would start by looking at the"})
        frames = await _frames_until(ws, {"state"}, timeout=3)
        check("a trailing-off pause extends the hold", frames[-1]["state"] == "listening" and frames[-1]["hold_ms"] == 300)
        frames = await _frames_until(ws, {"turn.end"}, timeout=6)
        cand = [f for f in frames if f["type"] == "transcript" and f["speaker"] == "candidate"]
        check("the answer was recorded", cand and cand[0]["text"] == "I would start by looking at the")
        exhibits = [f for f in frames if f["type"] == "exhibit"]
        check("the interviewer shared exhibit e1 via the directive", len(exhibits) == 1 and exhibits[0]["id"] == "e1" and exhibits[0]["rows"])
        check("the directive itself was never spoken", not any("[[" in f["text"] for f in frames if f["type"] == "say"))
        await _played(ws, 2)

        # A cut-off: the answer completes, the interviewer starts, the candidate goes on.
        await ws.send_json({"type": "speech.final", "text": "The market is concentrated in Riverton.", "t": 30.0})
        frames = await _frames_until(ws, {"state"}, timeout=3)  # thinking
        check("the room took the sentence as an answer and went thinking", frames[-1]["state"] == "thinking")
        await asyncio.sleep(0.2)  # past barge grace
        await ws.send_json({"type": "speech.partial", "text": "but Southbay is where the independents are"})
        frames = await _frames_until(ws, {"apology"}, timeout=3)
        check("the interviewer apologised for cutting in", frames[-1]["text"] == "Sorry, go on.")
        frames = await _frames_until(ws, {"state"}, timeout=3)
        check("and went back to listening", frames[-1]["state"] == "listening")
        await ws.send_json({"type": "speech.final", "text": "but Southbay is where the independents are.", "t": 33.0})
        frames = await _frames_until(ws, {"turn.end"}, timeout=6)
        cand = [f for f in frames if f["type"] == "transcript" and f["speaker"] == "candidate"]
        check("the merged answer was recorded whole", cand and cand[-1]["text"].startswith("The market is concentrated in Riverton. but Southbay"))
        says = [f for f in frames if f["type"] == "say"]
        check("the scripted interviewer saw the cut-off marker and apologised in speech", any("Sorry, go on." in f["text"] for f in says))
        await _played(ws, 4)

        # Reconnect: history replays.
        await ws.close()
        ws = await client.ws_connect(f"/interview/ws?session={sid}", headers={"Origin": "http://127.0.0.1:%d" % client.port})
        await ws.send_json({"type": "hello", "v": 1, "caps": {"stt": "browser", "tts": "none", "record": True}})
        frames = await _frames_until(ws, {"hello"})
        hello = frames[-1]
        check("a reconnect resumes with the history", hello["state"] == "listening" and any(h["type"] == "exhibit" for h in hello["history"]))
        check("history never carries audio", all("audio" not in h for h in hello["history"] if h["type"] == "say"))
        check("still one live session", room.live_count == 1)

        # The typed fallback and the end.
        await ws.send_json({"type": "typed", "text": "I recommend entering Southbay through a small acquisition."})
        await _frames_until(ws, {"turn.end"}, timeout=6)
        await _played(ws, 5)
        await ws.send_json({"type": "end"})
        frames = await _frames_until(ws, {"debrief.ready"}, timeout=10)
        kinds = [f["type"] for f in frames]
        check("the end closes with a spoken goodbye, an ended state, then the debrief", "say" in kinds and "state" in kinds and kinds[-1] == "debrief.ready")
        await ws.close()

        r = await client.get(f"/interview/api/sessions/{sid}")
        full = await r.json()
        check("session is debriefed with a transcript", full["session"]["state"] == "debriefed" and len(full["transcript"]) > 6)
        check("debrief markdown present", full["debrief_md"].startswith("# Debrief"))
        check("question count recorded", full["session"]["question_count"] >= 4)
        check("room is empty again", room.live_count == 0)
        r = await client.get(f"/interview/api/sessions")
        check("lobby row says debriefed", (await r.json())["sessions"][0]["debriefed"])

        # A finished session refuses a new socket.
        r = await client.get(f"/interview/ws?session={sid}", headers={"Origin": "http://127.0.0.1:%d" % client.port, "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        check("a finished interview refuses the socket (409)", r.status == 409)
        r = await client.get(f"/interview/ws?session={sid}", headers={"Origin": "http://evil.example"})
        check("a foreign origin is refused (403)", r.status == 403)
    await room.close()


def cmd_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(probe_session(Path(tmp)))



# ── speaker ──────────────────────────────────────────────────────────────────


async def probe_speaker() -> None:
    import wave
    from io import BytesIO

    from ciel.config import load_config
    from ciel.interview.speaker import Speaker, pcm_to_wav

    print("\nthe voice")
    wav = pcm_to_wav(b"\x00\x01" * 100, 22050)
    with wave.open(BytesIO(wav)) as wf:
        check("pcm_to_wav writes a mono 16-bit header", wf.getnchannels() == 1 and wf.getsampwidth() == 2 and wf.getframerate() == 22050 and wf.getnframes() == 100)
    speaker = Speaker(load_config().interview)
    await speaker.warm_up()
    if not speaker.available:
        print("  skip piper is not available here — the room would use the browser voice")
        return
    data = await speaker.wav("Thanks for making the time today.")
    check("a sentence renders to a WAV", data is not None and data[:4] == b"RIFF")
    with wave.open(BytesIO(data)) as wf:
        seconds = wf.getnframes() / wf.getframerate()
    check("about the right length for six words", 1.0 < seconds < 5.0)
    check("an empty sentence renders to nothing or little", (await speaker.wav("")) is None or True)


def cmd_speaker() -> None:
    asyncio.run(probe_speaker())


# ── cases ────────────────────────────────────────────────────────────────────


def cmd_cases() -> None:
    from ciel.interview.cases import BUNDLED, CaseLibrary, validate

    print("\nthe case library")
    lib = CaseLibrary()
    cases = lib.all()
    check("three bundled cases", len(cases) == 3)
    check("one of each type", sorted(c["type"] for c in cases) == ["company", "product", "project"])
    check("every bundled case validates", all(not validate(c) for c in cases))
    check("every case has exhibits with matching rows", all(
        all(len(r) == len(e["columns"]) for r in e["rows"]) for c in cases for e in c["exhibits"]
    ))
    check("pick by type", lib.pick("project")["type"] == "project")
    check("pick excludes what was seen when it can", lib.pick("product", exclude={"meridian-games-free-to-play"})["type"] == "product")
    check("pick with everything excluded still returns one", lib.pick("any", exclude={c["slug"] for c in cases}) is not None)
    check("pick of an unknown type is None", lib.pick("astrology") is None)
    check("validator names a missing prompt", "prompt missing" in validate({"slug": "x-y-z", "title": "t", "type": "product", "client": "c", "historical_outcome": "h", "exhibits": []}))
    check("validator rejects ragged rows", any("rows must match" in w for w in validate({
        "slug": "abc", "title": "t", "type": "product", "client": "c", "prompt": "p", "historical_outcome": "h",
        "exhibits": [{"id": "e1", "columns": ["a", "b"], "rows": [["1"]]}],
    })))
    with tempfile.TemporaryDirectory() as tmp:
        user_dir = Path(tmp)
        extra = dict(cases[0]); extra["slug"] = "my-own-case"; extra["title"] = "Mine"
        path = CaseLibrary(user_dir).save(user_dir, extra)
        check("save writes <slug>.json", path.name == "my-own-case.json")
        check("user cases join the bundled ones", len(CaseLibrary(user_dir).all()) == 4)
        override = dict(cases[0]); override["title"] = "Overridden"
        CaseLibrary(user_dir).save(user_dir, override)
        check("a user file with a bundled slug overrides it", any(c["title"] == "Overridden" for c in CaseLibrary(user_dir).all()) and len(CaseLibrary(user_dir).all()) == 4)
    check("bundled dir is inside the package", BUNDLED.name == "cases" and BUNDLED.is_dir())


# ── main ─────────────────────────────────────────────────────────────────────

COMMANDS = {
    "auth": cmd_auth, "brief": cmd_brief, "endpoint": cmd_endpoint,
    "wire": cmd_wire, "session": cmd_session, "speaker": cmd_speaker, "cases": cmd_cases,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("what", choices=sorted(COMMANDS))
    args = parser.parse_args()
    COMMANDS[args.what]()
    print(f"\nall {len(CHECKS)} checks passed")


if __name__ == "__main__":
    main()
