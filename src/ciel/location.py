"""Where the user is — from the sources this machine can actually see.

Two readings, honest about what each one is:

* **Find My** — the phone's position, from the cache Find My.app keeps at
  ``~/Library/Caches/com.apple.findmy.fmipcore/Devices.data``. The only
  source here that locates the *person* rather than the laptop. Two
  gates stand in front of it: the directory is TCC-protected, so the
  process running Ciel needs Full Disk Access; and Apple changed the
  cache from plain JSON to an opaque format in macOS 14.4, so on newer
  systems it may be unreadable even then. Each gate is logged once, with
  its remedy, and the watcher carries on with the next source.
* **Wi-Fi** — the name of the network the Mac is on, via
  ``system_profiler`` (CoreWLAN and ``ipconfig`` redact the SSID without
  Location Services now; ``system_profiler`` still says it). Needs no
  permission. Locates the laptop, which is the user whenever the laptop
  is with them — and for a machine that lives on a desk, "at home" is
  exactly the fact worth knowing.

CoreLocation is deliberately absent: macOS only shows the location
permission dialog to bundled apps with a usage string, and a Python
process is not one — verified here, ``kCLErrorDenied`` with no prompt.

Places are config: a name, and a Wi-Fi network and/or coordinates that
mean it. A fix resolves to the first place it matches, so the spoken
answer is "at home", not a pair of numbers. Read-only throughout: nothing
here writes anywhere, and the tool that speaks it is on the Witness list.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ciel.timers import spoken_clock

if TYPE_CHECKING:
    from ciel.config import LocationConfig

log = logging.getLogger(__name__)

_PROFILER_TIMEOUT_S = 30.0
"""``system_profiler`` takes about five seconds; this is the runaway
bound, not the expectation."""


class LocationUnavailable(RuntimeError):
    """A source could not answer. The message says why, in words a log
    reader can act on."""


@dataclass(frozen=True, slots=True)
class Fix:
    """One reading. ``at`` is when the reading was *taken* — a Find My
    position carries its own timestamp, so an hours-old cache entry says
    so instead of masquerading as now."""

    at: float
    source: str
    """"findmy" (the phone) or "wifi" (this Mac)."""

    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    network: str | None = None
    device: str | None = None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    networks: tuple[str, ...] = ()
    latitude: float | None = None
    longitude: float | None = None


def parse_places(raw: dict[str, Any]) -> dict[str, Place]:
    """``[location.places]`` → places. Each value may be a Wi-Fi name, a
    list of Wi-Fi names, a ``[lat, lon]`` pair, or a table with
    ``network``/``networks`` and ``lat``/``lon`` — whichever the person
    has to hand. Unparseable entries are warned about and skipped."""
    places: dict[str, Place] = {}
    for name, value in (raw or {}).items():
        networks: tuple[str, ...] = ()
        lat = lon = None
        if isinstance(value, str):
            networks = (value,)
        elif isinstance(value, (list, tuple)):
            if len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
                lat, lon = float(value[0]), float(value[1])
            else:
                networks = tuple(str(v) for v in value if isinstance(v, str))
        elif isinstance(value, dict):
            one = value.get("network")
            many = value.get("networks")
            networks = tuple(
                str(v) for v in ([one] if isinstance(one, str) else []) + (
                    list(many) if isinstance(many, (list, tuple)) else []
                )
            )
            if isinstance(value.get("lat"), (int, float)) and isinstance(value.get("lon"), (int, float)):
                lat, lon = float(value["lat"]), float(value["lon"])
        if not networks and lat is None:
            log.warning("[location.places] %r is not a network name, a [lat, lon] pair, or a table — ignored", name)
            continue
        places[str(name)] = Place(str(name), networks, lat, lon)
    return places


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres — good to a fraction of a percent,
    which is far inside any place's radius."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(a))


def place_of(fix: Fix, places: dict[str, Place], radius_m: float) -> str | None:
    """The first configured place a fix matches: by coordinates when the
    fix has them, by network when it has that. None is "somewhere else"."""
    for place in places.values():
        if fix.has_coordinates and place.latitude is not None and place.longitude is not None:
            if haversine_m(fix.latitude, fix.longitude, place.latitude, place.longitude) <= radius_m:
                return place.name
        if fix.network and fix.network.strip().lower() in {n.strip().lower() for n in place.networks}:
            return place.name
    return None


# ── sources ──────────────────────────────────────────────────────────────────


def read_findmy(cache: Path, device_fragment: str, now: float) -> Fix | None:
    """The named device's last position from Find My's cache, or None when
    the device has none. Raises LocationUnavailable when the cache cannot
    be read at all, with the reason."""
    try:
        raw = cache.read_bytes()
    except PermissionError as exc:
        raise LocationUnavailable(
            f"{cache} is protected — grant Full Disk Access to the app that "
            "runs Ciel (System Settings → Privacy & Security → Full Disk "
            "Access) for the phone's position"
        ) from exc
    except FileNotFoundError as exc:
        raise LocationUnavailable(
            f"{cache} does not exist — open Find My.app once so it writes its cache"
        ) from exc
    except OSError as exc:
        raise LocationUnavailable(f"could not read {cache} ({exc})") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LocationUnavailable(
            f"{cache} is not JSON — macOS 14.4 and later keep this cache in an "
            "opaque format, so the phone's position is out of reach here"
        ) from exc
    devices = data.get("devices") if isinstance(data, dict) else data
    if not isinstance(devices, list):
        raise LocationUnavailable(f"{cache} has a shape this reader does not know")
    wanted = device_fragment.strip().lower()
    for device in devices:
        if not isinstance(device, dict):
            continue
        name = str(device.get("name") or device.get("deviceDisplayName") or "")
        if wanted and wanted not in name.lower():
            continue
        location = device.get("location")
        if not isinstance(location, dict):
            continue
        lat, lon = location.get("latitude"), location.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        stamp = location.get("timeStamp")
        # Milliseconds in every cache anyone has published; seconds would
        # be before 1971, so the split is unambiguous.
        at = float(stamp) / 1000.0 if isinstance(stamp, (int, float)) and stamp > 1e11 else (
            float(stamp) if isinstance(stamp, (int, float)) else now
        )
        accuracy = location.get("horizontalAccuracy")
        return Fix(
            at=at,
            source="findmy",
            latitude=float(lat),
            longitude=float(lon),
            accuracy_m=float(accuracy) if isinstance(accuracy, (int, float)) else None,
            device=name or None,
        )
    return None


def read_wifi_network(run: Callable[..., Any] = subprocess.run) -> str | None:
    """The name of the Wi-Fi network this Mac is on, or None when it is on
    none. ``run`` is injectable for the probe."""
    try:
        completed = run(
            ["/usr/sbin/system_profiler", "SPAirPortDataType", "-json"],
            capture_output=True, text=True, timeout=_PROFILER_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocationUnavailable(f"system_profiler failed ({exc})") from exc
    try:
        data = json.loads(completed.stdout or "{}")
        interfaces = data["SPAirPortDataType"][0]["spairport_airport_interfaces"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LocationUnavailable("system_profiler's Wi-Fi report has an unexpected shape") from exc
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        current = interface.get("spairport_current_network_information")
        if isinstance(current, dict) and isinstance(current.get("_name"), str):
            name = current["_name"].strip()
            if name:
                return name
    return None


# ── the locator ──────────────────────────────────────────────────────────────


class Locator:
    """The one reader the tool and the watcher share: the freshest fix,
    which place it is, and the words for it.

    Readers are injectable for probes. Each source's failure is logged
    once per reason — "Full Disk Access" is worth one line, not one per
    five minutes — and never costs the other source.
    """

    def __init__(
        self,
        config: "LocationConfig",
        *,
        findmy_reader: Callable[[Path, str, float], Fix | None] | None = None,
        wifi_reader: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._places = parse_places(config.places)
        self._findmy = findmy_reader or read_findmy
        self._wifi = wifi_reader or read_wifi_network
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: Fix | None = None
        self._refreshed_at = 0.0
        self._warned: set[str] = set()

    @property
    def places(self) -> dict[str, Place]:
        return self._places

    @property
    def latest(self) -> Fix | None:
        return self._latest

    def refresh(self) -> Fix | None:
        """Read the sources now. The phone wins when Find My has a position
        recent enough to still mean "where they are"; otherwise the Mac's
        network. Blocking — callers use a worker thread."""
        now = self._clock()
        fix: Fix | None = None
        if self._config.findmy_device:
            try:
                found = self._findmy(self._config.findmy_cache, self._config.findmy_device, now)
                if found is not None and now - found.at <= self._config.findmy_max_age_s:
                    fix = found
                elif found is not None:
                    log.debug("find my position is %.0f s old — using the network instead", now - found.at)
            except LocationUnavailable as exc:
                self._warn_once("findmy", f"find my source is off: {exc}")
            except Exception:  # noqa: BLE001 - one dead source must not cost the other
                log.exception("find my read failed")
        if fix is None and self._config.wifi:
            try:
                network = self._wifi()
                if network:
                    fix = Fix(at=now, source="wifi", network=network)
            except LocationUnavailable as exc:
                self._warn_once("wifi", f"wi-fi source is off: {exc}")
            except Exception:  # noqa: BLE001
                log.exception("wi-fi read failed")
        with self._lock:
            self._refreshed_at = now
            if fix is not None:
                self._latest = fix
        return fix

    def current(self, max_age_s: float) -> Fix | None:
        """The latest fix, refreshed first if older than ``max_age_s``."""
        with self._lock:
            fresh = self._latest is not None and self._clock() - self._refreshed_at <= max_age_s
        return self._latest if fresh else self.refresh()

    def place_of(self, fix: Fix) -> str | None:
        return place_of(fix, self._places, self._config.place_radius_m)

    def describe(self, fix: Fix | None, now: float | None = None) -> str:
        """A fix as a spoken sentence: the place if known, the evidence,
        and how old the reading is — the model speaks from this."""
        if fix is None:
            return "No location reading yet — the Mac is on no Wi-Fi and no phone position is available."
        now = self._clock() if now is None else now
        age = max(0.0, now - fix.at)
        when = "just now" if age < 90 else f"as of {spoken_clock(fix.at)}"
        place = self.place_of(fix)
        if fix.source == "findmy":
            who = f"your {fix.device}" if fix.device else "the phone"
            where = (
                f"At {place}" if place else
                f"At {fix.latitude:.4f}, {fix.longitude:.4f}"
                + (f" (give or take {fix.accuracy_m:.0f} metres)" if fix.accuracy_m else "")
                + " — not a place I know"
            )
            return f"{where}, per {who} via Find My, {when}."
        where = f"At {place}" if place else f"On the {fix.network} Wi-Fi, which is not a place I know"
        return f"{where} — this Mac's network, {when}."

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning(message)


__all__ = [
    "Fix",
    "Locator",
    "LocationUnavailable",
    "Place",
    "haversine_m",
    "parse_places",
    "place_of",
    "read_findmy",
    "read_wifi_network",
]
