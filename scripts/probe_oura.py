"""Probe the Oura ring integration — scripted by default, live on request.

    uv run scripts/probe_oura.py                 # scripted checks, no network
    uv run scripts/probe_oura.py --authorize     # the one-time browser approval
    uv run scripts/probe_oura.py --live          # today's summary from the ring
    uv run scripts/probe_oura.py --live --days 7 # the last week

The scripted checks drive the pure shaping (records → sentences), the tool
through a fake client, the readiness nudge, the watcher's lifecycle and
failure streak, the OAuth token store (refresh, rotation, the file), the
local callback of the authorize step, and config — so a regression shows
up here before it shows up as a silent "not connected".

`--authorize` needs an Oura application's client id and secret ([oura] in
~/.ciel/config.toml, or CIEL_OURA_CLIENT_ID / CIEL_OURA_CLIENT_SECRET):
it opens the approval page, catches the redirect on localhost, and writes
~/.ciel/oura.json. `--live` then answers the remaining setup questions:
does Oura accept the authorization? has the ring synced today?
"""

import argparse
import asyncio
import datetime
import json
import logging
import os
import socket
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from ciel.brain.tools import oura as oura_tool
from ciel.brain.tools.oura import label_for, oura_summary, parse_day, summary_for
from ciel.config import Config, OuraConfig, load_config
import base64
import hashlib

from ciel.oura import (
    MAX_RANGE_DAYS,
    REAUTHORIZE,
    TOKEN_URL,
    OAuthTokens,
    OuraClient,
    OuraUnavailable,
    StaticToken,
    authorize,
    authorize_url,
    build_auth,
    by_day,
    exchange_code,
    hours_minutes,
    main_sleep_by_day,
    pkce_pair,
    redirect_uri,
    summarize_day,
    summarize_range,
    weakest_contributor,
)
from ciel.proactive.events import EventQueue
from ciel.proactive.oura import OuraWatcher, activity_nudge, morning_nudge

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


def text_of(result) -> str:
    return result["content"][0]["text"]


# ── fixtures: the shapes Oura actually sends ─────────────────────────────────


def readiness(day: str, score: int = 82, **contributors) -> dict:
    base = {
        "activity_balance": 80, "body_temperature": 95, "hrv_balance": 70,
        "previous_day_activity": 85, "previous_night": 75,
        "recovery_index": 90, "resting_heart_rate": 88, "sleep_balance": 78,
    }
    base.update(contributors)
    return {
        "id": f"r-{day}", "day": day, "score": score,
        "temperature_deviation": -0.12, "temperature_trend_deviation": 0.0,
        "timestamp": f"{day}T00:00:00+01:00", "contributors": base,
    }


def daily_sleep(day: str, score: int = 79) -> dict:
    return {
        "id": f"s-{day}", "day": day, "score": score,
        "contributors": {
            "deep_sleep": 85, "efficiency": 92, "latency": 60, "rem_sleep": 70,
            "restfulness": 55, "timing": 80, "total_sleep": 75,
        },
        "timestamp": f"{day}T00:00:00+01:00",
    }


def activity(day: str, score: int = 68) -> dict:
    return {
        "id": f"a-{day}", "day": day, "score": score, "steps": 8421,
        "active_calories": 412, "total_calories": 2310,
    }


def session(day: str, seconds: int = 25920, kind: str = "long_sleep") -> dict:
    return {
        "id": f"p-{day}-{kind}", "day": day, "type": kind,
        "total_sleep_duration": seconds, "average_hrv": 41,
        "lowest_heart_rate": 48,
        "bedtime_start": f"{day}T23:42:10+01:00",
        "bedtime_end": f"{day}T07:31:00+01:00",
    }


