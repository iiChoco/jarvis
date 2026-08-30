"""Spoken commands that never reach the brain.

"Ten minute timer" does not need a frontier model. Routing it through the
brain costs seconds of latency and a few cents; routing it through a regex
costs neither — and mechanical requests are exactly the ones where the wait
feels most absurd. This module is the dispatcher's grammar: pure text in,
:class:`Command` out, no side effects, so every pattern is table-testable.

The contract is *conservative certainty*. A pattern here fires only on a
whole utterance that cannot plausibly mean anything else — "cancel the
timer", not any sentence containing "timer". Anything the grammar is not
sure about falls through to the brain, which was already the path for
everything; a miss costs the old latency, never a wrong action. That is
also why reminders with labels and clock-time alarms stay brain-side: "wake
me at seven thirty tomorrow" and "remind me when the rice is done" carry
meaning the model should phrase, not a regex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_UNIT_S = {"second": 1.0, "minute": 60.0, "hour": 3600.0}

_NUM_WORD = "|".join([*_ONES, *_TENS])
_SMALL_WORD = "|".join(w for w, v in _ONES.items() if v <= 9)

# "10", "ten", "twenty five", "a"/"an" (as in "an hour").
_NUM = rf"(?P<num>\d+|an?|{_NUM_WORD})(?:\s+(?P<num2>{_SMALL_WORD}))?"

# "ten minutes", "an hour and a half", "two and a half minutes", "half an
# hour" — the half attaches before or after the unit depending on which way
# English wants it that day.
_DUR = (
    rf"(?:{_NUM}(?P<half1>\s+and\s+a\s+half)?"
    rf"\s+(?P<unit>second|minute|hour)s?(?P<half2>\s+and\s+a\s+half)?"
    rf"|(?P<halfof>half\s+an?\s+(?P<unit2>minute|hour)))"
)

_SET_TIMER = [
    re.compile(rf"^(?:set\s+|start\s+)?(?:an?\s+)?{_DUR}\s+timer$"),
    re.compile(rf"^(?:set\s+|start\s+)?(?:a\s+)?timer\s+for\s+{_DUR}$"),
]
_CANCEL_TIMER = re.compile(
    r"^(?:cancel|stop)\s+(?:the\s+|my\s+|that\s+)?(?:timer|alarm)$"
)
_LIST_TIMERS = re.compile(
    r"^(?:list|check)\s+(?:the\s+|my\s+)?(?:timers?|alarms?)$"
    r"|^what\s+timers?\s+(?:are|is)\s+(?:running|left|set)$"
    r"|^how\s+(?:long|much\s+time)\s+(?:is\s+|do\s+i\s+have\s+)?"
    r"(?:left|remaining)(?:\s+on\s+(?:the|my)\s+timer)?$"
)
_RELOAD = frozenset({"reload", "reload yourself"})

# The escape hatch: the user is done with this exchange — woke Ciel by
# accident, changed their mind mid-request, or is closing a follow-up window.
# All of these deserve an immediate quiet exit, not a model turn that answers
# "never mind" with conversation. Apostrophes arrive as spaces after
# normalization, hence "that s all".
_DISMISS = re.compile(
    r"^(?:no\s+|nope\s+)?"
    r"(?:never\s?mind(?:\s+that|\s+then)?"
    r"|forget\s+(?:it|that|about\s+it)"
    r"|cancel(?:\s+that)?"
    r"|nothing(?:\s+never\s?mind)?"
    r"|stop(?:\s+it|\s+talking)?"
    r"|shut\s+up"
    r"|that\s?s\s+all(?:\s+for\s+now)?"
    r"|that\s+is\s+all"
    r"|that\s?ll\s+be\s+all"
    r"|we\s?re\s+done(?:\s+here)?)$"
)

# Leading address words the wake path lets through when spoken in one breath
# — "jarvis" for the default wake phrase, and "seal", Whisper's favourite
# mishearing of the name.
_ADDRESS = frozenset({"hey", "okay", "ok", "ciel", "seal", "jarvis"})


@dataclass(frozen=True, slots=True)
class Command:
    kind: str  # "reload" | "set_timer" | "cancel_timer" | "list_timers" | "dismiss"
    seconds: float = 0.0  # set_timer only


def _seconds(m: re.Match[str]) -> float | None:
    if m.group("halfof"):
        return 0.5 * _UNIT_S[m.group("unit2")]
    word = m.group("num")
    n = (
        float(word) if word.isdigit()
        else 1.0 if word in ("a", "an")
        else float(_ONES.get(word) or _TENS[word])
    )
    if m.group("num2"):
        if word not in _TENS:
            return None  # "five six minute timer" is a mishearing, not a number
        n += _ONES[m.group("num2")]
    if m.group("half1") or m.group("half2"):
        n += 0.5
    seconds = n * _UNIT_S[m.group("unit")]
    # Sanity bounds: sub-second is noise, past a day is a calendar's job.
    return seconds if 1.0 <= seconds <= 24 * 3600 else None


def match(text: str) -> Command | None:
    """The whole grammar. Returns None for everything uncertain — the brain
    is the fallback for all of speech, so a miss here is never a loss."""
    tokens = re.sub(r"[^\w\s]", " ", text.lower()).split()
    while tokens and tokens[0] in _ADDRESS:
        tokens.pop(0)
    while tokens and tokens[-1] in ("please", "thanks"):
        tokens.pop()
    if len(tokens) >= 2 and tokens[-2:] == ["thank", "you"]:
        del tokens[-2:]
    s = " ".join(tokens)
    if not s:
        return None

    if s in _RELOAD:
        return Command("reload")
    if _CANCEL_TIMER.fullmatch(s):
        return Command("cancel_timer")
    if _DISMISS.fullmatch(s):
        return Command("dismiss")
    if _LIST_TIMERS.fullmatch(s):
        return Command("list_timers")
    for pattern in _SET_TIMER:
        m = pattern.fullmatch(s)
        if m:
            seconds = _seconds(m)
            return Command("set_timer", seconds) if seconds else None
    return None


__all__ = ["Command", "match"]
