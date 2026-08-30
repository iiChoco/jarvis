"""The Oura Ring client — readiness, sleep, and activity from the source.

Oura Cloud API v2 over OAuth2. Personal access tokens were the simple
path — one string, one header — but Oura stopped issuing them in December
2025 and has said the issued ones will be shut off, so the durable path is
an OAuth application of your own: a client id and secret from
developer.ouraring.com/applications, one browser approval, and a token
file Ciel keeps fresh from then on. A legacy token still works here for
as long as Oura honours it; the OAuth tokens win when both exist.

The endpoints are the ones the API host itself advertises
(``api.ouraring.com/.well-known/oauth-authorization-server``), not the
legacy pair in the older cloud.ouraring.com docs: applications from the
new developer portal are registered with this server only, and the legacy
approval page will happily mint a code the legacy token endpoint then
rejects as ``invalid_client`` — observed, and the reason this module
names its sources. Scopes are prefixed here (``extapi:daily``), and the
server supports PKCE, which a localhost callback should use: the code
travels through a browser redirect anyone on the machine could observe,
and the verifier is what makes it worthless to them.

Two things about Oura's OAuth shape decide the design. Access tokens live
about thirty days, so refreshes are rare and happen lazily on the next
use rather than on a timer. Refresh tokens are *single-use* — every
refresh hands back a new one and retires the old — so whoever refreshes
must persist what it gets, immediately and atomically, or the next
refresh fails and the ring is gone until someone re-authorizes. That is
why, unlike the Google Calendar watcher (which borrows a connector's
tokens and never writes), this module owns its token file outright.

Everything is synchronous stdlib urllib on purpose: callers (the brain
tool, the Vigil watcher) wrap calls in ``asyncio.to_thread``, so the event
loop never waits on Oura's servers. Read-only by construction — the only
data endpoints touched are the ``usercollection`` GETs.

Four collections, not three: the three *daily* summaries carry scores, but
the number a person actually wants from "how did I sleep" is hours, and
that lives on the sleep *sessions* (``total_sleep_duration``). The
summaries here speak both — score for the ring's verdict, duration for the
fact behind it.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import http.server
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from ciel.config import OuraConfig

log = logging.getLogger(__name__)

_API = "https://api.ouraring.com/v2/usercollection"
AUTHORIZE_URL = "https://api.ouraring.com/v2/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/v2/oauth/token"
SCOPES = ("extapi:daily",)
"""``extapi:daily`` covers the three summaries and the sleep sessions — all
this module reads. Nothing personal (height, weight, email) is ever
requested, so the approval screen asks for exactly what the tool uses."""

_HTTP_TIMEOUT_S = 15.0
_REFRESH_AHEAD_S = 24 * 3600.0
"""Refresh this early. A month-long token refreshed a day before expiry
costs one extra refresh a month and means a laptop shut for a weekend
never wakes to a dead token mid-request."""
_DEFAULT_EXPIRES_IN_S = 30 * 24 * 3600.0
"""Oura's documented access-token lifetime, used only if a token response
arrives without ``expires_in``."""

REAUTHORIZE = "run `uv run scripts/probe_oura.py --authorize` to connect the ring again"

MAX_RANGE_DAYS = 14
"""Widest window a single summary covers. Two weeks is "how has my sleep
been lately"; anything wider is a chart, not a spoken answer — and keeps
every request under Oura's page size, so no pagination is needed."""


