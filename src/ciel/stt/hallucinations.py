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

import logging
import re

log = logging.getLogger(__name__)

# Matched on the whole normalized transcript, so a genuine "thank you" inside
# a real sentence is untouched. Entries MUST be in normalized form —
# lowercase, no punctuation (see `normalize`) — or they can never match:
# earlier entries like "bye." and "!" were dead weight, their punctuation
# stripped from every transcript before the comparison. Pure-punctuation
# transcripts ("." , "?") normalize to "" and are already caught below.
_HALLUCINATIONS = frozenset({
    "you", "thank you", "thanks for watching", "thank you for watching",
    "bye", "okay", "ok",
    "please subscribe", "subtitles by the amaraorg community",
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
    words = normalized.split()
    loop = _loop_at(words, 0)
    if loop is not None and loop[1] == len(words):
        # The whole transcript is one short phrase repeated into a wall —
        # the decoder looped over noise with no real speech around it.
        # There is nothing here to salvage; a head-and-loop transcript
        # (real words, then the wall) is collapse_repetition's case, not
        # this one.
        return True
    # An echoed prompt is only a hallucination when it is the *entire*
    # transcript. If the user genuinely said those words as part of a longer
    # sentence, that sentence survives.
    return any(_is_repetition_of(normalized, echo) for echo in echoes)


# ── repetition loops ─────────────────────────────────────────────────────────
# Whisper's other reliable failure: the decoder locks onto a short n-gram and
# emits it until the token budget runs out — "clear clear clear ..." several
# hundred times, observed live 2026-08-20 on large-v3-turbo, sailing past
# both condition_on_previous_text=False and the temperature fallback (the
# retry can loop too). The walls are unmistakably degenerate: no human
# utterance repeats the same n-gram this many times consecutively, so a
# deterministic collapse is safe where a decoder knob is merely hopeful.

_LOOP_NGRAM_MAX = 4
"""Longest phrase checked for looping. Observed loops are 1-3 words
("clear", "I'm sorry.", "and so on and"); 4 buys margin."""

_LOOP_REPEATS = 6
"""Consecutive copies that count as a loop. Genuine speech says "no no no"
and even "very very very very"; six in a row is the decoder talking."""


def _loop_at(norm: list[str], i: int) -> tuple[int, int] | None:
    """The shortest n-gram loop starting at word ``i``, as ``(n, end)``.

    ``end`` is the index one past the run. Shortest first, so a wall of
    "clear" reads as a 1-gram loop rather than a 2-gram one — the collapse
    keeps ``2 * n`` words, and the smallest n keeps the least debris.
    """
    for n in range(1, _LOOP_NGRAM_MAX + 1):
        if i + n * _LOOP_REPEATS > len(norm):
            return None
        gram = norm[i:i + n]
        j = i + n
        reps = 1
        while norm[j:j + n] == gram:
            reps += 1
            j += n
        if reps >= _LOOP_REPEATS:
            return n, j
    return None


def collapse_repetition(text: str) -> str:
    """Collapse decoder repetition loops, keeping everything real.

    "I'm going to make a clear clear clear ... clear" (300 words) becomes
    "I'm going to make a clear clear": the head survives untouched, two
    copies of the looping phrase remain as evidence that *something* was
    said there, and the wall is gone — before it can be held as a partial
    thought, merged into the next utterance, or shipped to the model as a
    thousand tokens of noise. Words are compared normalized (case and
    punctuation ignored, so "clear," continues a "clear" run) but emitted
    as transcribed. Text without a loop passes through unchanged.
    """
    words = text.split()
    if len(words) < _LOOP_REPEATS:
        return text
    norm = [normalize(w) or w for w in words]
    out: list[str] = []
    i = 0
    collapsed = False
    while i < len(words):
        loop = _loop_at(norm, i)
        if loop is not None:
            n, end = loop
            out.extend(words[i:i + 2 * n])
            i = end
            collapsed = True
        else:
            out.append(words[i])
            i += 1
    if not collapsed:
        return text
    result = " ".join(out)
    log.info(
        "collapsed a repetition loop: %d words -> %d (%r)",
        len(words), len(out), result[:80],
    )
    return result


__all__ = ["collapse_repetition", "looks_hallucinated", "normalize"]
