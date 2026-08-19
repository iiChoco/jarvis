"""Whisper hallucination filtering, shared by every Whisper-family engine.

Whisper reliably hallucinates a small set of stock phrases when handed audio
that contains no speech — subtitle-corpus artefacts baked in during training.
Endpointing means we mostly avoid feeding it silence, but breath noise and
door slams still get through, and "Thank you." arriving as a user turn is
both baffling and expensive. The failure mode belongs to the *weights*, not
the runtime, so faster-whisper and mlx-whisper need the identical filter —
which is why it lives here rather than in either engine module.
"""

from __future__ import annotations

import re

# Matched on the whole normalized transcript, so a genuine "thank you" inside
# a real sentence is untouched. Entries MUST be in normalized form —
# lowercase, no punctuation (see `normalize`) — or they can never match:
# earlier entries like "bye." and "!" were dead weight, their punctuation
# stripped from every transcript before the comparison. Pure-punctuation
# transcripts ("." , "?") normalize to "" and are already caught below.
_HALLUCINATIONS = frozenset({
    "you", "thank you", "thanks for watching", "thank you for watching",
    "bye", "okay", "ok",
    "please subscribe", "subtitles by the amara.org community",
    "transcription by castingwords",
})

_PUNCT = re.compile(r"[^\w\s]")


def normalize(text: str) -> str:
    return " ".join(_PUNCT.sub("", text).lower().split())


def _is_repetition_of(normalized: str, phrase: str) -> bool:
    """True if ``normalized`` is ``phrase`` repeated one or more times.

    Whole-word comparison, and repetition-aware because a prompt echo often
    arrives doubled or tripled rather than once.
    """
    words, phrase_words = normalized.split(), phrase.split()
    if not phrase_words or not words or len(words) % len(phrase_words):
        return False
    step = len(phrase_words)
    return all(
        words[i:i + step] == phrase_words for i in range(0, len(words), step)
    )


def looks_hallucinated(text: str, echoes: frozenset[str] = frozenset()) -> bool:
    normalized = normalize(text)
    if not normalized:
        return True
    if normalized in _HALLUCINATIONS:
        return True
    # An echoed prompt is only a hallucination when it is the *entire*
    # transcript. If the user genuinely said those words as part of a longer
    # sentence, that sentence survives.
    return any(_is_repetition_of(normalized, echo) for echo in echoes)


__all__ = ["looks_hallucinated", "normalize"]