class OuraUnavailable(RuntimeError):
    """The API could not answer — lost authorization, network down, rate
    limited. One exception type so callers degrade the same way
    everywhere: the tool turns it into a spoken sentence, the watcher into
    a health-streak failure. The message is human-shaped for exactly that
    reason; ``status`` carries the HTTP code when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ── the token endpoint ───────────────────────────────────────────────────────

PostForm = Callable[..., dict[str, Any]]
"""``post(url, fields, basic=(client_id, client_secret) | None)``."""


def post_form(
    url: str, fields: dict[str, str], basic: tuple[str, str] | None = None
) -> dict[str, Any]:
    """One form POST → decoded JSON, every failure an OuraUnavailable
    carrying Oura's own ``error`` when it sent one. ``basic`` adds HTTP
    Basic client authentication. Injectable (the probe passes a fake)
    because the token endpoint is the one place this module talks to that
    a scripted check cannot reach."""
    body = urllib.parse.urlencode(fields).encode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if basic is not None:
        pair = f"{basic[0]}:{basic[1]}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("error_description") or payload.get("error") or "")
        except (OSError, ValueError, AttributeError):
            pass
        raise OuraUnavailable(
            f"Oura's token endpoint answered {exc.code}" + (f" ({detail})" if detail else "") + ".",
            status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OuraUnavailable(f"Could not reach Oura ({exc}).") from exc


def token_request(
    fields: dict[str, str], client_id: str, client_secret: str, post: PostForm
) -> dict[str, Any]:
    """A call to the token endpoint with the client authenticated.

    The server's metadata lists both ``client_secret_basic`` and
    ``client_secret_post``. Basic goes first — what RFC 6749 prefers, and
    it keeps the secret out of any request log — with the body form as
    the fallback on exactly a 401, so a change of taste at Oura's end
    degrades to one extra round trip rather than a dead ring.
    ``client_id`` rides in the body both times; servers tolerate it
    alongside Basic and some insist on it.
    """
    with_id = {**fields, "client_id": client_id}
    try:
        return post(TOKEN_URL, with_id, basic=(client_id, client_secret))
    except OuraUnavailable as exc:
        if exc.status != 401:
            raise
        log.info("oura token endpoint refused Basic auth — retrying with body credentials")
    return post(TOKEN_URL, {**with_id, "client_secret": client_secret})


class Auth(Protocol):
    """Whatever can produce a bearer token for the next request."""

    def bearer(self) -> str: ...


class StaticToken:
    """A legacy personal access token — no refresh, no file, works until
    Oura switches it off."""

    def __init__(self, token: str) -> None:
        self._token = token

    def bearer(self) -> str:
        return self._token


class OAuthTokens:
    """The OAuth authorization for one account, persisted — and owned.

    The file holds the client credentials alongside the tokens, so the
    runtime needs nothing from config but the path: the authorize step
    writes everything the refresh will need. Written atomically and
    owner-only, because a refresh token is a standing key to a year of
    someone's nights. The lock serializes refreshes across the tool's and
    the watcher's threads: with single-use refresh tokens, two concurrent
    refreshes are not merely wasteful — the loser's token is already dead.
    """

    def __init__(
        self,
        path: Path,
        client_id: str = "",
        client_secret: str = "",
        *,
        post: PostForm = post_form,
    ) -> None:
        self._path = path
        self._post = post
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                self._data = loaded
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            log.warning("could not read %s — the ring will need re-authorizing", path)
        # Config credentials fill gaps, never override: the file is what
        # was actually authorized.
        self._data.setdefault("client_id", client_id)
        self._data.setdefault("client_secret", client_secret)
        if not self._data["client_id"]:
            self._data["client_id"] = client_id
        if not self._data["client_secret"]:
            self._data["client_secret"] = client_secret

    @property
    def path(self) -> Path:
        return self._path

    @property
    def client_id(self) -> str:
        return str(self._data.get("client_id") or "")

    @property
    def client_secret(self) -> str:
        return str(self._data.get("client_secret") or "")

    @property
    def refresh_token(self) -> str:
        return str(self._data.get("refresh_token") or "")

    @property
    def expires_at(self) -> float:
        value = self._data.get("expires_at")
        return float(value) if isinstance(value, (int, float)) else 0.0

    @property
    def ready(self) -> bool:
        """Holds everything a refresh needs — the armed state."""
        return bool(self.refresh_token and self.client_id and self.client_secret)

    def bearer(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self._lock:
            access = str(self._data.get("access_token") or "")
            if access and now < self.expires_at - _REFRESH_AHEAD_S:
                return access
            self._refresh(now)
            return str(self._data["access_token"])

    def _refresh(self, now: float) -> None:
        if not self.ready:
            raise OuraUnavailable(f"The ring is not authorized — {REAUTHORIZE}.")
        try:
            granted = token_request(
                {"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                self.client_id, self.client_secret, self._post,
            )
        except OuraUnavailable as exc:
            if exc.status in (400, 401):
                # The refresh token is spent or revoked — the one failure
                # that never heals on its own, so say what does heal it.
                raise OuraUnavailable(
                    f"Oura no longer accepts the saved authorization — {REAUTHORIZE}.",
                    status=exc.status,
                ) from exc
            raise
        self._adopt(granted, now)
        log.info("oura access token refreshed")

    def adopt(self, granted: dict[str, Any], now: float | None = None) -> None:
        """Take a token response (from the code exchange or a refresh) as
        the new truth and persist it."""
        with self._lock:
            self._adopt(granted, time.time() if now is None else now)

    def _adopt(self, granted: dict[str, Any], now: float) -> None:
        access = granted.get("access_token")
        if not isinstance(access, str) or not access:
            raise OuraUnavailable("Oura's token response carried no access token.")
        self._data["access_token"] = access
        # A response without a new refresh token keeps the old one — the
        # spec allows it, and dropping ours would strand the account.
        refresh = granted.get("refresh_token")
        if isinstance(refresh, str) and refresh:
            self._data["refresh_token"] = refresh
        expires_in = granted.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, (int, float)) else _DEFAULT_EXPIRES_IN_S
        self._data["expires_at"] = now + lifetime
        if isinstance(granted.get("scope"), str):
            self._data["scope"] = granted["scope"]
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)


def build_auth(config: "OuraConfig") -> Auth | None:
    """The credential the tool and the watcher share: the OAuth file when
    it is complete, the legacy token when that is all there is, None when
    nothing is — which is what ``OuraConfig.armed`` reports."""
    tokens = OAuthTokens(config.token_file, config.client_id, config.client_secret)
    if tokens.ready:
        return tokens
    if config.token:
        return StaticToken(config.token)
    return None


# ── the one-time authorization ───────────────────────────────────────────────


def redirect_uri(port: int) -> str:
    """What to register on the Oura application — it must match exactly."""
    return f"http://localhost:{port}/callback"


def pkce_pair() -> tuple[str, str]:
    """A fresh ``(code_verifier, code_challenge)`` — S256, per RFC 7636."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, inside the 43–128 window
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(
    client_id: str, redirect: str, state: str, code_challenge: str | None = None
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": " ".join(SCOPES),
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect: str,
    *,
    code_verifier: str | None = None,
    post: PostForm = post_form,
) -> dict[str, Any]:
    fields = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect}
    if code_verifier:
        fields["code_verifier"] = code_verifier
    return token_request(fields, client_id, client_secret, post)