class FakeClient:
    """Serves canned records and remembers the windows it was asked for."""

    def __init__(self, days: list[str], fail: Exception | None = None) -> None:
        self.days = days
        self.fail = fail
        self.calls: list[tuple[str, datetime.date, datetime.date]] = []

    def _serve(self, name, start, end, make):
        self.calls.append((name, start, end))
        if self.fail is not None:
            raise self.fail
        return [make(d) for d in self.days if start.isoformat() <= d <= end.isoformat()]

    def daily_readiness(self, start, end):
        return self._serve("readiness", start, end, readiness)

    def daily_sleep(self, start, end):
        return self._serve("sleep", start, end, daily_sleep)

    def daily_activity(self, start, end):
        return self._serve("activity", start, end, activity)

    def sleep_sessions(self, start, end):
        return self._serve("sessions", start, end, session)


# ── scripted checks ──────────────────────────────────────────────────────────


def shaping_checks() -> None:
    print("shaping:")
    check("hours and minutes at minute precision", hours_minutes(25920) == "7 hours 12 minutes")
    check("a whole hour has no minutes", hours_minutes(3600) == "1 hour")
    check("under an hour is minutes", hours_minutes(1500) == "25 minutes")

    full = summarize_day(readiness("2026-08-21"), daily_sleep("2026-08-21"),
                         activity("2026-08-21"), session("2026-08-21"))
    check("sleep leads with score and hours",
          full.startswith("Sleep score 79, 7 hours 12 minutes asleep"))
    check("bed and wake times are spoken clocks", "in bed 11:42 pm to 7:31 am" in full)
    check("contributors are labelled as scores", "efficiency score 92" in full)
    check("readiness names its weakest contributor",
          "Readiness 82 (weakest contributor HRV balance at 70" in full)
    check("temperature is a signed deviation", "temperature -0.1°C from baseline" in full)
    check("activity carries steps and calories",
          full.endswith("Activity score 68 (8421 steps, 412 active calories)."))

    sleep_only = summarize_day(None, daily_sleep("2026-08-21"), None, None)
    check("a partial day still summarizes", sleep_only.startswith("Sleep score 79;"))
    check("a partial day omits what is missing",
          "Readiness" not in sleep_only and "Activity" not in sleep_only)
    check("an empty day says so",
          summarize_day(None, None, None) == "No Oura data for that day yet — the ring may not have synced.")
    check("a session alone still gives hours",
          summarize_day(None, None, None, session("2026-08-21")).startswith("Sleep, 7 hours 12 minutes asleep"))
    odd = summarize_day({"day": "x", "score": None, "contributors": {"hrv_balance": "n/a"}},
                        {"day": "x", "contributors": None}, {"day": "x", "steps": None},
                        {"day": "x", "total_sleep_duration": "soon", "bedtime_start": "not a time"})
    check("junk shapes never crash the summary", "Sleep score ?" in odd and "Readiness ?" in odd)

    check("by_day indexes on the day field",
          set(by_day([readiness("2026-08-20"), readiness("2026-08-21"), {"no": "day"}, "junk"])) == {"2026-08-20", "2026-08-21"})
    nap = session("2026-08-21", seconds=9000, kind="late_nap")
    night = session("2026-08-21", seconds=20000)
    check("the night beats a nap, whatever the lengths",
          main_sleep_by_day([nap, night])["2026-08-21"] is night)
    check("between two nights the longer wins",
          main_sleep_by_day([session("2026-08-21", 100), night])["2026-08-21"] is night)
    check("the weakest contributor is the minimum",
          weakest_contributor(readiness("d", hrv_balance=40)) == ("HRV balance", 40))
    check("no contributors means no weakest", weakest_contributor({"score": 50}) is None)

    days = [datetime.date(2026, 8, 20), datetime.date(2026, 8, 21)]
    lines = summarize_range(days, [readiness("2026-08-21")], [], [], [session("2026-08-21")])
    check("a range keeps the caller's order", [d for d, _ in lines] == days)
    check("a range marks unsynced days", lines[0][1].startswith("No Oura data"))
    check("a range fills synced days", "7 hours 12 minutes" in lines[1][1])


