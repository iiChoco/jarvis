"""Probe the section-signup watcher — scripted by default, live on request.

    uv run scripts/probe_sections.py          # scripted checks, no network
    uv run scripts/probe_sections.py --live   # list the site's sections for real

The scripted checks drive the payload parsing (raw refresh_state answers →
Sections, including the signed-out shape that must raise toward a fresh
cookie), the phrasing, the transition logic (first scans, openings, flaps,
enrollment, the hour-bucketed dedupe), the watcher's lifecycle and failure
streak, and config — so a regression shows up here before it shows up as a
missed spot.

`--live` needs the browser cookie in ``[sections]`` of ~/.ciel/config.toml
(or CIEL_SECTIONS_COOKIE): sign in to the site, copy the ``Cookie`` request
header from any ``/api/`` call in DevTools' Network tab. It then lists
every section with its id and open spots — the ids are what ``watch``
wants — and marks the watched and enrolled ones.
"""

import argparse
import asyncio
import json
import logging
import os
import smtplib
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from ciel.brain.prompt import build_system_prompt, sections_watch_line
from ciel.config import MailConfig, SectionsConfig, load_config
from ciel.gmail import GmailSender
from ciel import mail as mailmod
from ciel.mail import MailUnavailable, SmtpSender
from ciel.proactive import sections as watching
from ciel.proactive.events import EventQueue
from ciel.proactive.sections import SectionsWatcher, alarm_message, opening_events
from ciel.sections import (
    SectionsUnavailable,
    describe,
    fetch_state,
    parse_state,
    when_phrase,
)

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


# A Tuesday 10:00 local, so the phrasing checks are deterministic in any
# timezone the probe runs in.
ANCHOR = time.mktime((2026, 9, 1, 10, 0, 0, 0, 0, -1))


def section(sid, kind="Discussion", capacity=4, taken=4, **kw):
    return {
        "id": sid,
        "name": kind,
        "capacity": capacity,
        "students": [{"id": i} for i in range(taken)],
        "location": kw.get("location", "Soda 320"),
        "description": kw.get("description", ""),
        "tags": kw.get("tags", []),
        "staff": kw.get("staff", {"name": "Erin", "email": "erin@berkeley.edu"}),
        "startTime": kw.get("start", ANCHOR),
        "endTime": kw.get("end", ANCHOR + 3600),
    }


def payload(sections, enrolled=None, signed_in=True, course="CS 61B"):
    return {
        "success": True,
        "data": {
            "course": course,
            "currentUser": {"id": 7} if signed_in else None,
            "enrolledSections": enrolled,
            "sections": sections,
        },
    }


def parsing_checks() -> None:
    print("parsing:")
    state = parse_state(payload(
        [section(12, taken=3), section(31, kind="Lab", capacity=6, taken=6, tags=["Bridge"])],
        enrolled=[{"id": 31}],
    ))
    check("the roster parses", len(state.sections) == 2 and state.course == "CS 61B")
    disc, lab = state.sections
    check("spots is capacity minus the roster",
          disc.spots == 1 and disc.kind == "Discussion" and lab.spots == 0)
    check("ids are strings both ways", disc.id == "12" and state.enrolled_ids == {"31"})
    check("tags and staff come along", lab.tags == ("Bridge",) and disc.staff == "Erin")

    over = parse_state(payload([section(1, capacity=2, taken=5)])).sections[0]
    check("an overfull section floors at zero spots", over.spots == 0)

    logging.disable(logging.WARNING)
    try:
        kept = parse_state(payload([{"nonsense": True}, section(2, taken=1)]))
    finally:
        logging.disable(logging.NOTSET)
    check("a broken row is skipped, not fatal",
          [s.id for s in kept.sections] == ["2"])

    bare = parse_state(payload([{"id": 3, "capacity": 4}]))
    check("a minimal row still parses",
          bare.sections[0].spots == 4 and bare.sections[0].staff == ""
          and when_phrase(bare.sections[0]) == "time unlisted")

    try:
        parse_state(payload([], signed_in=False))
        check("a signed-out answer raises toward re-login", False)
    except SectionsUnavailable as exc:
        check("a signed-out answer raises toward re-login", "signed out" in str(exc))
    try:
        parse_state({"success": False, "message": "nope"})
        check("an unsuccessful answer raises", False)
    except SectionsUnavailable:
        check("an unsuccessful answer raises", True)

    check("the weekly slot reads as spoken English",
          when_phrase(disc) == "Tuesdays 10:00 AM to 11:00 AM")
    check("the listing line carries everything",
          describe(disc)
          == "#12  Discussion  Tuesdays 10:00 AM to 11:00 AM  Soda 320 with Erin  — 1/4 spots open")