def authorize(
    config: "OuraConfig",
    *,
    open_browser: bool = True,
    announce: Callable[[str], None] = print,
    state: str | None = None,
    timeout_s: float = 300.0,
    post: PostForm = post_form,
) -> OAuthTokens:
    """The browser dance, once: listen on localhost, send the user to
    Oura's approval page, catch the code on the way back, exchange it, and
    write the token file. Binds the port only for the duration of this
    call — nothing in the running assistant ever listens.

    Credentials come from config, or from a token file a previous run
    wrote (so re-authorizing after a revoked refresh token needs nothing
    typed). ``state`` is the CSRF check: a callback carrying any other
    value is answered 400 and ignored, and the wait continues.
    """
    tokens = OAuthTokens(config.token_file, config.client_id, config.client_secret, post=post)
    if not (tokens.client_id and tokens.client_secret):
        raise OuraUnavailable(
            "an Oura application's client_id and client_secret are needed first — "
            "see the README's ring section."
        )
    redirect = redirect_uri(config.redirect_port)
    expected = state or secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()
    received: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
            parsed = urllib.parse.urlparse(self.path)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            if parsed.path != "/callback":
                self._reply(404, "Not the callback.")
                return
            if query.get("state") != expected:
                self._reply(400, "State mismatch — that link was not this run's. Ignored.")
                return
            if "error" in query:
                received["error"] = query.get("error_description") or query["error"]
                self._reply(200, "Oura declined. You can close this tab.")
                return
            if not query.get("code"):
                self._reply(400, "No code in the callback.")
                return
            received["code"] = query["code"]
            self._reply(200, "Ciel is connected to your ring. You can close this tab.")

        def _reply(self, status: int, text: str) -> None:
            body = f"<!doctype html><title>Ciel</title><p>{text}</p>".encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug("oura callback: " + format, *args)

    server = http.server.HTTPServer(("127.0.0.1", config.redirect_port), Callback)
    server.timeout = 0.5
    try:
        url = authorize_url(tokens.client_id, redirect, expected, challenge)
        announce(url)
        if open_browser:
            webbrowser.open(url)
        deadline = time.monotonic() + timeout_s
        while not received and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in received:
        raise OuraUnavailable(f"Oura declined the authorization ({received['error']}).")
    if "code" not in received:
        raise OuraUnavailable("No callback arrived before the timeout — nothing was changed.")
    granted = exchange_code(
        tokens.client_id, tokens.client_secret, received["code"], redirect,
        code_verifier=verifier, post=post,
    )
    tokens.adopt(granted)
    return tokens


# ── the data ─────────────────────────────────────────────────────────────────


