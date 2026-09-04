"""Reading a transcript for whether the speaker was done.

The endpointer, wherever it runs, can only prove a *pause*: the mic went
quiet for long enough. Whether the pause was the end of a thought or a
breath in the middle of one is a judgement about the words, and it is the
same judgement on the Mac's whisper transcript and on a browser's — so it
lives here, a pure function with no audio, no config object, and no
opinion about who is listening. The pipeline's Cauchy hold and the
interview room's patient endpointer both call it.
"""

from __future__ import annotations

import re
from typing import Iterable

DANGLING_WORDS: tuple[str, ...] = (
    "um", "umm", "uh", "uhh", "er", "erm", "hmm", "hm",
    "and", "or", "but", "because", "the", "a", "an",
    "my", "your", "our", "their", "his", "to", "of",
)
"""Words that can't end a thought — the default for callers with no
config of their own. ``AudioConfig.dangling_words`` carries the same list
for the Mac, where it is user-tunable."""


def trails_off(heard: str, dangling: Iterable[str] = DANGLING_WORDS) -> bool:
    """Whether a transcript ends mid-thought.

    Read pessimistically:

    * a trailing ellipsis is the transcriber itself saying "trailed off".
    * ``?``/``!`` are always complete — transcribers don't punctuate
      fragments as questions.
    * a period is trusted unless the last word is one that can't end a
      thought (``dangling``): whisper appends periods to fragments out of
      habit, and "check my." is not a closed sentence.
    * no closing punctuation at all means the transcriber didn't hear a
      finished sentence either. This branch is what catches the pause
      after an "um" — fillers are stripped from transcripts, so a hold
      that waits to *see* one misses exactly the pauses it exists for.
    """
    text = heard.rstrip()
    if text.endswith(("...", "…")):
        return True
    if text.endswith(("?", "!")):
        return False
    words = text.split()
    if not words:
        return False
    last = re.sub(r"[^a-z']+", "", words[-1].lower())
    if text.endswith("."):
        return last in set(dangling)
    return True


__all__ = ["DANGLING_WORDS", "trails_off"]
