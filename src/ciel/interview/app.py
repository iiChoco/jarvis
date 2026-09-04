"""The room's HTTP surface: the page, the login, the admin panel, and — in
later phases — the sessions and the live socket.

Mounted onto the hub's existing aiohttp application by ``WebLink.start()``
(the Chart's server), under ``/interview``. Every handler here runs its own
door: a signed cookie, checked against the account file on each request,
so a disabled friend is out at once. Nothing here can reach the hub token,
the Chart's queue, or the personal brain — the room holds a
``Config`` for its own section and nothing that leads elsewhere.

The page and its script are read from disk per request, as the Chart's
are: an edit shows on refresh, and the designer's HTML can be dropped in
without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

from ciel.config import Config
from ciel.interview import prompt as prompts
from ciel.interview import wire
from ciel.interview.brain import Backend, BackendError, make_backend
from ciel.interview.session import InterviewSession
from ciel.interview.store import SessionStore
from ciel.interview.accounts import (
    ACCOUNTS_FILE,
    SECRET_FILE,
    USERNAME,
    Account,
    Accounts,
    mint_secret,
    read_cookie,
    sign_cookie,
)

log = logging.getLogger(__name__)

COOKIE = "ciel_iv"
PREFIX = "/interview"

_REMOTE = Path(__file__).resolve().parents[1] / "remote"
_PAGE = _REMOTE / "interview.html"
_SCRIPT = _REMOTE / "interview.js"

_LOGIN_WINDOW_S = 900.0
_LOGIN_LIMIT = 5

Handler = Callable[[Any], Awaitable[Any]]


class InterviewApp:
    """The room, as routes on somebody else's server."""

    def __init__(self, config: Config, *, dev: bool = False, port: int | None = None) -> None:
        self._config = config
        self._cfg = config.interview
        self._dev = dev
        self._port = port or config.web.port
        """The port the page is served on, for the socket's Origin gate."""
        """Loopback development: a ``dev``/``dev`` account exists, cookies
        skip the Secure flag, and the brain is scripted (later phases)."""
        self._dir: Path = self._cfg.dir
        self._accounts = Accounts(self._dir / ACCOUNTS_FILE)
        self._secret = ""
        self._attempts: dict[str, deque[float]] = {}
        """Failed logins by username and by peer address, for the limiter."""
        self._store = SessionStore(self._dir)
        self._live: dict[tuple[str, str], InterviewSession] = {}
        """Interviews in progress, by (username, session id)."""
        self._speaker: Any = None
        """The interviewer's voice — piper, when it loads (phase 3)."""
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def accounts(self) -> Accounts:
        return self._accounts

    @property
    def live_count(self) -> int:
        """Interviews in progress — the pipeline's idle gate reads this so a
        source reload waits for them."""
        return sum(1 for s in self._live.values() if s.live)

    async def pause_all(self, reason: str = "the room is restarting") -> None:
        """End every live interview gracefully (debriefs included) — the
        pipeline calls this when a reload can wait no longer."""
        for session in list(self._live.values()):
            with contextlib.suppress(Exception):
                await session.pause(reason)

    async def start(self) -> None:
        if self._started:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._secret = mint_secret(self._dir / SECRET_FILE)
        from ciel.interview.speaker import Speaker

        self._speaker = Speaker(self._cfg)
        await self._speaker.warm_up()
        if self._dev and self._accounts.get("dev") is None:
            self._accounts.create("dev", "admin", password="dev")
            log.info("interview dev account: dev / dev")
        self._started = True
        log.info(
            "interview room at %s (%d account%s, voice: %s)",
            PREFIX, len(self._accounts.list()),
            "" if len(self._accounts.list()) == 1 else "s",
            "piper" if self._speaker.available else "browser",
        )

    async def close(self) -> None:
        for session in list(self._live.values()):
            with contextlib.suppress(Exception):
                await session.close()
        self._live.clear()

    # ── routes ───────────────────────────────────────────────────────────────

    def register(self, router: Any) -> None:
        """Add the room's routes to an aiohttp router."""
        api = f"{PREFIX}/api"
        router.add_get(PREFIX, self._serve_page)
        router.add_get(f"{PREFIX}/", self._serve_page)
        router.add_get(f"{PREFIX}/app.js", self._serve_script)
        router.add_post(f"{api}/login", self._login)
        router.add_post(f"{api}/logout", self._logout)
        router.add_get(f"{api}/me", self._me)
        router.add_post(f"{api}/password", self._change_password)
        router.add_get(f"{api}/sessions", self._sessions_list)
        router.add_post(f"{api}/sessions", self._sessions_create)
        router.add_get(f"{api}/sessions/{{id}}", self._session_get)
        router.add_post(f"{api}/sessions/{{id}}/regenerate", self._session_regenerate)
        router.add_delete(f"{api}/sessions/{{id}}", self._session_delete)
        router.add_get(f"{api}/sessions/{{id}}/recording", self._session_recording)
        router.add_get(f"{PREFIX}/ws", self._serve_ws)
        router.add_get(f"{api}/admin/accounts", self._admin_list)
        router.add_post(f"{api}/admin/accounts", self._admin_create)
        router.add_post(f"{api}/admin/accounts/{{u}}/reset", self._admin_reset)
        router.add_post(f"{api}/admin/accounts/{{u}}/disable", self._admin_disable)
        router.add_post(f"{api}/admin/accounts/{{u}}/enable", self._admin_enable)
        router.add_delete(f"{api}/admin/accounts/{{u}}", self._admin_delete)

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _json(data: Any, status: int = 200) -> Any:
        from aiohttp import web

        return web.json_response(data, status=status)

    @staticmethod
    def _fail(status: int, code: str, reason: str) -> Any:
        from aiohttp import web

        return web.json_response(
            {"error": code, "reason": reason}, status=status,
            headers={"Cache-Control": "no-store"},
        )

    @staticmethod
    def _secure(request: Any) -> bool:
        forwarded = request.headers.get("X-Forwarded-Proto", "")
        return request.secure or forwarded.split(",")[0].strip() == "https"

    def _user(self, request: Any) -> Account | None:
        """The live account the request's cookie names, or None."""
        raw = request.cookies.get(COOKIE)
        if not raw or not self._secret:
            return None
        username = read_cookie(self._secret, raw)
        if username is None:
            return None
        account = self._accounts.get(username)
        if account is None or account.disabled:
            return None
        return account

    def _require(self, request: Any, *, admin: bool = False) -> Account:
        from aiohttp import web

        account = self._user(request)
        if account is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "unauthorized", "reason": "sign in"}),
                content_type="application/json",
            )
        if admin and not account.is_admin:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "forbidden", "reason": "owner only"}),
                content_type="application/json",
            )
        return account

    async def _body(self, request: Any) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - a bad body is a 400, not a trace
            return {}
        return data if isinstance(data, dict) else {}

    def _limited(self, key: str, now: float) -> bool:
        window = self._attempts.get(key)
        if window is None:
            return False
        while window and now - window[0] > _LOGIN_WINDOW_S:
            window.popleft()
        return len(window) >= _LOGIN_LIMIT

    def _note_failure(self, key: str, now: float) -> None:
        self._attempts.setdefault(key, deque()).append(now)

    @staticmethod
    def _peer(request: Any) -> str:
        forwarded = request.headers.get("CF-Connecting-IP") or request.headers.get(
            "X-Forwarded-For", ""
        )
        if forwarded:
            return forwarded.split(",")[0].strip()
        return str(request.remote or "-")

    # ── page ─────────────────────────────────────────────────────────────────

    async def _serve_file(self, path: Path, content_type: str) -> Any:
        from aiohttp import web

        try:
            body = await asyncio.to_thread(path.read_text, "utf-8")
        except OSError:
            log.exception("could not read %s", path.name)
            raise web.HTTPNotFound
        return web.Response(
            text=body, content_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    async def _serve_page(self, request: Any) -> Any:
        return await self._serve_file(_PAGE, "text/html")

    async def _serve_script(self, request: Any) -> Any:
        return await self._serve_file(_SCRIPT, "application/javascript")

    # ── auth ─────────────────────────────────────────────────────────────────

    async def _login(self, request: Any) -> Any:
        from aiohttp import web

        body = await self._body(request)
        username = str(body.get("username", "")).strip().lower()
        password = str(body.get("password", ""))
        now = time.time()
        peer = self._peer(request)
        if self._limited(f"ip:{peer}", now) or (
            username and self._limited(f"user:{username}", now)
        ):
            return self._fail(429, "too_many_attempts", "try again in a few minutes")
        account = None
        if USERNAME.match(username) and password:
            account = await asyncio.to_thread(self._accounts.verify, username, password)
        if account is None:
            self._note_failure(f"ip:{peer}", now)
            if username:
                self._note_failure(f"user:{username}", now)
            log.info("interview login refused for %r from %s", username, peer)
            return self._fail(401, "bad_login", "wrong username or password")
        self._accounts.touch(account.username)
        expires = now + self._cfg.cookie_days * 86400
        response = web.json_response(self._me_payload(account))
        response.set_cookie(
            COOKIE, sign_cookie(self._secret, account.username, expires),
            max_age=self._cfg.cookie_days * 86400, path=PREFIX,
            httponly=True, samesite="Lax", secure=self._secure(request),
        )
        log.info("interview login: %s from %s", account.username, peer)
        return response

    async def _logout(self, request: Any) -> Any:
        from aiohttp import web

        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path=PREFIX)
        return response

    def _me_payload(self, account: Account) -> dict[str, Any]:
        return {
            "username": account.username,
            "role": account.role,
            "caps": self._caps(),
            "limits": {
                "daily_sessions": self._cfg.daily_sessions_per_user,
                "max_answer_s": self._cfg.max_answer_s,
            },
        }

    def _caps(self) -> dict[str, Any]:
        piper = self._speaker is not None and getattr(self._speaker, "available", False)
        return {"stt": "browser", "tts": "piper" if piper else "browser", "record": True}

    async def _me(self, request: Any) -> Any:
        account = self._require(request)
        return self._json(self._me_payload(account))

    # ── sessions ─────────────────────────────────────────────────────────────

    def _backend(self) -> Backend:
        return make_backend(self._cfg, dev=self._dev)

    @staticmethod
    def _clean_setup(body: dict[str, Any]) -> dict[str, Any] | str:
        """The setup form, validated; a string is the refusal."""
        mode = str(body.get("mode", "company"))
        if mode not in prompts.MODES:
            return f"mode must be one of {', '.join(prompts.MODES)}"
        try:
            length = int(body.get("length_min") or 30)
        except (TypeError, ValueError):
            return "length_min must be a number"
        if length not in (15, 30, 45, 60):
            return "length_min must be 15, 30, 45, or 60"

        def text(key: str, limit: int = 600) -> str:
            return str(body.get(key) or "").strip()[:limit]

        setup: dict[str, Any] = {"mode": mode, "length_min": length, "request": text("request")}
        if mode == "case":
            case_type = text("case_type", 20) or "any"
            if case_type not in ("any", "product", "project", "company"):
                return "case_type must be any, product, project, or company"
            source = text("case_source", 20) or "library"
            if source not in ("library", "generated"):
                return "case_source must be library or generated"
            setup.update(case_type=case_type, case_source=source)
        else:
            setup.update(role=text("role", 120), seniority=text("seniority", 60))
            if mode == "technical":
                setup["focus"] = text("focus", 200)
        return setup

    async def _generate_brief(self, setup: dict[str, Any], username: str) -> dict[str, Any]:
        """The brief for a setup: a case from the library when asked and
        available, else one structured call to the model."""
        mode = setup["mode"]
        if mode == "case" and setup.get("case_source") == "library":
            picked = self._pick_case(setup.get("case_type", "any"), username)
            if picked is not None:
                return picked
        system, user, schema = prompts.brief_request(mode, setup)
        backend = self._backend()
        try:
            return await backend.ask_json(system, user, schema, effort="medium")
        finally:
            await backend.close()

    def _pick_case(self, case_type: str, username: str) -> dict[str, Any] | None:
        """A library case not yet seen by this user, if the library exists
        (it arrives in a later phase; until then, generation)."""
        try:
            from ciel.interview.cases import CaseLibrary
        except ImportError:
            return None
        seen = {
            m.get("case_slug") for m in self._store.list(username) if m.get("case_slug")
        }
        return CaseLibrary(self._dir / "cases").pick(case_type, exclude=seen)

    def _session_row(self, meta: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "mode", "title", "created", "started", "ended", "duration_s",
                "question_count", "state", "debriefed", "has_recording")
        return {k: meta.get(k) for k in keys}

    async def _sessions_list(self, request: Any) -> Any:
        account = self._require(request)
        rows = [self._session_row(m) for m in self._store.list(account.username)]
        return self._json({"sessions": rows})

    def _over_daily_cap(self, username: str) -> bool:
        today = time.strftime("%Y-%m-%dT00:00:00", time.localtime())
        return self._store.count_since(username, today) >= self._cfg.daily_sessions_per_user

    async def _sessions_create(self, request: Any) -> Any:
        account = self._require(request)
        setup = self._clean_setup(await self._body(request))
        if isinstance(setup, str):
            return self._fail(400, "bad_request", setup)
        if self._over_daily_cap(account.username):
            return self._fail(429, "daily_cap", "that is enough interviews for today — come back tomorrow")
        try:
            brief = await self._generate_brief(setup, account.username)
        except BackendError as exc:
            log.warning("brief generation failed for %s: %s", account.username, exc)
            return self._fail(502, "brief_failed", "the interviewer could not prepare a brief; try again")
        meta = self._store.create(account.username, setup["mode"], setup, brief)
        log.info("interview %s prepared for %s (%s)", meta["id"], account.username, setup["mode"])
        return self._json({"session": self._session_row(meta), "brief": brief})

    def _owned(self, request: Any) -> tuple[Account, str, dict[str, Any]]:
        from aiohttp import web

        account = self._require(request)
        session_id = str(request.match_info.get("id", ""))
        try:
            meta = self._store.load_meta(account.username, session_id)
        except KeyError:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "no_such_session", "reason": session_id}),
                content_type="application/json",
            )
        return account, session_id, meta

    async def _session_get(self, request: Any) -> Any:
        account, session_id, meta = self._owned(request)
        debrief, debrief_md = self._store.debrief(account.username, session_id)
        meta["debriefed"] = debrief is not None
        meta["has_recording"] = self._store.recording_path(account.username, session_id).is_file()
        payload: dict[str, Any] = {
            "session": self._session_row(meta),
            "setup": meta.get("setup") or {},
            "brief": self._store.load_brief(account.username, session_id),
            "transcript": self._store.transcript(account.username, session_id),
            "code": self._store.code(account.username, session_id),
            "debrief": debrief,
            "debrief_md": debrief_md,
        }
        return self._json(payload)

    async def _session_regenerate(self, request: Any) -> Any:
        account, session_id, meta = self._owned(request)
        if meta.get("state") != "prepared":
            return self._fail(409, "already_started", "this interview has already begun")
        setup = meta.get("setup") or {}
        try:
            brief = await self._generate_brief(setup, account.username)
        except BackendError as exc:
            log.warning("brief regeneration failed for %s: %s", account.username, exc)
            return self._fail(502, "brief_failed", "the interviewer could not prepare a brief; try again")
        from ciel.interview.store import brief_title

        self._store.save_brief(account.username, session_id, brief)
        meta = self._store.update_meta(
            account.username, session_id,
            title=brief_title(setup.get("mode", "company"), brief),
            case_slug=brief.get("slug") if setup.get("mode") == "case" else None,
        )
        return self._json({"session": self._session_row(meta), "brief": brief})

    async def _session_recording(self, request: Any) -> Any:
        from aiohttp import web

        account, session_id, _meta = self._owned(request)
        path = self._store.recording_path(account.username, session_id)
        if not path.is_file():
            raise web.HTTPNotFound
        # FileResponse honours Range, which the page's player needs to seek.
        return web.FileResponse(
            path, headers={"Content-Type": "audio/webm", "Cache-Control": "private, no-store"},
        )

    async def _session_delete(self, request: Any) -> Any:
        account, session_id, meta = self._owned(request)
        if meta.get("state") == "live":
            return self._fail(409, "live", "end the interview first")
        self._store.delete(account.username, session_id)
        return self._json({"ok": True})

    # ── the live socket ──────────────────────────────────────────────────────

    def _origins(self) -> tuple[str, ...]:
        hub = self._config.hub
        return (hub.bind or self._config.web.host,) + tuple(hub.origins)

    async def _serve_ws(self, request: Any) -> Any:
        from aiohttp import web

        from ciel.remote.web import origin_allowed

        if not origin_allowed(request.headers.get("Origin"), self._port, self._origins()):
            log.warning("interview socket refused origin %r", request.headers.get("Origin"))
            raise web.HTTPForbidden
        account = self._user(request)
        if account is None:
            raise web.HTTPUnauthorized
        session_id = str(request.query.get("session", ""))
        try:
            meta = self._store.load_meta(account.username, session_id)
        except KeyError:
            raise web.HTTPNotFound
        key = (account.username, session_id)
        session = self._live.get(key)
        if session is None:
            if meta.get("state") not in ("prepared", "live"):
                raise web.HTTPConflict(text="this interview is over")
            if self.live_count >= self._cfg.max_concurrent:
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.send_str(wire.encode({
                    "type": "error", "code": "room_full",
                    "reason": "the room is full right now — try again in a few minutes",
                }))
                await ws.close()
                return ws
            session = self._open_session(account.username, session_id, meta)

        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        caps: dict[str, Any] = {}
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=self._config.hub.hello_timeout_s)
        except asyncio.TimeoutError:
            await ws.close(code=4408, message=b"hello timeout")
            return ws
        if first.type == web.WSMsgType.TEXT:
            try:
                hello = wire.decode(first.data, "c2h")
            except wire.WireError:
                hello = {"type": "?"}
            if hello.get("type") == "hello":
                caps = dict(hello.get("caps") or {})
            else:
                await ws.close(code=4400, message=b"hello first")
                return ws
        else:
            await ws.close(code=4400, message=b"hello first")
            return ws

        await session.attach(ws, caps)
        log.info("interview %s: %s connected", session_id, account.username)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        frame = wire.decode(msg.data, "c2h")
                    except wire.WireError as exc:
                        log.debug("interview %s: bad frame: %s", session_id, exc)
                        continue
                    session.on_frame(frame)
                elif msg.type == web.WSMsgType.BINARY:
                    try:
                        seq, chunk = wire.unpack_audio(msg.data)
                    except wire.WireError:
                        continue
                    session.on_audio(seq, chunk)
        except Exception:  # noqa: BLE001 - one socket's tantrum stays its own
            log.debug("interview %s: socket errored", session_id, exc_info=True)
        finally:
            session.detach(ws)
            log.info("interview %s: %s disconnected", session_id, account.username)
        return ws

    def _open_session(self, username: str, session_id: str, meta: dict[str, Any]) -> InterviewSession:
        brief = self._store.load_brief(username, session_id)
        session = InterviewSession(
            cfg=self._cfg, store=self._store, username=username, session_id=session_id,
            meta=meta, brief=brief, backend=self._backend(), debrief_backend=self._backend,
            speaker=self._speaker, on_finished=self._session_finished,
        )
        self._live[(username, session_id)] = session
        log.info("interview %s opened for %s (%d live)", session_id, username, len(self._live))
        return session

    def _session_finished(self, session: InterviewSession) -> None:
        self._live.pop((session.username, session.session_id), None)

    # ── admin ────────────────────────────────────────────────────────────────

    @staticmethod
    def _account_row(account: Account) -> dict[str, Any]:
        return {
            "username": account.username,
            "role": account.role,
            "created": account.created,
            "disabled": account.disabled,
            "last_seen": account.last_seen,
        }

    async def _admin_list(self, request: Any) -> Any:
        self._require(request, admin=True)
        rows = [self._account_row(a) for a in self._accounts.list()]
        return self._json({"accounts": rows})

    async def _admin_create(self, request: Any) -> Any:
        admin = self._require(request, admin=True)
        body = await self._body(request)
        username = str(body.get("username", "")).strip().lower()
        role = "admin" if body.get("role") == "admin" else "user"
        chosen = str(body.get("password") or "") or None
        try:
            password = await asyncio.to_thread(self._accounts.create, username, role, chosen)
        except ValueError as exc:
            return self._fail(400, "bad_request", str(exc))
        log.info("interview account %r created by %s", username, admin.username)
        account = self._accounts.get(username)
        assert account is not None
        return self._json({"account": self._account_row(account), "password": password, "chosen": chosen is not None})

    def _target(self, request: Any) -> str:
        return str(request.match_info.get("u", "")).strip().lower()

    async def _admin_reset(self, request: Any) -> Any:
        admin = self._require(request, admin=True)
        username = self._target(request)
        body = await self._body(request)
        chosen = str(body.get("password") or "") or None
        try:
            password = await asyncio.to_thread(self._accounts.reset, username, chosen)
        except KeyError:
            return self._fail(404, "no_such_account", username)
        except ValueError as exc:
            return self._fail(400, "bad_request", str(exc))
        log.info("interview account %r reset by %s", username, admin.username)
        return self._json({"username": username, "password": password, "chosen": chosen is not None})

    async def _change_password(self, request: Any) -> Any:
        """A signed-in user chooses their own password, proving the old one."""
        account = self._require(request)
        body = await self._body(request)
        current = str(body.get("current") or "")
        new = str(body.get("new") or "")
        if await asyncio.to_thread(self._accounts.verify, account.username, current) is None:
            # 403, not 401: the cookie is fine, the proof is not — a 401
            # would read as "signed out" to the page.
            return self._fail(403, "bad_current", "the current password is wrong")
        try:
            await asyncio.to_thread(self._accounts.reset, account.username, new)
        except ValueError as exc:
            return self._fail(400, "bad_request", str(exc))
        log.info("interview account %r changed its own password", account.username)
        return self._json({"ok": True})

    async def _set_disabled(self, request: Any, disabled: bool) -> Any:
        admin = self._require(request, admin=True)
        username = self._target(request)
        if username == admin.username and disabled:
            return self._fail(400, "bad_request", "you cannot disable yourself")
        try:
            account = self._accounts.set_disabled(username, disabled)
        except KeyError:
            return self._fail(404, "no_such_account", username)
        return self._json({"account": self._account_row(account)})

    async def _admin_disable(self, request: Any) -> Any:
        return await self._set_disabled(request, True)

    async def _admin_enable(self, request: Any) -> Any:
        return await self._set_disabled(request, False)

    async def _admin_delete(self, request: Any) -> Any:
        admin = self._require(request, admin=True)
        username = self._target(request)
        if username == admin.username:
            return self._fail(400, "bad_request", "you cannot delete yourself")
        try:
            self._accounts.delete(username)
        except KeyError:
            return self._fail(404, "no_such_account", username)
        log.info("interview account %r deleted by %s", username, admin.username)
        return self._json({"ok": True})


__all__ = ["COOKIE", "InterviewApp", "PREFIX"]
