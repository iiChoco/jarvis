"""Making written text speakable.

The gap this closes: a model writes for a reader, and a speech engine reads
what is literally on the page. Most of the time those agree. They stop agreeing
on words with no vowels — "hmm", "mm-hm", "shh". Piper phonemizes through
espeak-ng, which has no dictionary entry for those, so it falls back to
spelling them out: "hmm" comes out as the letters *aitch em em*, which is
baffling to listen to and immediately breaks the illusion of speech.

For the hum family the fix is to *respell to a canonical form*, not strip:
measured on the shipped voice (lessac-medium), piper renders "hmm" as an
actual 0.48s hum — against 0.91s for genuinely spelled-out letters — and
"mm-hmm" and "mmm" likewise. Stripping them made Ciel silently swallow every
"Hmm," which reads as her not having heard at all. Variant spellings the
model produces ("Hm", "Hmmmm", "mhm") are normalized onto the forms verified
to hum. The truly unpronounceable ("shh", "tsk", "pfft") are still stripped.

This runs on the way to the speaker only. The transcript, the logs, and the
model's own context keep the original text.
"""

from __future__ import annotations

import re

# The hum family, normalized onto spellings verified to voice correctly on
# the shipped piper voice. Order matters: the mhm pattern spans a hyphen the
# plain h+m+ pattern would otherwise split.
_RESPELL: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bm+-?h+m+\b", re.IGNORECASE), "mm-hmm"),
    (re.compile(r"\bh+m+\b", re.IGNORECASE), "hmm"),
    # Hyphen guards keep this off the halves of an already-canonical
    # "mm-hmm" produced by the rule above. The digit-space guard keeps it off
    # the unit "mm": "50 mm" is a measurement, not a hum, and respelling it to
    # "50 mmm" makes the voice hum a number's units.
    (re.compile(r"(?<!-)(?<!\d\s)\bm{2,}\b(?!-)", re.IGNORECASE), "mmm"),
]

# Still vowel-free and still unverified as hums — stripped as before.
# Anything with a vowel ("ah", "oh", "uh", "um") espeak already pronounces
# correctly and is left alone.
_UNSPEAKABLE = re.compile(
    r"\b(?:sh{2,}|ts+k+(?:-ts+k+)*|pf+t+)\b",
    re.IGNORECASE,
)

# Punctuation orphaned by the removal: a leading ", " or the doubled comma in
# "Well, hmm, yes" once the middle word is gone.
_ORPHAN_LEAD = re.compile(r"^\s*[,;:—–-]+\s*")
_DOUBLED_PUNCT = re.compile(r"\s*,\s*(?=[,.;:!?])")
_EXTRA_SPACE = re.compile(r"\s{2,}")


def speakable(text: str) -> str:
    """Strip what a speech engine would mangle, and tidy up after itself.

    Returns "" if nothing but stripped interjections remain — callers already
    treat an empty string as "nothing to say", so a sentence that was only
    "Shh." is silently skipped rather than spelled out. (A lone "Hmm." is
    *respelled* and hummed, not skipped — see the module docstring.)
    """
    respelled = text
    for pattern, canonical in _RESPELL:
        respelled = pattern.sub(canonical, respelled)

    cleaned = _UNSPEAKABLE.sub("", respelled)
    if cleaned == text:
        return text
    if cleaned == respelled:
        # Only respelling happened — no words were removed, so there is no
        # orphaned punctuation to tidy and nothing to re-capitalize.
        return respelled

    cleaned = _DOUBLED_PUNCT.sub("", cleaned)
    cleaned = _EXTRA_SPACE.sub(" ", cleaned)
    cleaned = _ORPHAN_LEAD.sub("", cleaned).strip()

    # Left with only punctuation — there was no sentence here, just a noise.
    if not re.search(r"\w", cleaned):
        return ""

    # "hmm, that's interesting" loses its capital along with the word.
    return cleaned[0].upper() + cleaned[1:]


__all__ = ["speakable"]