async def tool_checks() -> None:
    print("the tool:")
    today = datetime.date(2026, 8, 21)
    check("'' and 'today' are today", parse_day("", today) == today and parse_day("Today", today) == today)
    check("'last night' is this morning", parse_day("last night", today) == today)
    check("'yesterday' subtracts a day", parse_day("yesterday", today) == datetime.date(2026, 8, 20))
    check("an ISO date parses", parse_day("2026-08-01", today) == datetime.date(2026, 8, 1))
    check("nonsense is None", parse_day("next tuesday", today) is None)
    check("labels: today, yesterday, then weekday and date",
          (label_for(today, today), label_for(today - datetime.timedelta(days=1), today),
           label_for(datetime.date(2026, 8, 15), today)) == ("Today", "Yesterday", "Saturday 15 August"))

    oura_tool.bind_oura(None)
    check("unbound means not connected",
          text_of(await oura_summary.handler({"day": "today"})) == "The Oura ring is not connected right now.")

    fake = FakeClient(["2026-08-20", "2026-08-21"])
    oura_tool.bind_oura(fake)
    one = await summary_for(fake, today, 1, today)
    check("one day is one labelled line", one.startswith("Today: Sleep score 79, 7 hours 12 minutes"))
    check("all four collections are read for the same window",
          [c[0] for c in fake.calls] == ["readiness", "sleep", "activity", "sessions"]
          and all(c[1] == today and c[2] == today for c in fake.calls))

    fake.calls.clear()
    week = await summary_for(fake, today, 7, today)
    check("a week is seven lines, oldest first",
          week.count("\n") == 6 and week.startswith("Saturday 15 August: No Oura data")
          and week.endswith(one))
    check("the window starts days-1 back", fake.calls[0][1] == datetime.date(2026, 8, 15))

    fake.calls.clear()
    await summary_for(fake, today, 400, today)
    check("the range is capped", (today - fake.calls[0][1]).days == MAX_RANGE_DAYS - 1)
    fake.calls.clear()
    await summary_for(fake, today, 0, today)
    check("zero and negatives mean one day", fake.calls[0][1] == today)

    real_today = datetime.date.today()
    check("an unparsable day is named back",
          text_of(await oura_summary.handler({"day": "next tuesday"})).startswith("Could not parse 'next tuesday'"))
    check("the future is refused",
          text_of(await oura_summary.handler({"day": (real_today + datetime.timedelta(days=1)).isoformat()}))
          == "That day hasn't happened yet.")
    check("a junk days value falls back to one",
          text_of(await oura_summary.handler({"day": "2026-08-21", "days": "lots"})).startswith("Friday 21 August" if real_today != today else "Today"))

    oura_tool.bind_oura(FakeClient([], fail=OuraUnavailable("Oura rejected the token — fake.")))
    logging.disable(logging.WARNING)
    try:
        check("an unavailable ring speaks its reason",
              text_of(await oura_summary.handler({})) == "Oura rejected the token — fake.")
    finally:
        logging.disable(logging.NOTSET)
    oura_tool.bind_oura(None)


