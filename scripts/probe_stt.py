"""Probe the STT filter layer — hallucinations and repetition loops.

    uv run scripts/probe_stt.py

All scripted, no model, no microphone: these filters are pure text
functions shared by both Whisper engines, so a regression shows up here in
milliseconds instead of as a wall of "clear" in a live conversation
(observed 2026-08-20 — that exact failure is a fixture below).
"""

import sys

from ciel.stt.hallucinations import (
    collapse_repetition,
    looks_hallucinated,
    normalize,
)

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


def hallucination_checks() -> None:
    print("stock hallucinations:")
    check("empty is hallucinated", looks_hallucinated(""))
    check("pure punctuation is hallucinated", looks_hallucinated("..."))
    check("'Thank you.' is hallucinated", looks_hallucinated("Thank you."))
    check("a real sentence survives",
          not looks_hallucinated("Thank you for fixing the door."))
    echo = frozenset({normalize("A spoken conversation with Ciel.")})
    check("a prompt echo is hallucinated",
          looks_hallucinated("A spoken conversation with Ciel.", echo))
    check("a doubled prompt echo is hallucinated",
          looks_hallucinated(
              "A spoken conversation with Ciel. A spoken conversation with Ciel.",
              echo,
          ))


def loop_checks() -> None:
    print("repetition loops:")
    wall = "clear " * 300

    # The observed failure: real speech, then the wall.
    observed = "I'm going to use the same way to make a " + wall
    collapsed = collapse_repetition(observed)
    check("the 2026-08-20 wall collapses",
          collapsed == "I'm going to use the same way to make a clear clear")
    check("a head-and-wall transcript is not dropped whole",
          not looks_hallucinated(observed))

    # A wall with no head is noise, dropped entirely.
    check("a pure wall is hallucinated", looks_hallucinated(wall))
    check("a pure phrase wall is hallucinated",
          looks_hallucinated("I'm sorry. " * 40))

    # Multi-word loops collapse too, punctuation notwithstanding.
    check("a 2-gram loop collapses",
          collapse_repetition("He said " + "I'm sorry. " * 40 + "and left.")
          == "He said I'm sorry. I'm sorry. and left.")
    check("punctuation variants continue a run",
          collapse_repetition("okay so clear, clear. clear clear! clear clear clear")
          == "okay so clear, clear.")

    # What must NOT collapse: genuine speech with natural repetition.
    for text in (
        "no no no, that's not what I meant",
        "it was very very very very good",
        "set a timer for ten minutes",
        "the clear answer is to clear the cache and clear the logs",
    ):
        check(f"untouched: {text[:40]!r}", collapse_repetition(text) == text)
    check("five repeats stay (six is the bar)",
          collapse_repetition("go go go go go stop") == "go go go go go stop")

    # The tail after a loop survives.
    check("text after the loop survives",
          collapse_repetition("start " + "beep " * 20 + "then the end")
          == "start beep beep then the end")

    # Degenerate punctuation-only runs are runs too.
    check("a dash wall collapses",
          collapse_repetition("wait — — — — — — — — — —") == "wait — —")

    # Scale: a serious wall stays cheap.
    import time
    big = "one two three " + "clear " * 2000 + "four five"
    started = time.monotonic()
    result = collapse_repetition(big)
    elapsed = time.monotonic() - started
    check("a 2000-word wall collapses correctly",
          result == "one two three clear clear four five")
    check("and in well under a second", elapsed < 1.0)


def main() -> int:
    hallucination_checks()
    loop_checks()
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
