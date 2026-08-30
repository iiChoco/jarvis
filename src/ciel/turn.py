"""The turn contract — one shape for a user turn, whatever lane it rode in on.

Before this module, lane identity was smeared across three places: which
handler function got called, which system note was glued onto the prompt,
and which speaker label the transcript row carried. Four near-identical
handlers each hand-wired the same skeleton (record, local-command bypass,
held notes, brain stream, timers commit) around those differences. This
module writes the differences down as data — the registry below — and the
pipeline keeps exactly one copy of the skeleton (``Pipeline._run_turn``).

The labels here are load-bearing and must never drift: ``user``,
``you-confirm``, and ``user-web`` rows are Vigil's presence evidence,
``user-remote`` is deliberately evidence of the opposite, and the Chart
renders rows by these exact strings. The registry emits today's labels
byte-for-byte; ``scripts/probe_turns.py`` pins them.

Delivery stays out of this module on purpose: how sentences reach the
user (played, buffered into one text, or teed through the transcript tap)
is a :class:`TurnSink`, implemented next to the pipeline for the audio
lane and as small buffer classes for the text lanes. In the endgame the
hub drives the same ``_run_turn`` with a sink that writes wire frames —
which is the point of cutting here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

_REMOTE_NOTE = (
    "(System note — this message arrived over the Discord link: the user is "
    "away from this machine, and nothing you write will be spoken aloud. "
    "Your reply goes back to them as a text, so keep it text-shaped: short "
    "and plain. Nothing at the machine reaches them where they are — a "
    "timer or alarm armed now rings here, not on their phone, so arm one "
    "only for when they'll be back to hear it, and say as much.)\n\n"
)
"""Prefixed to every remote turn — the model's only way to know the room is
empty. Prefix, not transcript: the record keeps the user's raw words, the
same contract as the held-notes note."""


_REMOTE_PUBLIC_NOTE = (
    "(System note — this arrived as an @mention in a Discord server channel: "
    "the user is away from this machine, and your reply posts to that channel "
    "where OTHERS CAN READ IT. Keep it text-shaped and discreet — volunteer "
    "no personal details, memories, schedules, or private context beyond what "
    "the user's own message already put in the open, and offer to continue "
    "in DMs when a real answer would need them. Nothing you write is spoken "
    "aloud, and nothing at the machine reaches the user where they are.)\n\n"
)
"""The mention lane's variant of the note above. The audience is the
difference: a DM is the owner's eyes only, a channel is whoever is in it —
so this one trades the timer caveat for a discretion rule."""


_WEB_NOTE = (
    "(System note — this message arrived through the local GUI: the user is "
    "at this machine but typing because the room should stay quiet, and your "
    "reply is shown on screen, never spoken. Keep it text-shaped: short and "
    "plain.)\n\n"
)
"""Prefixed to every web turn, the remote note's contract: prefix, not
transcript — the record keeps the user's raw words."""


_WEB_MUTED_NOTE = (
    "(System note — this message arrived through the local GUI, and the "
    "speakers are muted: the user is somewhere that must stay silent (a "
    "class, a library). Your reply is shown on screen, never spoken, and "
    "nothing at this machine can make a sound right now — a timer or alarm "
    "armed now appears only as text on the screen, so say as much if you arm "
    "one. Keep it text-shaped: short and plain.)\n\n"
)
"""The web note while muted. The difference worth spelling out is the
timers: unmuted, a timer still rings at the machine; muted, its
announcement is text on a page the user may have stopped watching."""


@dataclass(frozen=True)
class TurnRequest:
    """One user turn, lane-tagged, as handed to ``Pipeline._run_turn``.

    ``channel`` is the lane's opaque reply route (a Discord channel; None
    everywhere else, and for a DM). ``public`` is whether the reply lands
    where others can read it — only a Discord guild channel today, and it
    is what swaps the system note and withholds the held Vigil notes.
    ``arrival_wall`` is the wall-clock twin of the queues' monotonic
    stamps: nothing consumes it yet, but the hub's clock discipline
    (hub-side gating only ever compares wall stamps it minted itself)
    starts by carrying it.
    """

    lane: str  # "voice" | "typed" | "web" | "discord"
    text: str
    channel: Any = None
    public: bool = False
    arrival_wall: float | None = None