async def watcher_checks(tmp: Path) -> None:
    print("the morning nudge:")
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    ids = iter(f"e{i}" for i in range(1, 100))
    nid = lambda: next(ids)  # noqa: E731

    low = morning_nudge([readiness(today, score=55, hrv_balance=30)], [], [], now, 60, 60, nid)
    check("a low readiness nudges", low is not None and low.source == "oura" and low.importance == 2)
    check("the nudge says the score and the mark",
          low.summary.startswith("Oura readiness is 55 today, at or under the 60 mark"))
    check("the nudge names the weakest contributor",
          low.summary.endswith("The weakest contributor is HRV balance, at 30."))
    check("dedupe is by morning", low.dedupe_key == f"oura:morning:{today}")
    local = time.localtime(now)
    midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    check("it expires at local midnight", low.expires_at == midnight)
    check("at the mark counts as low",
          morning_nudge([readiness(today, score=60)], [], [], now, 60, 60, nid) is not None)
    check("a fine morning is silent",
          morning_nudge([readiness(today, score=61)], [daily_sleep(today, 61)], [], now, 60, 60, nid) is None)
    check("a threshold of 0 disables that check",
          morning_nudge([readiness(today, score=10)], [], [], now, 0, 60, nid) is None)
    check("yesterday's low score is not today's",
          morning_nudge([readiness("2000-01-01", score=10)], [], [], now, 60, 60, nid) is None)
    check("no score, no nudge",
          morning_nudge([{"day": today, "score": None}], [], [], now, 60, 60, nid) is None)
    bare = morning_nudge([{"day": today, "score": 40}], [], [], now, 60, 60, nid)
    check("no contributors, no reason clause", bare is not None and bare.summary.endswith("going easy."))

    short = morning_nudge([readiness(today, score=75)], [daily_sleep(today, 52)],
                          [session(today, 20400)], now, 60, 60, nid)
    check("a short night nudges on its own",
          short is not None and short.summary ==
          "Short night by the ring: sleep score 52, 5 hours 40 minutes asleep, under the 60 mark — readiness held up at 75.")
    rough = morning_nudge([readiness(today, score=55, hrv_balance=30)], [daily_sleep(today, 52)],
                          [session(today, 20400)], now, 60, 60, nid)
    check("both low is one rough-night nudge",
          rough is not None and rough.summary.startswith(
              "Rough night by the ring: sleep score 52, 5 hours 40 minutes asleep; readiness 55, under the 60 mark")
          and rough.summary.endswith("The weakest contributor is HRV balance, at 30."))
    check("sleep alone disabled leaves readiness to speak",
          morning_nudge([readiness(today, score=75)], [daily_sleep(today, 52)], [], now, 60, 0, nid) is None)
    no_session = morning_nudge([], [daily_sleep(today, 52)], [], now, 60, 60, nid)
    check("no session means no hours, no readiness means no held-up clause",
          no_session is not None and no_session.summary == "Short night by the ring: sleep score 52, under the 60 mark.")

    print("the activity nudge:")
    minutes_now = local.tm_hour * 60 + local.tm_min
    earlier, later = max(0, minutes_now - 1), min(1439, minutes_now + 1)
    still = activity_nudge([{**activity(today, 35), "steps": 2100}], now, 40, earlier, nid)
    check("a still day past the check hour files a note",
          still is not None and still.importance == 1 and still.dedupe_key == f"oura:activity:{today}"
          and still.summary == "The ring says it's been a still day: activity score 35 so far, 2100 steps — a walk would fix it.")
    check("before the check hour nothing is said",
          activity_nudge([activity(today, 35)], now, 40, later, nid) is None or later == minutes_now)
    check("an active day is silent", activity_nudge([activity(today, 68)], now, 40, earlier, nid) is None)
    check("0 or no hour disables it",
          activity_nudge([activity(today, 5)], now, 0, earlier, nid) is None
          and activity_nudge([activity(today, 5)], now, 40, None, nid) is None)
    check("no steps, no steps clause",
          activity_nudge([{"day": today, "score": 20}], now, 40, earlier, nid).summary
          == "The ring says it's been a still day: activity score 20 so far — a walk would fix it.")

    print("the watcher:")
    queue = EventQueue(tmp / "oura-events.json", held_max_age_s=3600.0)
    config = OuraConfig(enabled=True, token="legacy", token_file=tmp / "none.json",
                        poll_s=0.05, low_readiness=60, low_sleep=60, low_activity=0)
    watcher = OuraWatcher(config, queue)
    check("a legacy token still builds a client", isinstance(watcher._client, OuraClient))
    watcher._client = FakeClient([today])  # score 82 — fine
    await watcher.start()
    await asyncio.sleep(0.12)
    check("a fine morning queues nothing", not queue.pending)
    low_client = FakeClient([today])
    low_client.daily_readiness = lambda s, e: [readiness(today, score=50)]
    watcher._client = low_client
    watcher.kick()
    await asyncio.sleep(0.12)
    check("a low morning queues one nudge", queue.pending and queue.count_source("oura") == 1)
    await asyncio.sleep(0.15)
    check("rescans do not repeat it", queue.count_source("oura") == 1)
    await watcher.close()
    check("close stops the poll", watcher._task is None)

    logging.disable(logging.ERROR)
    try:
        failing = OuraWatcher(config, EventQueue(tmp / "oura-health.json", 3600.0))
        failing._client = FakeClient([], fail=OuraUnavailable("down"))
        await failing.start()
        await asyncio.sleep(0.25)
        await failing.close()
    finally:
        logging.disable(logging.NOTSET)
    check("a failing ring reports the watcher once",
          failing._queue.count_source("vigil") == 1)


