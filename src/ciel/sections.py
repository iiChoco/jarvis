"""The section-signup site — reading spot counts from sections.datastructur.es.

Berkeley courses run section signups on the open-source *sections* app
(sections.datastructur.es for CS 61B). The app is a React shell over a
same-origin JSON API; everything this module needs comes from one call,
``POST /api/refresh_state``, which returns every section with its
``capacity`` and current ``students`` roster — spots left is the
difference. The catch is authentication: the API answers anonymously with
an *empty* section list (no error, just nothing), and only a session
signed in through the course's Canvas OAuth sees real data. There is no
token flow to automate — the user logs in once in a browser and hands
Ciel the request's ``Cookie`` header, which lives in ``[sections]`` config
like the Discord token does.

Everything is synchronous stdlib urllib on purpose, same reasoning as
``oura.py``: the callers wrap requests in ``asyncio.to_thread``, so the
event loop never waits on the site. Read-only by construction — the API
also offers ``join_section``, and this module deliberately does not: Ciel
tells you a spot opened; taking it stays a human act.

Sections are weekly: ``startTime``/``endTime`` are epoch seconds anchoring
the first meeting, and the wall-clock weekday and time are what a section
*is* ("Tuesdays at 10"), so phrasing formats the anchor's local wall time
rather than rolling the epoch forward — fixed wall time is exactly what
survives the DST crossing mid-semester.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 15.0

RELOGIN = (
    "sign in to the sections site in a browser and paste the fresh Cookie "
    "header into [sections] cookie in ~/.ciel/config.toml (scripts/"
    "probe_sections.py --live checks it)"
)


class SectionsUnavailable(RuntimeError):
    """The site could not answer — network down, HTTP error, or a cookie
    that no longer signs in. One exception type so callers degrade the
    same way everywhere: the watcher turns it into a health-streak
    failure whose note points at the fix. The message is human-shaped for
    exactly that reason."""

    def __init__(
        self, message: str, status: int | None = None, auth: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.auth = auth
        """True when the cause is the cookie (signed out, or a 401/403) —
        the case a re-login fixes, as opposed to the site being down or
        unreachable, where the cookie may be perfectly good and a browser
        refresh would be wasted."""


@dataclass(frozen=True, slots=True)
class Section:
    """One weekly section, reduced to what watching needs."""

    id: str
    kind: str
    """The app calls this ``name``: "Discussion", "Lab", or "Tutoring"."""
    location: str
    description: str
    tags: tuple[str, ...]
    staff: str
    """The teaching assistant's name, or "" while unassigned."""
    capacity: int
    enrolled: int
    start_time: float
    end_time: float

    @property
    def spots(self) -> int:
        return max(self.capacity - self.enrolled, 0)


@dataclass(frozen=True, slots=True)
class SectionsState:
    """One ``refresh_state`` answer: the sections, and which of them the
    signed-in user is already enrolled in (openings there are old news)."""

    sections: tuple[Section, ...]
    enrolled_ids: frozenset[str]
    course: str = ""
    """The course the deployment serves ("CS 61B"), straight from the
    payload — it opens the spoken sentence, so it should be the site's own
    words, not config."""


def _clock(epoch: float) -> str:
    return time.strftime("%-I:%M %p", time.localtime(epoch))


def when_phrase(section: Section) -> str:
    """The section's weekly slot as spoken English — "Tuesdays 10:00 AM to
    11:00 AM". Formats the anchor meeting's local wall time: the wall time
    is the invariant, the epoch is just its first instance."""
    if section.start_time <= 0:
        return "time unlisted"
    day = time.strftime("%A", time.localtime(section.start_time))
    phrase = f"{day}s {_clock(section.start_time)}"
    if section.end_time > section.start_time:
        phrase += f" to {_clock(section.end_time)}"
    return phrase


def describe(section: Section) -> str:
    """One listing line for the probe: everything a person needs to decide
    whether this is a section they want."""
    tags = f" [{', '.join(section.tags)}]" if section.tags else ""
    staff = f" with {section.staff}" if section.staff else ""
    where = section.location or "location unlisted"
    return (
        f"#{section.id}  {section.kind}{tags}  {when_phrase(section)}  "
        f"{where}{staff}  — {section.spots}/{section.capacity} spots open"
    )


def _parse_section(raw: dict[str, Any]) -> Section | None:
    """One raw section → a Section, or None for a shape too broken to use.

    Tolerant on purpose: the app is course-staff-operated and its payload
    is not a contract. A section missing its id or capacity is skipped and
    logged, never fatal — one odd row must not blind the watcher to the
    rest of the list.
    """
    sid = raw.get("id")
    capacity = raw.get("capacity")
    if sid is None or not isinstance(capacity, (int, float)):
        log.warning("skipping a section with no id/capacity: %r", raw)
        return None
    students = raw.get("students")
    staff = raw.get("staff")
    tags = raw.get("tags")
    return Section(
        id=str(sid),
        kind=str(raw.get("name") or "Section"),
        location=str(raw.get("location") or ""),
        description=str(raw.get("description") or ""),
        tags=tuple(str(t) for t in tags) if isinstance(tags, list) else (),
        staff=str((staff or {}).get("name") or "") if isinstance(staff, dict) else "",
        capacity=int(capacity),
        enrolled=len(students) if isinstance(students, list) else 0,
        start_time=float(raw.get("startTime") or 0),
        end_time=float(raw.get("endTime") or 0),
    )


def parse_state(payload: Any) -> SectionsState:
    """The ``refresh_state`` response body → a SectionsState.

    Raises :class:`SectionsUnavailable` when the answer is well-formed but
    signed out — ``currentUser`` null and no sections — because that is
    the cookie having expired, and it must surface as a failure the
    health streak can report, not as "every section quietly vanished"
    (which the transition logic would misread as nothing to say).
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not payload.get("success", False):
        raise SectionsUnavailable("the sections site sent an unrecognized answer")
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list):
        raw_sections = []
    if data.get("currentUser") is None and not raw_sections:
        raise SectionsUnavailable(
            f"the sections cookie is signed out — {RELOGIN}", auth=True
        )
    sections = tuple(
        s for raw in raw_sections
        if isinstance(raw, dict) and (s := _parse_section(raw)) is not None
    )
    enrolled = data.get("enrolledSections")
    enrolled_ids = frozenset(
        str(e["id"]) for e in enrolled if isinstance(e, dict) and "id" in e
    ) if isinstance(enrolled, list) else frozenset()
    return SectionsState(
        sections=sections,
        enrolled_ids=enrolled_ids,
        course=str(data.get("course") or ""),
    )


def fetch_state(base_url: str, cookie: str) -> SectionsState:
    """POST ``/api/refresh_state`` with the saved cookie and parse it.

    Synchronous; callers thread it. Network trouble and HTTP errors all
    become :class:`SectionsUnavailable` with a human-shaped message.
    """
    url = base_url.rstrip("/") + "/api/refresh_state"
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json", "Cookie": cookie},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SectionsUnavailable(
                f"the sections site refused the cookie ({exc.code}) — {RELOGIN}",
                status=exc.code,
                auth=True,
            ) from exc
        raise SectionsUnavailable(
            f"the sections site answered {exc.code}", status=exc.code
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SectionsUnavailable(
            f"the sections site could not be reached ({exc})"
        ) from exc
    return parse_state(payload)


__all__ = [
    "RELOGIN",
    "Section",
    "SectionsState",
    "SectionsUnavailable",
    "describe",
    "fetch_state",
    "parse_state",
    "when_phrase",
]