def transition_checks() -> None:
    print("the transition logic:")
    now = time.time()
    hour = time.strftime("%Y-%m-%d-%H", time.localtime(now))
    ids = iter(f"e{i}" for i in range(1, 100))
    nid = lambda: next(ids)  # noqa: E731
    watch = ("12",)

    full = parse_state(payload([section(12), section(99, taken=0)]))
    events, spots = opening_events(full, watch, None, now, nid)
    check("a full section on the first scan is silent",
          events == [] and spots == {"12": 0})
    check("an unwatched open section says nothing", "99" not in spots)

    open_ = parse_state(payload([section(12, taken=3)]))
    events, spots = opening_events(open_, watch, {"12": 0}, now, nid)
    check("full to open fires one urgent event",
          len(events) == 1 and events[0].importance == 3
          and events[0].source == "sections")
    check("the nudge is a spoken sentence",
          events[0].summary
          == "A spot just opened in CS 61B discussion section 12 — "
             "Tuesdays 10:00 AM to 11:00 AM at Soda 320 with Erin. "
             "Grab it on the sections site before it fills.")
    check("dedupe is by section and hour",
          events[0].dedupe_key == f"sections:12:{hour}")
    check("it expires before it can lie",
          events[0].expires_at == now + 30 * 60.0)
    check("the payload names the section",
          events[0].payload == {"section_id": "12", "spots": "1"})

    still, _ = opening_events(open_, watch, {"12": 1}, now, nid)
    check("still open is silent", still == [])
    wider = parse_state(payload([section(12, taken=1)]))
    grew, _ = opening_events(wider, watch, {"12": 1}, now, nid)
    check("more spots while already open is still silent", grew == [])

    reopened, _ = opening_events(open_, watch, {"12": 0}, now, nid)
    check("refill then reopen fires again", len(reopened) == 1)
    plural, _ = opening_events(wider, watch, {"12": 0}, now, nid)
    check("plural spots read as a count",
          plural[0].summary.startswith("3 spots are open in CS 61B discussion section 12"))

    first_open, _ = opening_events(open_, watch, None, now, nid)
    check("already open on the first scan is the news the watch wanted",
          len(first_open) == 1)

    enrolled = parse_state(payload([section(12, taken=3)], enrolled=[{"id": 12}]))
    quiet, _ = opening_events(enrolled, watch, {"12": 0}, now, nid)
    check("a section you are already in is old news", quiet == [])

    logging.disable(logging.WARNING)
    try:
        _, seen = opening_events(open_, ("12", "404"), None, now, nid)
    finally:
        logging.disable(logging.NOTSET)
    check("a watched id the site doesn't list is logged, not fatal",
          seen == {"12": 1})