class OuraClient:
    """Thin fetcher over the daily summary collections and sleep sessions.

    Each method returns the raw ``data`` list from Oura's envelope — one
    dict per day (or per sleep session), oldest first — because the
    records are already flat and a wrapper class would just rename Oura's
    own field names. Dates are inclusive on both ends, as Oura's are.
    """

    def __init__(self, auth: Auth) -> None:
        self._auth = auth

    def daily_readiness(self, start: datetime.date, end: datetime.date) -> list[dict[str, Any]]:
        return self._collection("daily_readiness", start, end)

    def daily_sleep(self, start: datetime.date, end: datetime.date) -> list[dict[str, Any]]:
        return self._collection("daily_sleep", start, end)

    def daily_activity(self, start: datetime.date, end: datetime.date) -> list[dict[str, Any]]:
        return self._collection("daily_activity", start, end)

    def sleep_sessions(self, start: datetime.date, end: datetime.date) -> list[dict[str, Any]]:
        """Individual sleep periods — the long night plus any naps — each
        stamped with the ``day`` it ended on, which is how Oura files a
        night under the morning after."""
        return self._collection("sleep", start, end)

    def _collection(
        self, name: str, start: datetime.date, end: datetime.date
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        })
        request = urllib.request.Request(
            f"{_API}/{name}?{query}",
            headers={"Authorization": f"Bearer {self._auth.bearer()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The two failures the user can actually fix name their remedy;
            # everything else is transient until proven otherwise.
            if exc.code == 401:
                raise OuraUnavailable(
                    f"Oura rejected the authorization — {REAUTHORIZE}.", status=401
                ) from exc
            if exc.code == 403:
                raise OuraUnavailable(
                    f"Oura refused {name} — the authorization lacks the daily scope, "
                    f"or the membership has lapsed; {REAUTHORIZE}.",
                    status=403,
                ) from exc
            if exc.code == 429:
                raise OuraUnavailable("Oura is rate limiting — try again in a bit.", status=429) from exc
            raise OuraUnavailable(f"Oura answered {exc.code} for {name}.", status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OuraUnavailable(f"Could not reach Oura ({exc}).") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else []


# ── pure shaping, so the probe can drive every shape Oura sends ──────────────

_READINESS_CONTRIBUTORS = {
    "previous_night": "last night's sleep",
    "sleep_balance": "sleep balance",
    "previous_day_activity": "yesterday's activity",
    "activity_balance": "activity balance",
    "body_temperature": "body temperature",
    "resting_heart_rate": "resting heart rate",
    "hrv_balance": "HRV balance",
    "recovery_index": "recovery index",
}
"""Oura's readiness contributors, in the order they appear in the app, with
the words a person would use for them. Each is itself a 0–100 score."""


def by_day(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Daily records indexed by their ``day`` (ISO date). Last one wins,
    which is also newest — Oura lists oldest first."""
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        day = record.get("day") if isinstance(record, dict) else None
        if isinstance(day, str):
            indexed[day] = record
    return indexed


def main_sleep_by_day(sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The night's main sleep per day: the longest session, preferring
    Oura's own ``long_sleep`` type so a late nap never stands in for the
    night it followed."""
    chosen: dict[str, dict[str, Any]] = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        day = session.get("day")
        if not isinstance(day, str):
            continue
        current = chosen.get(day)
        if current is None or _session_rank(session) > _session_rank(current):
            chosen[day] = session
    return chosen


def _session_rank(session: dict[str, Any]) -> tuple[int, float]:
    duration = session.get("total_sleep_duration")
    seconds = float(duration) if isinstance(duration, (int, float)) else 0.0
    return (1 if session.get("type") == "long_sleep" else 0, seconds)


def weakest_contributor(readiness: dict[str, Any]) -> tuple[str, int] | None:
    """The readiness contributor dragging the score down most, as
    ``(spoken name, score)`` — what a nudge names so "readiness is low"
    comes with a reason. None when Oura sent no contributor scores."""
    contributors = readiness.get("contributors")
    if not isinstance(contributors, dict):
        return None
    scored = [
        (label, int(contributors[key]))
        for key, label in _READINESS_CONTRIBUTORS.items()
        if isinstance(contributors.get(key), (int, float))
    ]
    if not scored:
        return None
    return min(scored, key=lambda pair: pair[1])


def summarize_day(
    readiness: dict[str, Any] | None,
    sleep: dict[str, Any] | None,
    activity: dict[str, Any] | None,
    session: dict[str, Any] | None = None,
) -> str:
    """One day's records → compact prose the model can speak from.

    Pure, so a probe can drive every shape Oura sends without a network.
    Missing collections are simply omitted — a ring that hasn't synced its
    activity yet still yields a useful sleep line — and an all-empty day
    says so instead of returning an empty string a model might misread as
    "no tool output". Contributor numbers are labelled as scores, because
    "duration 85" read as minutes would be a very short night.
    """
    parts: list[str] = []
    if sleep or session:
        line = "Sleep"
        if sleep:
            line += f" score {_score(sleep)}"
        if session:
            duration = session.get("total_sleep_duration")
            if isinstance(duration, (int, float)) and duration > 0:
                line += f", {hours_minutes(float(duration))} asleep"
            bedtime = _clock(session.get("bedtime_start"))
            waketime = _clock(session.get("bedtime_end"))
            if bedtime and waketime:
                line += f", in bed {bedtime} to {waketime}"
            hrv = session.get("average_hrv")
            low_hr = session.get("lowest_heart_rate")
            vitals = [
                text for text in (
                    f"average HRV {hrv}" if isinstance(hrv, (int, float)) else None,
                    f"lowest heart rate {low_hr}" if isinstance(low_hr, (int, float)) else None,
                ) if text
            ]
            if vitals:
                line += f" ({', '.join(vitals)})"
        contributors = (sleep or {}).get("contributors") or {}
        extras = [
            f"{label} score {contributors[key]}"
            for key, label in (
                ("efficiency", "efficiency"),
                ("restfulness", "restfulness"),
                ("deep_sleep", "deep sleep"),
                ("rem_sleep", "REM"),
            )
            if isinstance(contributors.get(key), (int, float))
        ]
        if extras:
            line += f"; {', '.join(extras)}"
        parts.append(line + ".")
    if readiness:
        line = f"Readiness {_score(readiness)}"
        details: list[str] = []
        weakest = weakest_contributor(readiness)
        if weakest is not None:
            details.append(f"weakest contributor {weakest[0]} at {weakest[1]}")
        temp = readiness.get("temperature_deviation")
        if isinstance(temp, (int, float)):
            details.append(f"temperature {temp:+.1f}°C from baseline")
        if details:
            line += f" ({', '.join(details)})"
        parts.append(line + ".")
    if activity:
        line = f"Activity score {_score(activity)}"
        steps = activity.get("steps")
        calories = activity.get("active_calories")
        extras = [
            text for text in (
                f"{steps} steps" if isinstance(steps, (int, float)) else None,
                f"{calories} active calories" if isinstance(calories, (int, float)) else None,
            ) if text
        ]
        if extras:
            line += f" ({', '.join(extras)})"
        parts.append(line + ".")
    return " ".join(parts) if parts else (
        "No Oura data for that day yet — the ring may not have synced."
    )


def summarize_range(
    days: list[datetime.date],
    readiness: list[dict[str, Any]],
    sleep: list[dict[str, Any]],
    activity: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> list[tuple[datetime.date, str]]:
    """Per-day summaries for a window, in the order ``days`` is given."""
    readiness_by, sleep_by, activity_by = by_day(readiness), by_day(sleep), by_day(activity)
    main_by = main_sleep_by_day(sessions)
    return [
        (
            day,
            summarize_day(
                readiness_by.get(day.isoformat()),
                sleep_by.get(day.isoformat()),
                activity_by.get(day.isoformat()),
                main_by.get(day.isoformat()),
            ),
        )
        for day in days
    ]


def hours_minutes(seconds: float) -> str:
    """``25920`` → "7 hours 12 minutes". Minute precision on purpose —
    timers' half-hour rounding would round a short night into a fine one."""
    hours, minutes = divmod(int(round(seconds / 60)), 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not parts:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts)


def _score(record: dict[str, Any]) -> str:
    """A record's score as text, "?" when absent *or* null — Oura sends
    ``"score": null`` for a day it hasn't finished computing."""
    score = record.get("score")
    return str(int(score)) if isinstance(score, (int, float)) else "?"


def _clock(stamp: Any) -> str | None:
    """An Oura timestamp (RFC3339 with offset) → "11:42 pm", in the offset
    Oura recorded — the ring's notion of local time, which is the right
    one for "when did I go to bed"."""
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}"


__all__ = [
    "AUTHORIZE_URL",
    "MAX_RANGE_DAYS",
    "OAuthTokens",
    "OuraClient",
    "OuraUnavailable",
    "REAUTHORIZE",
    "SCOPES",
    "StaticToken",
    "TOKEN_URL",
    "authorize",
    "authorize_url",
    "build_auth",
    "by_day",
    "exchange_code",
    "hours_minutes",
    "main_sleep_by_day",
    "pkce_pair",
    "post_form",
    "redirect_uri",
    "summarize_day",
    "summarize_range",
    "token_request",
    "weakest_contributor",
]
