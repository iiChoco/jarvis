"""Probe the location layer — scripted by default, live on request.

    uv run scripts/probe_location.py          # scripted checks, no sources touched
    uv run scripts/probe_location.py --live   # read the real sources once

The scripted checks drive the place matching, both source readers against
fakes (a Find My cache in a temp dir, a canned system_profiler report),
the locator's precedence and one-warning-per-reason behaviour, the spoken
description, the move notes, the watcher's baseline-then-note state, the
tool, and config. The live mode reads what this machine can actually see
and says what it makes of it — the fastest way to learn whether Find My
is reachable (it needs Full Disk Access, and a macOS that still writes
JSON) and which network names to put in `[location.places]`.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from ciel.brain.tools import location as location_tool
from ciel.brain.tools.location import where_am_i
from ciel.config import Config, LocationConfig, load_config
from ciel.location import (
    Fix,
    Locator,
    LocationUnavailable,
    haversine_m,
    parse_places,
    place_of,
    read_findmy,
    read_wifi_network,
)
from ciel.proactive.events import EventQueue
from ciel.proactive.location import LocationWatcher, transition_event

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


def text_of(result) -> str:
    return result["content"][0]["text"]


HOME = (33.6405, -117.8443)
OFFICE = (33.6846, -117.8265)  # ~5 km away

PLACES = {
    "home": {"network": "attinternet", "lat": HOME[0], "lon": HOME[1]},
    "office": ["OfficeNet", "OfficeNet-5G"],
    "cabin": [44.0, -121.0],
}


def profiler_report(network: str | None) -> str:
    current = (
        {"_name": network, "spairport_network_channel": "36 (5GHz, 40MHz)"}
        if network else {"spairport_network_type": "none"}
    )
    return json.dumps({"SPAirPortDataType": [{"spairport_airport_interfaces": [
        {"_name": "en0", "spairport_status_information": "spairport_status_connected",
         "spairport_current_network_information": current},
        {"_name": "awdl0", "spairport_current_network_information": {"spairport_network_type": "x"}},
    ]}]})


class FakeRun:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return type("Done", (), {"stdout": self.stdout, "returncode": 0})()


def findmy_cache(path: Path, devices: list) -> None:
    path.write_text(json.dumps(devices))


def device(name: str, lat: float, lon: float, age_s: float, now: float, acc: float = 65.0) -> dict:
    return {"name": name, "deviceDisplayName": name, "batteryLevel": 0.8,
            "location": {"latitude": lat, "longitude": lon, "horizontalAccuracy": acc,
                         "timeStamp": int((now - age_s) * 1000), "positionType": "Wifi"}}


# ── scripted checks ──────────────────────────────────────────────────────────


def place_checks() -> None:
    print("places:")
    places = parse_places(PLACES)
    check("a table with network and coordinates parses",
          places["home"].networks == ("attinternet",) and places["home"].latitude == HOME[0])
    check("a list of names is networks", places["office"].networks == ("OfficeNet", "OfficeNet-5G"))
    check("a numeric pair is coordinates", places["cabin"].latitude == 44.0 and places["cabin"].networks == ())
    check("a bare string is one network", parse_places({"gym": "GymWifi"})["gym"].networks == ("GymWifi",))
    logging.disable(logging.WARNING)
    try:
        check("junk entries are skipped", parse_places({"bad": 42, "worse": {}}) == {})
    finally:
        logging.disable(logging.NOTSET)

    check("haversine is right to a few metres",
          abs(haversine_m(*HOME, *OFFICE) - 5150) < 200 and haversine_m(*HOME, *HOME) == 0.0)
    check("a network fix matches by name, case-insensitively",
          place_of(Fix(0, "wifi", network="officenet-5g"), places, 200) == "office")
    check("a coordinate fix matches within the radius",
          place_of(Fix(0, "findmy", latitude=HOME[0] + 0.001, longitude=HOME[1]), places, 200) == "home")
    check("outside the radius is nowhere",
          place_of(Fix(0, "findmy", latitude=HOME[0] + 0.01, longitude=HOME[1]), places, 200) is None)
    check("an unknown network is nowhere", place_of(Fix(0, "wifi", network="Cafe"), places, 200) is None)


def source_checks(tmp: Path) -> None:
    print("find my:")
    now = time.time()
    cache = tmp / "Devices.data"
    findmy_cache(cache, [device("Mac", 0, 0, 10, now), device("Choco's iPhone", *HOME, 120, now)])
    fix = read_findmy(cache, "iphone", now)
    check("the named device's position is read",
          fix is not None and fix.source == "findmy" and fix.latitude == HOME[0] and fix.device == "Choco's iPhone")
    check("the fix carries the cache's own timestamp", abs((now - fix.at) - 120) < 2 and fix.accuracy_m == 65.0)
    check("a device with no position is None",
          read_findmy(cache, "watch", now) is None)
    findmy_cache(cache, {"devices": [device("iPhone", *OFFICE, 0, now)]})
    check("a dict-wrapped cache is read too", read_findmy(cache, "iphone", now).longitude == OFFICE[1])
    cache.write_bytes(b"\\x00\\x01binary")
    try:
        read_findmy(cache, "iphone", now); check("an opaque cache says so", False)
    except LocationUnavailable as exc:
        check("an opaque cache says so", "14.4" in str(exc))
    try:
        read_findmy(tmp / "missing.data", "iphone", now); check("a missing cache says so", False)
    except LocationUnavailable as exc:
        check("a missing cache says so", "Find My.app" in str(exc))
    if os.geteuid() != 0:
        findmy_cache(cache, [])
        cache.chmod(0)
        try:
            read_findmy(cache, "iphone", now); check("a protected cache names Full Disk Access", False)
        except LocationUnavailable as exc:
            check("a protected cache names Full Disk Access", "Full Disk Access" in str(exc))
        finally:
            cache.chmod(0o600)

    print("wi-fi:")
    check("the current network is read from the report",
          read_wifi_network(FakeRun(profiler_report("attinternet"))) == "attinternet")
    check("no network is None", read_wifi_network(FakeRun(profiler_report(None))) is None)
    try:
        read_wifi_network(FakeRun("not json")); check("a broken report is unavailable", False)
    except LocationUnavailable:
        check("a broken report is unavailable", True)


def locator_checks(tmp: Path) -> None:
    print("the locator:")
    clock = [1_724_000_000.0]  # a real-scale epoch; Find My stamps are ms, distinguished by magnitude
    cache = tmp / "Devices.data"
    config = LocationConfig(enabled=True, places=PLACES, findmy_device="iphone",
                            findmy_cache=cache, findmy_max_age_s=600, poll_s=1)
    wifi = {"name": "OfficeNet", "calls": 0}

    def wifi_reader():
        wifi["calls"] += 1
        return wifi["name"]

    loc = Locator(config, wifi_reader=wifi_reader, clock=lambda: clock[0])
    findmy_cache(cache, [device("iPhone", *HOME, 60, clock[0])])
    fix = loc.refresh()
    check("a fresh phone position wins", fix.source == "findmy" and loc.place_of(fix) == "home")
    check("the network is not consulted when the phone answers", wifi["calls"] == 0)
    findmy_cache(cache, [device("iPhone", *HOME, 3600, clock[0])])
    fix = loc.refresh()
    check("a stale phone position yields to the network",
          fix.source == "wifi" and fix.network == "OfficeNet" and loc.place_of(fix) == "office")
    check("latest is the last good fix", loc.latest is fix)
    check("current serves from memory when fresh", loc.current(100) is fix and wifi["calls"] == 1)
    clock[0] += 500
    check("current refreshes when stale", loc.current(100) is not fix and wifi["calls"] == 2)

    cache.write_bytes(b"\\x00opaque")
    records: list[str] = []
    handler = logging.Handler(); handler.emit = lambda r: records.append(r.getMessage())
    logging.getLogger("ciel.location").addHandler(handler)
    try:
        loc.refresh(); loc.refresh(); loc.refresh()
    finally:
        logging.getLogger("ciel.location").removeHandler(handler)
    check("a dead source warns once and the other carries on",
          sum("find my source is off" in r for r in records) == 1 and loc.latest.source == "wifi")

    wifi["name"] = None
    before = loc.latest
    check("no sources means no fix, and the last one is kept", loc.refresh() is None and loc.latest is before)

    print("the words:")
    at = clock[0]
    check("a known place by wi-fi",
          loc.describe(Fix(at, "wifi", network="attinternet"), at) == "At home — this Mac's network, just now.")
    older = loc.describe(Fix(at - 3600, "wifi", network="Cafe"), at)
    check("an unknown network is honest about it",
          older.startswith("On the Cafe Wi-Fi, which is not a place I know — this Mac's network, as of "))
    check("a phone fix names the device and the place",
          loc.describe(Fix(at, "findmy", latitude=HOME[0], longitude=HOME[1], device="iPhone"), at)
          == "At home, per your iPhone via Find My, just now.")
    unknown = loc.describe(Fix(at, "findmy", latitude=10.0, longitude=20.0, accuracy_m=65, device="iPhone"), at)
    check("an unknown coordinate fix gives the numbers and the error",
          unknown == "At 10.0000, 20.0000 (give or take 65 metres) — not a place I know, per your iPhone via Find My, just now.")
    check("no fix at all says so", loc.describe(None).startswith("No location reading yet"))


async def watcher_checks(tmp: Path) -> None:
    print("move notes:")
    ids = iter(f"e{i}" for i in range(1, 100))
    nid = lambda: next(ids)  # noqa: E731
    now = time.time()
    fix = Fix(now, "wifi", network="x")
    arrive = transition_event(None, "office", fix, now, nid)
    check("arriving somewhere named is a note",
          arrive is not None and arrive.importance == 1 and arrive.source == "location"
          and arrive.summary.startswith("You arrived at office around "))
    leave = transition_event("home", None, fix, now, nid)
    check("leaving for nowhere named is a note", leave.summary.startswith("You left home around "))
    check("home to office is an arrival", transition_event("home", "office", fix, now, nid).summary.startswith("You arrived at office"))
    check("staying is nothing", transition_event("home", "home", fix, now, nid) is None)
    check("nowhere to nowhere is nothing", transition_event(None, None, fix, now, nid) is None)
    check("the note carries its source", arrive.payload == {"source": "wifi", "place": "office"})

    print("the watcher:")
    queue = EventQueue(tmp / "location-events.json", held_max_age_s=3600.0)
    network = {"name": "attinternet"}
    config = LocationConfig(enabled=True, places=PLACES, poll_s=0.05)
    loc = Locator(config, wifi_reader=lambda: network["name"])
    watcher = LocationWatcher(config, loc, queue)
    await watcher.start()
    await asyncio.sleep(0.12)
    check("the first reading is a silent baseline", not queue.pending and watcher._place == "home")
    network["name"] = "OfficeNet"
    watcher.kick()
    await asyncio.sleep(0.12)
    check("a move files one note", queue.count_source("location") == 1)
    await asyncio.sleep(0.15)
    check("staying put files nothing more", queue.count_source("location") == 1)
    network["name"] = "Cafe"
    watcher.kick()
    await asyncio.sleep(0.12)
    check("leaving for an unknown network files a departure", queue.count_source("location") == 2)
    await watcher.close()
    check("close stops the poll", watcher._task is None)

    print("the tool:")
    location_tool.bind_locator(None)
    check("unbound means unavailable", text_of(await where_am_i.handler({})) == "Location is not available right now.")
    location_tool.bind_locator(loc)
    check("bound, it speaks the latest reading",
          text_of(await where_am_i.handler({})).startswith("On the Cafe Wi-Fi, which is not a place I know"))

    def boom():
        raise RuntimeError("no report")

    logging.disable(logging.ERROR)
    try:
        stuck = Locator(LocationConfig(enabled=True, places=PLACES), wifi_reader=boom)
        location_tool.bind_locator(stuck)
        check("a broken source is a sentence", text_of(await where_am_i.handler({})).startswith("No location reading yet"))
    finally:
        logging.disable(logging.NOTSET)
        location_tool.bind_locator(None)


def config_checks(tmp: Path) -> None:
    print("config:")
    check("off by default", not Config().location.enabled)
    toml = tmp / "config.toml"
    toml.write_text(
        '[location]\nenabled = true\nfindmy_device = "iPhone"\npoll_s = 120\n\n'
        '[location.places]\nhome = "attinternet"\noffice = ["OfficeNet", "OfficeNet-5G"]\ncabin = [44.0, -121.0]\n'
    )
    loaded = load_config(toml).location
    check("[location] and [location.places] load from TOML",
          loaded.enabled and loaded.findmy_device == "iPhone" and loaded.poll_s == 120.0
          and parse_places(loaded.places)["office"].networks == ("OfficeNet", "OfficeNet-5G")
          and parse_places(loaded.places)["cabin"].latitude == 44.0)
    from ciel.brain.witness import witness_allowed
    check("the tool may run unattended", "mcp__ciel__where_am_i" in witness_allowed(Config()))
    from ciel.brain.prompt import build_system_prompt
    check("the prompt knows the tool only when enabled",
          "where_am_i" in build_system_prompt(location=True) and "where_am_i" not in build_system_prompt())
    from ciel.brain.tools import build_tool_server
    _s, allowed, *_ = build_tool_server(Config())
    check("the tool is withheld when [location] is off", not any("where_am_i" in a for a in allowed))


# ── live ─────────────────────────────────────────────────────────────────────


def live() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config().location
    if not config.enabled:
        print("note: [location] enabled = false — the probe reads anyway; Ciel itself won't")
    loc = Locator(config)
    print("reading the sources…")
    fix = loc.refresh()
    print("fix:", fix)
    print("place:", loc.place_of(fix) if fix else None)
    print("spoken:", loc.describe(fix))
    if fix is not None and fix.source == "wifi" and loc.place_of(fix) is None:
        print(f"\nto name this: under [location.places] add  somewhere = \"{fix.network}\"")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="read the real sources once")
    args = parser.parse_args()
    if args.live:
        return live()

    place_checks()
    with tempfile.TemporaryDirectory() as tmp:
        source_checks(Path(tmp))
        locator_checks(Path(tmp))
        await watcher_checks(Path(tmp))
        config_checks(Path(tmp))
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