async def watcher_checks(tmp: Path) -> None:
    print("the watcher:")
    config = SectionsConfig(
        enabled=True, cookie="session=probe", watch=("12",), poll_s=0.05,
        cookie_file=tmp / "no-such-cookie-file",
    )
    queue = EventQueue(tmp / "sections-events.json", held_max_age_s=3600.0)
    real_fetch = watching.fetch_state

    logging.disable(logging.WARNING)
    try:
        idle = SectionsWatcher(replace(config, cookie=""), queue)
        await idle.start()
        check("no cookie idles instead of failing forever", idle._task is None)
        idle2 = SectionsWatcher(replace(config, watch=()), queue)
        await idle2.start()
        check("no watch list idles too", idle2._task is None)
    finally:
        logging.disable(logging.NOTSET)

    answers = [payload([section(12)])]
    watching.fetch_state = lambda url, cookie: parse_state(answers[-1])
    try:
        watcher = SectionsWatcher(config, queue)
        await watcher.start()
        await asyncio.sleep(0.12)
        check("a full section queues nothing", not queue.pending)
        answers.append(payload([section(12, taken=3)]))
        watcher.kick()
        await asyncio.sleep(0.12)
        check("an opening queues one nudge",
              queue.pending and queue.count_source("sections") == 1)
        await asyncio.sleep(0.15)
        check("rescans do not repeat it", queue.count_source("sections") == 1)
        await watcher.close()
        check("close stops the poll", watcher._task is None)

        logging.disable(logging.ERROR)
        try:
            def down(url, cookie):
                raise SectionsUnavailable("down")
            watching.fetch_state = down
            failing = SectionsWatcher(config, EventQueue(tmp / "sections-health.json", 3600.0))
            await failing.start()
            await asyncio.sleep(0.25)
            await failing.close()
        finally:
            logging.disable(logging.NOTSET)
        check("a dead cookie reports the watcher once",
              failing._queue.count_source("vigil") == 1)
    finally:
        watching.fetch_state = real_fetch


async def selfheal_checks(tmp: Path) -> None:
    print("self-heal:")
    cookie_file = tmp / "live-cookie"
    ran = tmp / "refresh-ran"
    # A stand-in refresh command: writes a fresh cookie and records each run,
    # so the test can see it fired and that the cooldown fired it only once.
    refresh = ["sh", "-c", f"printf session=fresh > '{cookie_file}'; printf x >> '{ran}'"]
    config = SectionsConfig(
        enabled=True, cookie="session=stale", watch=("12",), poll_s=0.05,
        cookie_file=cookie_file, auto_refresh=True, refresh_cmd=tuple(refresh),
        refresh_cooldown_s=3600.0,
    )
    queue = EventQueue(tmp / "selfheal-events.json", 3600.0)
    real_fetch = watching.fetch_state

    def signed_out(url, cookie):
        raise SectionsUnavailable("signed out", auth=True)

    logging.disable(logging.WARNING)
    try:
        watching.fetch_state = signed_out
        w = SectionsWatcher(config, queue)
        await w.start()
        await asyncio.sleep(0.3)
        await w.close()
    finally:
        watching.fetch_state = real_fetch
        logging.disable(logging.NOTSET)
    check("a signed-out scan spawns the refresh", ran.exists())
    check("the cooldown spawns it only once", ran.read_text() == "x")
    check("the refresh wrote the live cookie, seen without a reload",
          config.current_cookie() == "session=fresh")

    off = replace(config, auto_refresh=False)
    ran.unlink()
    logging.disable(logging.WARNING)
    try:
        watching.fetch_state = signed_out
        w = SectionsWatcher(off, EventQueue(tmp / "selfheal-off.json", 3600.0))
        await w.start()
        await asyncio.sleep(0.15)
        await w.close()
    finally:
        watching.fetch_state = real_fetch
        logging.disable(logging.NOTSET)
    check("auto_refresh off never spawns a browser", not ran.exists())


def fake_gmail_files(tmp: Path) -> tuple[Path, Path]:
    """A connector that looks authorized, without Google."""
    keys = tmp / "gcp-oauth.keys.json"
    token = tmp / "credentials.json"
    keys.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "s"}}))
    token.write_text(json.dumps({"refresh_token": "r"}))
    return keys, token