class FakePost:
    """Stands in for Oura's token endpoint: records what was posted and
    answers from a script, one response per call."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.posted: list[tuple[str, dict]] = []

    def __call__(self, url: str, fields: dict, basic=None) -> dict:
        self.posted.append((url, dict(fields), basic))
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def granted(access: str, refresh: str | None = "r2", expires_in: int = 2592000) -> dict:
    out = {"token_type": "bearer", "access_token": access, "expires_in": expires_in, "scope": "daily"}
    if refresh is not None:
        out["refresh_token"] = refresh
    return out


def auth_checks(tmp: Path) -> None:
    print("the token store:")
    path = tmp / "oura.json"
    empty = OAuthTokens(path, "cid", "sec", post=FakePost())
    check("no file means not ready", not empty.ready and not path.exists())
    try:
        empty.bearer(now=1000.0)
        check("a bare store refuses to refresh", False)
    except OuraUnavailable as exc:
        check("a bare store refuses to refresh", REAUTHORIZE in str(exc))

    post = FakePost(granted("a1", "r1"))
    store = OAuthTokens(path, "cid", "sec", post=post)
    store.adopt(granted("a0", "r0", expires_in=100), now=1000.0)
    check("adopt writes the file", path.exists() and json.loads(path.read_text())["refresh_token"] == "r0")
    check("the file is owner-only", stat.S_IMODE(path.stat().st_mode) == 0o600)
    check("credentials are saved alongside", json.loads(path.read_text())["client_id"] == "cid")
    check("a fresh token is returned without a refresh",
          OAuthTokens(path, post=post).bearer(now=1000.0 + 100 - 24 * 3600 - 1) == "a0" and not post.posted)
    reborn = OAuthTokens(path, post=post)
    check("a reloaded store is ready with the file's credentials", reborn.ready and reborn.client_secret == "sec")
    check("a stale token refreshes", reborn.bearer(now=1050.0) == "a1")
    url, fields, basic = post.posted[0]
    check("the refresh posts the spent token to the token endpoint",
          url == TOKEN_URL and fields["grant_type"] == "refresh_token"
          and fields["refresh_token"] == "r0" and fields["client_id"] == "cid")
    check("the client authenticates with HTTP Basic, secret out of the body",
          basic == ("cid", "sec") and "client_secret" not in fields)
    check("the rotated refresh token is persisted at once",
          json.loads(path.read_text())["refresh_token"] == "r1")
    check("expiry follows expires_in", json.loads(path.read_text())["expires_at"] == 1050.0 + 2592000)
    check("the refreshed token is then served from memory",
          reborn.bearer(now=2000.0) == "a1" and len(post.posted) == 1)

    keep = FakePost(granted("a2", refresh=None))
    store = OAuthTokens(path, post=keep)
    store.bearer(now=1050.0 + 2592000)
    check("a response without a refresh token keeps the old one",
          json.loads(path.read_text())["refresh_token"] == "r1")

    picky = FakePost(
        OuraUnavailable("Oura's token endpoint answered 401 (invalid_client).", status=401),
        granted("a3", "r3"),
    )
    store = OAuthTokens(path, post=picky)
    check("a 401 to Basic falls back to body credentials",
          store.bearer(now=1e12) == "a3" and len(picky.posted) == 2
          and picky.posted[1][2] is None and picky.posted[1][1]["client_secret"] == "sec"
          and picky.posted[1][1]["refresh_token"] == "r1")
    check("the fallback's grant is persisted", json.loads(path.read_text())["refresh_token"] == "r3")
    twice = FakePost(
        OuraUnavailable("401 (invalid_client)", status=401),
        OuraUnavailable("401 (invalid_client)", status=401),
    )
    try:
        OAuthTokens(path, post=twice).bearer(now=3e12)  # past the fallback's grant
        check("both refused means the authorization is gone", False)
    except OuraUnavailable as exc:
        check("both refused means the authorization is gone", REAUTHORIZE in str(exc))
    dead = FakePost(OuraUnavailable("Oura's token endpoint answered 400 (invalid_grant).", status=400))
    try:
        OAuthTokens(path, post=dead).bearer(now=3e12)
        check("a spent refresh token says how to recover", False)
    except OuraUnavailable as exc:
        check("a spent refresh token says how to recover", REAUTHORIZE in str(exc) and exc.status == 400)
    down = FakePost(OuraUnavailable("Could not reach Oura (boom)."))
    try:
        OAuthTokens(path, post=down).bearer(now=3e12)
        check("a network failure stays a network failure", False)
    except OuraUnavailable as exc:
        check("a network failure stays a network failure", "reach Oura" in str(exc))
    empty_grant = FakePost({"token_type": "bearer"})
    try:
        OAuthTokens(path, post=empty_grant).bearer(now=3e12)
        check("a token response without a token is refused", False)
    except OuraUnavailable:
        check("a token response without a token is refused", True)
    check("the file survived the failures", json.loads(path.read_text())["refresh_token"] == "r3")

    print("auth selection:")
    oauth_cfg = OuraConfig(enabled=True, token_file=path)
    check("a complete file wins", isinstance(build_auth(oauth_cfg), OAuthTokens))
    check("a complete file wins over a legacy token",
          isinstance(build_auth(replace(oauth_cfg, token="pat")), OAuthTokens))
    legacy = OuraConfig(enabled=True, token="pat", token_file=tmp / "absent.json")
    check("a legacy token alone is a static bearer",
          isinstance(build_auth(legacy), StaticToken) and build_auth(legacy).bearer() == "pat")
    check("nothing means None", build_auth(OuraConfig(token_file=tmp / "absent.json")) is None)
    (tmp / "half.json").write_text(json.dumps({"refresh_token": "r", "client_id": "c"}))
    check("a file missing the secret is not ready",
          build_auth(OuraConfig(token_file=tmp / "half.json")) is None)
    check("config fills a file's missing secret",
          isinstance(build_auth(OuraConfig(token_file=tmp / "half.json", client_secret="s")), OAuthTokens))
    (tmp / "corrupt.json").write_text("{not json")
    logging.disable(logging.WARNING)
    try:
        check("a corrupt file is not ready, not a crash",
              build_auth(OuraConfig(token_file=tmp / "corrupt.json", client_id="c", client_secret="s")) is None)
    finally:
        logging.disable(logging.NOTSET)

    print("the authorize step:")
    check("the redirect uri is localhost and the port", redirect_uri(8791) == "http://localhost:8791/callback")
    url = authorize_url("cid", "http://localhost:1/callback", "st")
    check("the approval url is the advertised endpoint with code flow, scope, and state",
          url.startswith("https://api.ouraring.com/v2/oauth/authorize?")
          and "response_type=code" in url and "scope=extapi%3Adaily" in url
          and "state=st" in url and "client_id=cid" in url)
    check("without a challenge the url carries no PKCE", "code_challenge" not in url)
    verifier, challenge = pkce_pair()
    check("the PKCE pair is S256 of the verifier",
          43 <= len(verifier) <= 128 and challenge == base64.urlsafe_b64encode(
              hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode())
    check("a challenge rides in the url as S256",
          f"code_challenge={challenge}" in authorize_url("cid", "http://localhost:1/callback", "st", challenge)
          and "code_challenge_method=S256" in authorize_url("cid", "http://localhost:1/callback", "st", challenge))
    ex = FakePost(granted("a9", "r9"))
    exchange_code("cid", "sec", "the-code", "http://localhost:1/callback", post=ex)
    check("the exchange posts the code, redirect, and client id under Basic auth",
          ex.posted[0][1] == {"grant_type": "authorization_code", "code": "the-code",
                              "redirect_uri": "http://localhost:1/callback", "client_id": "cid"}
          and ex.posted[0][0] == "https://api.ouraring.com/v2/oauth/token"
          and ex.posted[0][2] == ("cid", "sec"))
    ex = FakePost(granted("a9", "r9"))
    exchange_code("cid", "sec", "c", "http://localhost:1/callback", code_verifier="v", post=ex)
    check("a verifier is posted with the code", ex.posted[0][1]["code_verifier"] == "v")
    try:
        authorize(OuraConfig(token_file=tmp / "absent.json"), open_browser=False, post=FakePost())
        check("authorizing without credentials says what is missing", False)
    except OuraUnavailable as exc:
        check("authorizing without credentials says what is missing", "client_id" in str(exc))


async def callback_checks(tmp: Path) -> None:
    """Drive the local callback for real: a listener on a free port, the
    probe playing the browser."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    path = tmp / "authorized.json"
    config = OuraConfig(client_id="cid", client_secret="sec", token_file=path, redirect_port=port)
    post = FakePost(granted("a-new", "r-new"))
    urls: list[str] = []
    flow = asyncio.create_task(asyncio.to_thread(
        authorize, config, open_browser=False, announce=urls.append, state="expected",
        timeout_s=10.0, post=post,
    ))
    for _ in range(50):
        if urls:
            break
        await asyncio.sleep(0.05)
    check("the approval url is announced", urls and "state=expected" in urls[0])

    def get(query: str) -> int:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?{query}", timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except urllib.error.URLError:
            return 0  # nothing listening — what "the listener is gone" expects

    check("a callback with the wrong state is refused", await asyncio.to_thread(get, "code=x&state=forged") == 400)
    check("a callback without a code is refused", await asyncio.to_thread(get, "state=expected") == 400)
    check("the flow keeps waiting after refusals", not flow.done())
    check("the right callback is accepted", await asyncio.to_thread(get, "code=abc&state=expected") == 200)
    tokens = await flow
    check("the code is exchanged", post.posted[0][1]["code"] == "abc"
          and post.posted[0][1]["redirect_uri"] == f"http://localhost:{port}/callback")
    sent_challenge = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(urls[0]).query))["code_challenge"]
    posted_verifier = post.posted[0][1]["code_verifier"]
    check("the real flow's verifier matches the challenge it announced",
          base64.urlsafe_b64encode(hashlib.sha256(posted_verifier.encode()).digest()).rstrip(b"=").decode()
          == sent_challenge)
    check("the token file is written and ready", tokens.ready and json.loads(path.read_text())["refresh_token"] == "r-new")
    check("the listener is gone", await asyncio.to_thread(get, "code=late&state=expected") == 0)

    declined = asyncio.create_task(asyncio.to_thread(
        authorize, config, open_browser=False, announce=lambda _u: None, state="s2",
        timeout_s=10.0, post=FakePost(),
    ))
    await asyncio.sleep(0.2)
    await asyncio.to_thread(get, "error=access_denied&state=s2")
    try:
        await declined
        check("a declined approval is reported", False)
    except OuraUnavailable as exc:
        check("a declined approval is reported", "declined" in str(exc))
    check("a decline leaves the old file alone", json.loads(path.read_text())["refresh_token"] == "r-new")