@dataclass(frozen=True)
class LaneSpec:
    """The per-lane facts the shared skeleton consults.

    ``label`` is the transcript speaker (presence evidence, or its
    deliberate absence). ``origin`` is the parenthesized console tag
    ("typed", "discord #general"); None for the voice lane, whose rows
    are bare "you:". ``log_name`` is the lane's name in log lines
    ("remote", not "discord" — the strings predate the registry and logs
    grep the same forever); None means the lane writes its own turn line
    (the voice sink's first-speech split). ``held_notes`` is whether held
    Vigil notes ride into the prompt — everywhere but a public channel,
    which must never be where "your 2pm moved" lands. ``reload_ack`` is
    what a locally-handled "reload" says back on lanes whose usual
    answer — the spoken "Reloading." — happens in a room the user isn't
    watching. ``confirm_origin`` names the broker's remote-mode origin,
    None for lanes whose confirmations are voiced.
    """

    label: str
    origin: str | None
    log_name: str | None
    held_notes: bool = True
    reload_ack: str | None = None
    confirm_origin: str | None = None

    @property
    def console_tag(self) -> str:
        """The transcript-style console prefix for the user's row."""
        return "you" if self.origin is None else f"you ({self.origin})"


def lane_spec(req: TurnRequest) -> LaneSpec:
    """The registry: today's lane behavior, byte-for-byte."""
    if req.lane == "voice":
        return LaneSpec(label="user", origin=None, log_name=None)
    if req.lane == "typed":
        return LaneSpec(label="user", origin="typed", log_name="typed")
    if req.lane == "web":
        return LaneSpec(
            label="user-web",
            origin="web",
            log_name="web",
            reload_ack="Reloading.",
            confirm_origin="web",
        )
    if req.lane == "discord":
        origin = (
            f"discord #{getattr(req.channel, 'name', '?')}"
            if req.public
            else "discord"
        )
        return LaneSpec(
            label="user-remote",
            origin=origin,
            log_name="remote",
            held_notes=not req.public,
            reload_ack="Reloading.",
            confirm_origin="discord",
        )
    raise ValueError(f"unknown lane {req.lane!r}")


def prompt_note(req: TurnRequest, *, muted: bool) -> str:
    """The system note glued onto the prompt — the model's only way to
    know where the user is and where the reply lands. Empty for the lanes
    whose replies are spoken into the room the user is in."""
    if req.lane == "web":
        return _WEB_MUTED_NOTE if muted else _WEB_NOTE
    if req.lane == "discord":
        return _REMOTE_PUBLIC_NOTE if req.public else _REMOTE_NOTE
    return ""


class TurnSink(Protocol):
    """Where a turn's sentences go — the delivery half of a lane.

    The shared skeleton prints and records every sentence itself (the
    console and the transcript are lane-independent); the sink handles
    what varies: playing audio, buffering for a single text, or nothing
    at all because the transcript tap already delivers to the page. Any
    hook returning False aborts the stream — the voice sink's barge-in.

    ``confirm_send`` is the broker's remote sink for lanes whose
    confirmations travel the lane (None for voiced ones); the skeleton
    hands it to ``confirm.remote`` when the spec names an origin.
    """

    confirm_send: Any  # Callable[[str], Awaitable[None]] | None

    def gate(self) -> bool:
        """Checked before each yielded item; False stops consuming."""
        ...

    async def begin(self, started: float) -> None:
        """Before the brain stream starts (voice: arm the ack filler)."""
        ...

    async def escalation(self) -> bool:
        """The model handed the question to deep thought."""
        ...

    async def thinking(self, sentence: str) -> bool:
        """One reasoning sentence."""
        ...

    async def reply(self, sentence: str) -> bool:
        """One answer sentence."""
        ...

    async def finish(self, started: float) -> None:
        """After the stream and its contexts close, on the non-exception
        path: deliver anything buffered, log the turn line."""
        ...

    async def cleanup(self) -> None:
        """Always runs (the turn's finally): release anything held."""
        ...


__all__ = [
    "LaneSpec",
    "TurnRequest",
    "TurnSink",
    "lane_spec",
    "prompt_note",
]