async def alarm_checks(tmp: Path) -> None:
    print("the email alarm:")
    import base64
    from email import message_from_bytes

    keys, token = fake_gmail_files(tmp)
    sender = GmailSender(keys, token)
    check("an authorized connector is available", sender.available())
    check("a missing connector is not",
          not GmailSender(tmp / "nope.json", tmp / "nope2.json").available())

    captured: list[tuple[str, str, dict | None]] = []
    sender._request = lambda method, url, payload=None: (  # type: ignore[method-assign]
        captured.append((method, url, payload)) or {"id": "m1", "emailAddress": "me@x.edu"}
    )
    sender.send("", "Hi", "body")
    method, url, payload = captured[-1]
    parsed = message_from_bytes(base64.urlsafe_b64decode(payload["raw"]))
    check("send posts a raw RFC822 message",
          method == "POST" and url.endswith("/messages/send")
          and parsed["Subject"] == "Hi" and parsed.get_payload().strip() == "body")
    check("an empty recipient means the signed-in account",
          parsed["To"] == "me@x.edu" and captured[0][1].endswith("/profile"))
    sender.send("other@x.edu", "Yo", "b")
    check("a pinned recipient is honored",
          message_from_bytes(base64.urlsafe_b64decode(captured[-1][2]["raw"]))["To"]
          == "other@x.edu")
    check("no sender means no From header (Gmail stamps its own)",
          message_from_bytes(base64.urlsafe_b64decode(captured[-1][2]["raw"]))["From"] is None)
    sender.send("", "Yo", "b", "ciel@example.com")
    check("a sender becomes the From header",
          message_from_bytes(base64.urlsafe_b64decode(captured[-1][2]["raw"]))["From"]
          == "ciel@example.com")

    now = time.time()
    event = watching.ProactiveEvent(
        id="e1", source="sections", importance=3, created_at=now, expires_at=now + 1,
        summary="A spot just opened in CS 61B discussion section 12 — Tuesdays.",
        dedupe_key="k", payload={"section_id": "12", "spots": "2"},
    )
    subjects = [alarm_message(event, n, 5)[0] for n in range(1, 6)]
    check("each alarm has its own subject", len(set(subjects)) == 5
          and subjects[0] == "Spot open in section 12 — 2 left (1 of 5)")
    check("the body carries the spoken line and the link",
          alarm_message(event, 1, 5)[1].startswith(event.summary)
          and "sections.datastructur.es" in alarm_message(event, 1, 5)[1])

    sent: list[tuple[str, str]] = []
    config = SectionsConfig(
        enabled=True, cookie="session=probe", watch=("12",), poll_s=0.05,
        cookie_file=tmp / "no-cookie", email_on_opening=3, email_interval_s=0.01,
        gmail_oauth_keys=keys, gmail_token_file=token, email_from="ciel@example.com",
    )
    real_fetch = watching.fetch_state
    watching.fetch_state = lambda url, cookie: parse_state(payload_open())
    try:
        watcher = SectionsWatcher(config, EventQueue(tmp / "alarm-events.json", 3600.0))
        check("the watcher builds a mailer when asked to email", watcher._mailer is not None)
        watcher._mailer.send = (
            lambda to, subject, body, sender="": sent.append((to, subject, sender)) or "id"
        )
        await watcher.start()
        await asyncio.sleep(0.3)
        await watcher.close()
    finally:
        watching.fetch_state = real_fetch
    check("an opening emails the configured number of times", len(sent) == 3)
    check("with distinct subjects, to the default recipient, as the configured From",
          len({s for _, s, _ in sent}) == 3
          and all(to == "" and frm == "ciel@example.com" for to, _, frm in sent))

    logging.disable(logging.WARNING)
    try:
        quiet = SectionsWatcher(replace(config, email_on_opening=0),
                                EventQueue(tmp / "alarm-off.json", 3600.0))
        check("0 emails builds no mailer", quiet._mailer is None)
        unauth = SectionsWatcher(
            replace(config, gmail_token_file=tmp / "missing.json"),
            EventQueue(tmp / "alarm-unauth.json", 3600.0),
        )
        check("an unauthorized connector degrades to no mailer", unauth._mailer is None)
    finally:
        logging.disable(logging.NOTSET)