def config_checks(tmp: Path) -> None:
    print("config:")
    check("off by default", not Config().oura.enabled and not Config().oura.armed)
    check("enabled without an authorization is not armed",
          not OuraConfig(enabled=True, token_file=tmp / "missing.json").armed)
    authorized = tmp / "oura.json"
    authorized.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    check("a complete token file arms it", OuraConfig(enabled=True, token_file=authorized).armed)
    half = tmp / "half.json"
    half.write_text(json.dumps({"client_id": "c", "refresh_token": "r"}))
    check("a file without the secret does not arm it", not OuraConfig(enabled=True, token_file=half).armed)
    check("config can supply the missing secret",
          OuraConfig(enabled=True, token_file=half, client_secret="s").armed)
    check("a legacy token arms it", OuraConfig(enabled=True, token="pat", token_file=tmp / "missing.json").armed)

    toml = tmp / "config.toml"
    toml.write_text("[oura]\nenabled = true\nclient_id = \"abc\"\nlow_readiness = 55\nredirect_port = 9001\n")
    loaded = load_config(toml)
    check("[oura] loads from TOML",
          loaded.oura.enabled and loaded.oura.client_id == "abc"
          and loaded.oura.low_readiness == 55 and loaded.oura.redirect_port == 9001)
    os.environ["CIEL_OURA_CLIENT_SECRET"] = "env-secret"
    os.environ["CIEL_OURA_LOW_READINESS"] = "70"
    try:
        from_env = load_config(toml)
    finally:
        del os.environ["CIEL_OURA_CLIENT_SECRET"], os.environ["CIEL_OURA_LOW_READINESS"]
    check("CIEL_OURA_CLIENT_SECRET comes from the environment", from_env.oura.client_secret == "env-secret")
    check("environment wins over the file", from_env.oura.low_readiness == 70)

    from ciel.brain.permissions import FORBIDDEN_NAMES
    check("oura.json is on the blocklist", "oura.json" in FORBIDDEN_NAMES)
    from ciel.brain.witness import witness_allowed
    check("the tool may run unattended", "mcp__ciel__oura_summary" in witness_allowed(Config()))
    from ciel.brain.prompt import build_system_prompt
    check("the prompt knows the ring only when armed",
          "oura_summary" in build_system_prompt(oura=True)
          and "oura_summary" not in build_system_prompt())


