"""Cutting a stream of text into speakable sentences.

A synthesizer needs whole sentences: half a sentence has the wrong prosody
and lands as a stutter, and a sentence split inside a decimal pauses
mid-number. The rules for where a sentence ends are shared by every voice
in the house — Ciel's own brain and the interview room's interviewer — so
they live here as pure functions over a text buffer, with no client, no
config object, and no opinion about who is speaking.
"""

from __future__ import annotations

import re

# A sentence ends at .?! followed by whitespace or a closing quote/bracket — but
# not when the period is part of a decimal ("3.5" — the lookahead rejects it,
# since the next character is a digit) or an ellipsis. End-of-buffer is
# deliberately NOT a boundary: mid-stream the buffer ends at an arbitrary delta
# cut, so "3." at a delta boundary must wait for the next delta rather than
# split the number — the true end of a response is emitted by the caller's
# tail flush instead. Getting this wrong is audible: a premature split pauses
# mid-number.
BOUNDARY = re.compile(r"(?<!\.)([.!?])(?=[\s\"')\]])")

# Abbreviations that end in a period without ending a sentence. The prompt
# discourages these, but the model still produces them occasionally. Ordinary
# words that double as abbreviations ("no", "us") don't belong here — "The
# answer is no." is a complete sentence far more often than "No. 5" appears.
ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "approx", "fig", "inc", "ltd", "co", "uk",
})

# Abbreviations recognized only in their dotted spelling ("e.g.", "i.e.",
# "a.m.", "U.S."). Their dotless forms ("eg", "am", "us") are ordinary words
# that legitimately end sentences, so they qualify only when the trailing token
# actually carried internal periods.
DOTTED_ABBREVIATIONS = frozenset({"eg", "ie", "am", "pm", "us"})

# The trailing token before a sentence-final period, keeping any internal dots
# so "e.g." is seen whole rather than as a bare "g".
_TRAILING_WORD = re.compile(r"([A-Za-z.]+)\.$")


def is_real_boundary(candidate: str) -> bool:
    """Reject boundaries that fall inside an abbreviation."""
    match = _TRAILING_WORD.search(candidate.strip())
    if match is None:
        return True
    token = match.group(1)
    word = token.replace(".", "").lower()
    if word in ABBREVIATIONS:
        return False
    if "." in token and word in DOTTED_ABBREVIATIONS:
        return False
    return True


def split_sentences(buffer: str, flush_chars: int) -> tuple[list[str], str]:
    """Split off every complete sentence.

    Returns the finished sentences and whatever text remains unterminated,
    which the caller keeps buffering into. Past ``flush_chars`` with no
    boundary in sight, a run-on is released at a clause boundary anyway —
    otherwise a long sentence leaves the listener in silence indefinitely.
    """
    sentences: list[str] = []
    start = 0

    for match in BOUNDARY.finditer(buffer):
        end = match.end()
        candidate = buffer[start:end]
        if not is_real_boundary(candidate):
            continue
        text = candidate.strip()
        if text:
            sentences.append(text)
        start = end

    if sentences:
        return sentences, buffer[start:]

    if flush_chars > 0 and len(buffer) >= flush_chars:
        cut = flush_point(buffer, flush_chars)
        if cut > 0:
            head = buffer[:cut].strip()
            if head:
                return [head], buffer[cut:]

    return [], buffer


def flush_point(buffer: str, limit: int) -> int:
    """Choose where to cut an over-long buffer.

    Prefer a clause boundary — a comma, semicolon, colon, or dash. Cutting
    at an arbitrary word break is audible: "...compared to drinking" / "none."
    puts a pause in a place no speaker would, because the synthesizer gives
    each chunk its own falling intonation. A clause boundary is somewhere a
    person would naturally draw breath.
    """
    window = buffer[:limit]
    # Ignore boundaries in the first third: cutting there leaves a stub too
    # short to be worth speaking on its own ("Yes," as its own utterance).
    earliest = limit // 3

    for marker in ("; ", ", ", ": ", " — ", " - "):
        cut = window.rfind(marker)
        if cut > earliest:
            return cut + len(marker)

    cut = window.rfind(" ")
    # The same guard applies to the word-break fallback, which otherwise
    # happily returns the space after the first word.
    return cut if cut > earliest else limit


__all__ = [
    "ABBREVIATIONS",
    "BOUNDARY",
    "DOTTED_ABBREVIATIONS",
    "flush_point",
    "is_real_boundary",
    "split_sentences",
]
