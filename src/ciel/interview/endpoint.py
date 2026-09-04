"""Knowing when the candidate has finished — patiently.

Ciel's own endpointer hears silence at half a second, because a
conversation with an assistant is quick. An interview answer is not: a
candidate thinks between sentences, and the worst thing the room can do
is take a breath for an ending. So this one waits two seconds, reads the
transcript at the deadline with the same eyes as the Cauchy hold
(``ciel.endpointing.trails_off``), and when the words trail off it waits
a while longer before it decides — once.

It is a pure state machine over a clock the caller supplies: transcripts
in, a deadline out, a decision when asked. No socket, no task, no audio.
The session drives it and the probe drives it with a fake clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ciel.endpointing import DANGLING_WORDS, trails_off


@dataclass
class Endpointer:
    silence_s: float = 2.0
    extend_s: float = 3.0
    max_answer_s: float = 240.0
    dangling: tuple[str, ...] = DANGLING_WORDS

    finals: list[str] = field(default_factory=list)
    partial: str = ""
    first_speech: float | None = None
    last_speech: float | None = None
    extended: bool = False
    done: bool = False
    """Set by ``answer.done``: the candidate said so; no timer needed."""

    # ── input ────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self.finals.clear()
        self.partial = ""
        self.first_speech = self.last_speech = None
        self.extended = False
        self.done = False

    def seed(self, text: str, now: float) -> None:
        """Start an answer with words already in hand — the merge after a
        cut-off, or a partial that arrived while the room was speaking."""
        self.reset()
        if text.strip():
            self.finals.append(text.strip())
            self.first_speech = self.last_speech = now

    def _heard(self, now: float) -> None:
        if self.first_speech is None:
            self.first_speech = now
        self.last_speech = now
        # New words after an extension: the extension did its job, and the
        # next pause gets a fresh judgement (and may extend again).
        self.extended = False

    def feed_partial(self, text: str, now: float) -> None:
        text = text.strip()
        if text == self.partial:
            # The recogniser repeats interims while the speaker pauses; a
            # repeat is not new speech and must not push the deadline.
            return
        self.partial = text
        if text:
            self._heard(now)

    def feed_final(self, text: str, now: float) -> None:
        text = text.strip()
        self.partial = ""
        if text:
            self.finals.append(text)
            self._heard(now)

    def mark_done(self) -> None:
        self.done = True

    # ── output ───────────────────────────────────────────────────────────────

    @property
    def text(self) -> str:
        parts = list(self.finals)
        if self.partial:
            parts.append(self.partial)
        return " ".join(parts).strip()

    @property
    def speaking_started(self) -> bool:
        return self.first_speech is not None

    def deadline(self) -> float | None:
        """When the next judgement is due, or None while nothing has been
        said (the candidate may think for as long as they like before they
        start; the cap runs only from their first word)."""
        if self.done:
            return 0.0
        if self.last_speech is None or self.first_speech is None:
            return None
        quiet = self.last_speech + self.silence_s + (self.extend_s if self.extended else 0.0)
        cap = self.first_speech + self.max_answer_s
        return min(quiet, cap)

    def decide(self, now: float) -> str:
        """``wait`` (not yet), ``extend`` (the pause reads mid-thought — wait
        once more and tell the page), or ``complete``."""
        if self.done:
            return "complete" if self.text else "wait"
        deadline = self.deadline()
        if deadline is None or now < deadline:
            return "wait"
        if self.first_speech is not None and now >= self.first_speech + self.max_answer_s:
            return "complete"
        if not self.extended and trails_off(self.text, self.dangling):
            self.extended = True
            return "extend"
        return "complete"


__all__ = ["Endpointer"]
