"""Re-mint the sections.datastructur.es session cookie, unattended.

The site's session is a sliding two-hour window: any request pushes its
expiry forward, so while Ciel polls it never lapses. It dies only after
two hours with no request — an overnight sleep. Reviving it means the
Canvas OAuth login again, and that login needs a live bCourses session.
This script keeps one in a dedicated browser profile
(`~/.ciel/sections-browser`) that you sign into exactly once (`--login`,
where you clear CalNet and Duo yourself and tick "remember this device");
from then on the OAuth round-trip is silent server redirects a headless
Chrome can follow, and each run writes the fresh cookie to
`~/.ciel/sections-cookie`, where Ciel's watcher reads it on the next poll.

    <venv>/bin/python scripts/refresh_sections_cookie.py --login   # once, headed
    <venv>/bin/python scripts/refresh_sections_cookie.py           # scheduled/self-heal

Deliberately self-contained — it runs from its own small venv (Playwright
only, no Ciel package), so paths default to Ciel's and are overridable by
flag. It never handles your password or your Duo prompt: those happen in
the visible browser during `--login`, by your hand. If the remembered
session ever lapses, a scheduled run exits non-zero (Ciel then files its
ordinary re-login note) and one more `--login` re-arms it.

Needs Google Chrome installed (used via Playwright's `channel="chrome"`,
so no separate browser download) and the `playwright` pip package.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://sections.datastructur.es"
DEFAULT_PROFILE = Path.home() / ".ciel" / "sections-browser"
DEFAULT_COOKIE_FILE = Path.home() / ".ciel" / "sections-cookie"
_HTTP_TIMEOUT_S = 15.0


def log(message: str) -> None:
    print(f"[sections-refresh] {message}", flush=True)


def quick_check(url: str, cookie: str) -> bool:
    """Is this cookie still signed in? One plain request, no browser — so a
    scheduled run while the session is alive (the common case) costs a
    single HTTP call and never spins Chrome up."""
    if not cookie:
        return False
    request = urllib.request.Request(
        url.rstrip("/") + "/api/refresh_state",
        data=b"{}",
        headers={"Content-Type": "application/json", "Cookie": cookie},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return isinstance(data, dict) and data.get("currentUser") is not None


def write_cookie(cookie_file: Path, cookie: str) -> None:
    """Owner-only, atomically — a half-written credential is a signed-out
    watcher. Same 0600 care as the Oura token file."""
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cookie_file.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, cookie.encode("ascii"))
    finally:
        os.close(fd)
    tmp.replace(cookie_file)


def _session_cookie(context, url: str) -> str:
    for c in context.cookies(url):
        if c.get("name") == "session" and c.get("value"):
            return f"session={c['value']}"
    return ""


def _authenticated(context, url: str) -> bool:
    """Ask the site, through the browser's own session, whether it knows
    who we are — the same signal the app uses, so it can't disagree."""
    try:
        response = context.request.post(
            url.rstrip("/") + "/api/refresh_state",
            data="{}",
            headers={"content-type": "application/json"},
            timeout=_HTTP_TIMEOUT_S * 1000,
        )
        payload = response.json()
    except Exception:  # noqa: BLE001 - any failure here means "not confirmed"
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return isinstance(data, dict) and data.get("currentUser") is not None


def run(profile: Path, url: str, cookie_file: Path, login: bool) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("playwright is not installed in this environment — see the setup notes")
        return 3

    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel="chrome",
                headless=not login,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"could not launch Chrome: {exc}")
            log("is Google Chrome installed? Playwright uses it via channel=chrome")
            return 3

        try:
            page = context.pages[0] if context.pages else context.new_page()
            if login:
                log("a browser window is open — sign in through CalNet and Duo,")
                log("tick 'remember this device', approve the app if asked, then wait.")
                page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                deadline = time.time() + 300
                while time.time() < deadline:
                    if _authenticated(context, url):
                        break
                    time.sleep(2)
                else:
                    log("timed out waiting for a signed-in session — nothing written")
                    return 1
            else:
                page.goto(
                    url.rstrip("/") + "/oauth/canvas_login",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                # Give the OAuth redirects a moment to settle back onto the site.
                for _ in range(10):
                    if _authenticated(context, url):
                        break
                    time.sleep(1)
                else:
                    host = ""
                    try:
                        from urllib.parse import urlparse
                        host = urlparse(page.url).netloc
                    except Exception:  # noqa: BLE001
                        pass
                    log(
                        "could not sign in silently — the profile's bCourses "
                        f"session has lapsed (stuck at {host or 'the login page'})."
                    )
                    log("run this script with --login once to re-arm it.")
                    return 1

            cookie = _session_cookie(context, url)
            if not cookie:
                log("signed in, but no session cookie was set — nothing written")
                return 1
            write_cookie(cookie_file, cookie)
            log(f"refreshed — wrote a fresh cookie to {cookie_file}")
            return 0
        finally:
            context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true",
                        help="one-time headed sign-in to seed the browser profile")
    parser.add_argument("--force", action="store_true",
                        help="refresh even if the current cookie still works")
    parser.add_argument("--url", default=os.environ.get("CIEL_SECTIONS_URL", DEFAULT_URL))
    parser.add_argument("--profile", type=Path,
                        default=Path(os.environ.get("CIEL_SECTIONS_PROFILE", DEFAULT_PROFILE)))
    parser.add_argument("--cookie-file", type=Path,
                        default=Path(os.environ.get("CIEL_SECTIONS_COOKIE_FILE", DEFAULT_COOKIE_FILE)))
    args = parser.parse_args()

    if not args.login and not args.force:
        try:
            current = args.cookie_file.read_text().strip()
        except OSError:
            current = ""
        if quick_check(args.url, current):
            log("the current cookie is still valid — nothing to do")
            return 0

    return run(args.profile, args.url, args.cookie_file, args.login)


if __name__ == "__main__":
    raise SystemExit(main())