def payload_open():
    return payload([section(12, taken=2)])


class FakeRelay:
    """Stands in for smtplib.SMTP_SSL: records the login and the messages."""
    logins: list[tuple[str, str]] = []
    sent: list = []
    reject_login = False

    def __init__(self, host, port, timeout=None, context=None):
        FakeRelay.args = (host, port)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, token):
        if FakeRelay.reject_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad token")
        FakeRelay.logins.append((user, token))

    refuse: dict = {}

    def send_message(self, message):
        FakeRelay.sent.append(message)
        return dict(FakeRelay.refuse)


async def smtp_checks(tmp: Path) -> None:
    print("the SMTP relay:")
    real = mailmod.smtplib.SMTP_SSL
    mailmod.smtplib.SMTP_SSL = FakeRelay
    try:
        sender = SmtpSender("smtp.mx.cloudflare.net", 465, "api_token", "tok")
        check("a token makes it available", sender.available()
              and not SmtpSender("h", 465, "u", "").available())
        sender.send("me@gmail.com", "Hi", "body", "ciel@example.com")
        m = FakeRelay.sent[-1]
        check("it logs in as api_token with the token and sends over 465",
              FakeRelay.logins[-1] == ("api_token", "tok") and FakeRelay.args == ("smtp.mx.cloudflare.net", 465)
              and m["From"] == "ciel@example.com" and m["To"] == "me@gmail.com" and m["Subject"] == "Hi")
        copied = SmtpSender("h", 465, "api_token", "tok", copy_to="me@berkeley.edu")
        copied.send("me@gmail.com", "Hi", "b", "ciel@example.com")
        check("copy_to rides along as a Bcc", FakeRelay.sent[-1]["Bcc"] == "me@berkeley.edu")
        copied.send("Me@Berkeley.edu", "Hi", "b", "ciel@example.com")
        check("no Bcc when the copy address is the recipient", FakeRelay.sent[-1]["Bcc"] is None)
        try:
            sender.send("me@gmail.com", "Hi", "b", "")
            check("no From is refused before the wire", False)
        except MailUnavailable:
            check("no From is refused before the wire", True)
        FakeRelay.refuse = {"me@berkeley.edu": (550, b"unverified")}
        logging.disable(logging.WARNING)
        try:
            copied.send("me@gmail.com", "Hi", "b", "ciel@example.com")
            check("a refused copy still counts as sent", True)
        except MailUnavailable:
            check("a refused copy still counts as sent", False)
        finally:
            logging.disable(logging.NOTSET)
        FakeRelay.refuse = {"me@gmail.com": (550, b"denied")}
        try:
            sender.send("me@gmail.com", "Hi", "b", "ciel@example.com")
            check("a refused recipient is a failed send", False)
        except MailUnavailable as exc:
            check("a refused recipient is a failed send", "550" in str(exc))
        FakeRelay.refuse = {}
        FakeRelay.reject_login = True
        try:
            sender.send("me@gmail.com", "Hi", "b", "ciel@example.com")
            check("a rejected token is one MailUnavailable", False)
        except MailUnavailable as exc:
            check("a rejected token is one MailUnavailable", "535" in str(exc))
        finally:
            FakeRelay.reject_login = False

        keys, token = fake_gmail_files(tmp)
        base = SectionsConfig(
            enabled=True, cookie="session=probe", watch=("12",), poll_s=0.05,
            cookie_file=tmp / "no-cookie", email_on_opening=2, email_interval_s=0.01,
            gmail_oauth_keys=keys, gmail_token_file=token,
        )
        q = lambda name: EventQueue(tmp / f"{name}.json", 3600.0)  # noqa: E731
        check("a token picks the relay over Gmail",
              isinstance(SectionsWatcher(replace(base, smtp_token="tok", email_from="ciel@example.com",
                                                 email_to="me@gmail.com"), q("s1"))._mailer, SmtpSender))
        check("no token keeps Gmail",
              isinstance(SectionsWatcher(base, q("s2"))._mailer, GmailSender))
        mail = MailConfig(enabled=True, address="ciel@example.com", owner="me@gmail.com", token="mt",
                          copy_to="me@berkeley.edu")
        shared = SectionsWatcher(base, q("s5"), mail=mail)
        check("[mail] armed lends its relay, address, owner, and copy to the alarm",
              isinstance(shared._mailer, SmtpSender) and shared._mailer._token == "mt"
              and shared._mailer._copy_to == "me@berkeley.edu"
              and shared._alarm_from == "ciel@example.com" and shared._alarm_to == "me@gmail.com")
        pinned = SectionsWatcher(replace(base, email_to="other@x.edu"), q("s6"), mail=mail)
        check("email_to still overrides the owner", pinned._alarm_to == "other@x.edu")
        own = SectionsWatcher(replace(base, smtp_token="tok", email_from="a@b.c", email_to="d@e.f"),
                              q("s7"), mail=mail)
        check("the watcher's own token wins over [mail]", own._mailer._token == "tok")
        logging.disable(logging.WARNING)
        try:
            check("[mail] with no owner and no email_to arms nothing",
                  SectionsWatcher(base, q("s8"), mail=replace(mail, owner=""))._mailer is None)
            check("[mail] disabled falls through to Gmail",
                  isinstance(SectionsWatcher(base, q("s9"), mail=replace(mail, enabled=False))._mailer,
                             GmailSender))
        finally:
            logging.disable(logging.NOTSET)
        logging.disable(logging.WARNING)
        try:
            check("a token without pinned addresses arms nothing",
                  SectionsWatcher(replace(base, smtp_token="tok"), q("s3"))._mailer is None)
        finally:
            logging.disable(logging.NOTSET)

        FakeRelay.sent.clear()
        real_fetch = watching.fetch_state
        watching.fetch_state = lambda url, cookie: parse_state(payload_open())
        try:
            w = SectionsWatcher(replace(base, smtp_token="tok", email_from="ciel@example.com",
                                        email_to="me@gmail.com"), q("s4"))
            await w.start(); await asyncio.sleep(0.25); await w.close()
        finally:
            watching.fetch_state = real_fetch
        check("an opening goes out through the relay as Ciel",
              len(FakeRelay.sent) == 2 and all(m["From"] == "ciel@example.com" and m["To"] == "me@gmail.com"
                                              for m in FakeRelay.sent))
    finally:
        mailmod.smtplib.SMTP_SSL = real


