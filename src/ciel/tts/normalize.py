"""Making written text speakable.

The gap this closes: a model writes for a reader, and a speech engine reads
what is literally on the page. Most of the time those agree. They stop agreeing
on words with no vowels — "hmm", "mm-hm", "shh". Piper phonemizes through
espeak-ng, which has no dictionary entry for those, so it falls back to
spelling them out: "hmm" comes out as the letters *aitch em em*, which is
baffling to listen to and immediately breaks the illusion of speech.

The fix is to strip them rather than respell them. A phonetic spelling that
happens to hum on one voice is not guaranteed to on another, and a wrong guess
here is the same failure again with extra steps. Dropping the interjection
costs a little warmth and always sounds like language.

This runs on the way to the speaker only. The transcript, the logs, and the
model's own context keep the original text.
"""

from __future__ import annotations

import re

# Vowel-free interjections, matched whole-word and case-insensitively:
#   hm, hmm, hmmm...   mm, mmm...   mhm, mm-hm, uh-huh's cousin
#   shh, sh...         tsk, tsk-tsk        pfft
# Deliberately conservative. Anything with a vowel ("ah", "oh", "uh", "um")
# espeak already pronounces correctly and is left alone.
_UNSPEAKABLE = re.compile(
    r"\b(?:h+m+|m+-?h+m+|m{2,}|sh{2,}|ts+k+(?:-ts+k+)*|pf+t+)\b",
    re.IGNORECASE,
)

# Punctuation orphaned by the removal: a leading ", " or the doubled comma in
# "Well, hmm, yes" once the middle word is gone.
_ORPHAN_LEAD = re.compile(r"^\s*[,;:—–-]+\s*")
_DOUBLED_PUNCT = re.compile(r"\s*,\s*(?=[,.;:!?])")
_EXTRA_SPACE = re.compile(r"\s{2,}")


def speakable(text: str) -> str:
    """Strip what a speech engine would mangle, and tidy up after itself.

    Returns "" if nothing but interjections remain — callers already treat an
    empty string as "nothing to say", so a sentence that was only "Hmm." is
    silently skipped rather than spelled out.
    """
    cleaned = _UNSPEAKABLE.sub("", text)
    if cleaned == text:
        return text

    cleaned = _DOUBLED_PUNCT.sub("", cleaned)
    cleaned = _EXTRA_SPACE.sub(" ", cleaned)
    cleaned = _ORPHAN_LEAD.sub("", cleaned).strip()

    # Left with only punctuation — there was no sentence here, just a noise.
    if not re.search(r"\w", cleaned):
        return ""

    # "hmm, that's interesting" loses its capital along with the word.
    return cleaned[0].upper() + cleaned[1:]


__all__ = ["speakable"]