# ── live ─────────────────────────────────────────────────────────────────────


def authorize_live() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config().oura
    print(f"register this redirect URI on the Oura application: {redirect_uri(config.redirect_port)}")
    try:
        tokens = authorize(config, announce=lambda url: print(f"\nopening:\n  {url}\n\nwaiting for Oura…"))
    except OuraUnavailable as exc:
        print(f"failed: {exc}")
        return 1
    print(f"authorized — tokens written to {tokens.path}")
    if not config.enabled:
        print("now set `enabled = true` under [oura] in ~/.ciel/config.toml")
    return 0


async def live(days: int) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config().oura
    auth = build_auth(config)
    if auth is None:
        print(f"no authorization in {config.token_file} — run with --authorize first")
        return 1
    if not config.enabled:
        print("note: [oura] enabled = false — the probe reads anyway; Ciel itself won't")
    if isinstance(auth, StaticToken):
        print("note: using the legacy personal access token — Oura will shut these off; --authorize when you can")

    client = OuraClient(auth)
    today = datetime.date.today()
    try:
        print(await summary_for(client, today, days, today))
        records = await asyncio.to_thread(client.daily_readiness, today, today)
    except OuraUnavailable as exc:
        print(f"unavailable: {exc}")
        return 1
    nudge = morning_nudge(records, [], [], time.time(), config.low_readiness, 0, lambda: "probe")
    print("\nreadiness nudge today:", nudge.summary if nudge else
          f"none (threshold {config.low_readiness})")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize", action="store_true", help="the one-time browser approval")
    parser.add_argument("--live", action="store_true", help="read the ring for real")
    parser.add_argument("--days", type=int, default=1, help="with --live: days back (max 14)")
    args = parser.parse_args()

    if args.authorize:
        return authorize_live()
    if args.live:
        return await live(args.days)

    shaping_checks()
    await tool_checks()
    with tempfile.TemporaryDirectory() as tmp:
        await watcher_checks(Path(tmp))
        auth_checks(Path(tmp))
        await callback_checks(Path(tmp))
        config_checks(Path(tmp))
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