def prompt_checks() -> None:
    print("self-knowledge:")
    line = sections_watch_line(("12132",), 30.0, True, 5)
    check("the standing watch is described, with every outlet",
          line.startswith("One watch is standing") and "section 12132" in line
          and "every 30 seconds" in line and "texted if they are away" in line
          and "emailed 5 times" in line and "cannot enroll" in line)
    check("no outlets beyond speech when none are armed",
          "texted" not in sections_watch_line(("1",), 30.0, False, 0)
          and "emailed" not in sections_watch_line(("1",), 30.0, False, 0))
    check("two sections read as plural", "sections 1, 2" in sections_watch_line(("1", "2"), 30.0, False, 0))
    check("no watch, no line", sections_watch_line((), 30.0, True, 5) == "")
    check("the prompt carries it under Watching things",
          "section 12132" in build_system_prompt(vigil=True, vigil_standing=line)
          and "section 12132" not in build_system_prompt(vigil=True))


def config_checks(tmp: Path) -> None:
    print("config:")
    fresh = SectionsConfig()
    check("off by default, quick when on",
          not fresh.enabled and fresh.poll_s == 30.0
          and fresh.url == "https://sections.datastructur.es"
          and fresh.watch == () and not fresh.auto_refresh
          and fresh.refresh_cmd == ()
          and fresh.cookie_file.name == "sections-cookie"
          and fresh.email_on_opening == 0 and fresh.email_interval_s == 60.0
          and fresh.email_to == "" and fresh.email_from == "" and fresh.smtp_token == ""
          and fresh.smtp_host == "smtp.mx.cloudflare.net" and fresh.smtp_port == 465
          and fresh.gmail_token_file.name == "credentials.json")

    cf = tmp / "precedence-cookie"
    seeded = SectionsConfig(cookie="session=seed", cookie_file=cf)
    check("the seed is used when no live file exists",
          seeded.current_cookie() == "session=seed")
    cf.write_text("  session=live\n")
    check("the live file wins over the seed",
          seeded.current_cookie() == "session=live")
    cf.write_text("   ")
    check("a blank live file falls back to the seed",
          seeded.current_cookie() == "session=seed")

    toml = tmp / "config.toml"
    toml.write_text(
        '[sections]\nenabled = true\ncookie = "session=abc"\n'
        'watch = ["12", "31"]\npoll_s = 15\n'
    )
    loaded = load_config(toml).sections
    check("the TOML section loads",
          loaded.enabled and loaded.cookie == "session=abc"
          and loaded.watch == ("12", "31") and loaded.poll_s == 15.0)

    os.environ["CIEL_SECTIONS_COOKIE"] = "session=fresher"
    os.environ["CIEL_SECTIONS_WATCH"] = "7,8"
    try:
        env = load_config(toml).sections
        check("the environment wins over the file",
              env.cookie == "session=fresher" and env.watch == ("7", "8"))
    finally:
        del os.environ["CIEL_SECTIONS_COOKIE"]
        del os.environ["CIEL_SECTIONS_WATCH"]


