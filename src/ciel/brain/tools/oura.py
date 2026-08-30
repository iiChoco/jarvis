"""The Oura tool — "how did I sleep", answered from the ring.

The model converts speech to a window ("last night" → today, because Oura
files a night under the morning it ended on; "this week" → seven days)
and the client fetches; the shaping in ``ciel.oura`` turns the records into
sentences the model speaks from. Read-only end to end, which is why it sits
on the Witness list: an unattended morning turn may check the ring before
it says anything about the night.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

from claude_agent_sdk import tool

from ciel.oura import MAX_RANGE_DAYS, OuraClient, OuraUnavailable, summarize_range

log = logging.getLogger(__name__)

# Bound at startup, same pattern as the timer service: the SDK's @tool
# decorator wants plain module-level functions.
_client: OuraClient | None = None


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def parse_day(spec: str, today: datetime.date) -> datetime.date | None:
    """'today' / 'yesterday' / '' / ISO date → a date; None when unparsable.
    ``today`` is injected so the probe can pin the calendar."""
    word = (spec or "").strip().lower()
    if word in ("", "today", "tonight", "last night", "this morning"):
        return today
    if word == "yesterday":
        return today - datetime.timedelta(days=1)
    try:
        return datetime.date.fromisoformat(word)
    except ValueError:
        return None


def label_for(day: datetime.date, today: datetime.date) -> str:
    """A spoken-style label: today and yesterday by name, the rest by
    weekday and date — what a person says when reading a week back."""
    if day == today:
        return "Today"
    if day == today - datetime.timedelta(days=1):
        return "Yesterday"
    return day.strftime("%A %-d %B")


def _fetch_sync(
    client: OuraClient, start: datetime.date, end: datetime.date
) -> tuple[list, list, list, list]:
    return (
        client.daily_readiness(start, end),
        client.daily_sleep(start, end),
        client.daily_activity(start, end),
        client.sleep_sessions(start, end),
    )


async def summary_for(
    client: OuraClient, end: datetime.date, days: int, today: datetime.date
) -> str:
    """The tool's answer for a window ending on ``end``: one labelled line
    per day, oldest first, fetched off the event loop."""
    days = max(1, min(int(days), MAX_RANGE_DAYS))
    start = end - datetime.timedelta(days=days - 1)
    readiness, sleep, activity, sessions = await asyncio.to_thread(
        _fetch_sync, client, start, end
    )
    window = [start + datetime.timedelta(days=i) for i in range(days)]
    return "\n".join(
        f"{label_for(day, today)}: {text}"
        for day, text in summarize_range(window, readiness, sleep, activity, sessions)
    )


@tool(
    "oura_summary",
    (
        "How the user slept and how recovered they are, from their Oura "
        "ring — for 'how did I sleep', 'what's my readiness', 'how active "
        "was I yesterday', 'how has my sleep been this week'. Pass `day` as "
        "'today', 'yesterday', or YYYY-MM-DD (default today). A night's "
        "sleep is filed under the morning it ended, so 'last night' is "
        "today. Pass `days` to widen to a range ending on that day (default "
        "1, max 14). Scores are 0-100; durations are actual time asleep. "
        "Read-only. Speak the numbers briefly — lead with hours asleep and "
        "the readiness score, mention a contributor only when it stands "
        "out, and don't read back every field."
    ),
    {"day": str, "days": int},
)
async def oura_summary(args: dict[str, Any]) -> dict[str, Any]:
    if _client is None:
        return _text("The Oura ring is not connected right now.")

    today = datetime.date.today()
    end = parse_day(str(args.get("day") or ""), today)
    if end is None:
        return _text(
            f"Could not parse {args.get('day')!r} — pass 'today', 'yesterday', "
            "or a YYYY-MM-DD date."
        )
    if end > today:
        return _text("That day hasn't happened yet.")
    try:
        days = int(args.get("days") or 1)
    except (TypeError, ValueError):
        days = 1

    try:
        return _text(await summary_for(_client, end, days, today))
    except OuraUnavailable as exc:
        log.warning("oura summary failed: %s", exc)
        return _text(str(exc))


def bind_oura(client: OuraClient) -> None:
    """Attach the client this tool reads through."""
    global _client
    _client = client


OURA_TOOLS = [oura_summary]

__all__ = ["OURA_TOOLS", "bind_oura", "oura_summary", "parse_day", "summary_for"]