def live() -> int:
    config = load_config().sections
    if not config.current_cookie():
        print("no cookie: sign in to the site in a browser, copy the Cookie")
        print("request header from any /api/ call in DevTools' Network tab,")
        print('and put it in ~/.ciel/config.toml as [sections] cookie = "..."')
        return 1
    if not config.enabled:
        print("note: [sections] enabled = false — the probe reads anyway; Ciel itself won't")
    try:
        state = fetch_state(config.url, config.current_cookie())
    except SectionsUnavailable as exc:
        print(f"unavailable: {exc}")
        return 1
    print(f"{state.course or config.url}: {len(state.sections)} sections\n")
    for s in sorted(state.sections, key=lambda s: (s.kind, s.start_time)):
        marks = ("  << watching" if s.id in config.watch else "") + (
            "  (enrolled)" if s.id in state.enrolled_ids else ""
        )
        print(f"  {describe(s)}{marks}")
    missing = [sid for sid in config.watch if not any(s.id == sid for s in state.sections)]
    if missing:
        print(f"\nwatched but not listed by the site: {', '.join(missing)}")
    open_watched = [
        s for s in state.sections
        if s.id in config.watch and s.spots > 0 and s.id not in state.enrolled_ids
    ]
    if open_watched:
        print(f"\nopen right now among the watched: "
              f"{', '.join('#' + s.id for s in open_watched)}")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="list the site's sections for real")
    args = parser.parse_args()

    if args.live:
        return live()

    parsing_checks()
    transition_checks()
    with tempfile.TemporaryDirectory() as tmp:
        await watcher_checks(Path(tmp))
        await selfheal_checks(Path(tmp))
        await alarm_checks(Path(tmp))
        await smtp_checks(Path(tmp))
        prompt_checks()
        config_checks(Path(tmp))
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
