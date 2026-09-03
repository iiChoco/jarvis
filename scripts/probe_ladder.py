"""Probe the turn-slot arbiter — the priority ladder as a table.

    uv run scripts/probe_ladder.py

``pick_next`` is the frame loop's scheduling contract extracted into a
pure function; in the endgame the hub's arbiter runs the same ladder
with no microphone anywhere. This drives every rank and every guard of
the table directly — no pipeline, no clock, no audio — so a reordering
(a lane quietly outranking a confirmation, Vigil claiming a slot from a
live listening window) fails here before it fails as a misbehavior
someone has to notice by ear.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ciel.schedule import Snapshot, Source, State, pick_next

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


W, L, B = State.WAITING, State.LISTENING, State.BUSY

EVERYTHING = dict(
    confirm_active=True,
    timers_due=True,
    typed_pending=True,
    web_pending=True,
    remote_pending=True,
    vigil_ready=True,
)


def main() -> int:
    print("the ranks")
    check(
        "a pending confirmation outranks everything, even from BUSY",
        pick_next(Snapshot(state=B, **EVERYTHING)) is Source.CONFIRM,
    )
    all_but_confirm = {**EVERYTHING, "confirm_active": False}
    check(
        "timers outrank every lane",
        pick_next(Snapshot(state=W, **all_but_confirm)) is Source.TIMERS,
    )
    check(
        "a spoken turn already heard outranks every queue but timers",
        pick_next(
            Snapshot(state=W, voice_pending=True, typed_pending=True,
                     web_pending=True, remote_pending=True, vigil_ready=True)
        ) is Source.VOICE
        and pick_next(Snapshot(state=W, timers_due=True, voice_pending=True))
        is Source.TIMERS,
    )
    check(
        "the voice lane claims from a listening window, never over a turn",
        pick_next(Snapshot(state=L, voice_pending=True, held_thought=True))
        is Source.VOICE
        and pick_next(Snapshot(state=B, voice_pending=True)) is Source.NONE,
    )
    check(
        "the keyboard outranks the page, the phone, and the machine",
        pick_next(
            Snapshot(state=W, typed_pending=True, web_pending=True,
                     remote_pending=True, vigil_ready=True)
        ) is Source.TYPED,
    )
    check(
        "the page outranks the phone and the machine",
        pick_next(
            Snapshot(state=W, web_pending=True, remote_pending=True,
                     vigil_ready=True)
        ) is Source.WEB,
    )
    check(
        "the phone outranks the machine",
        pick_next(Snapshot(state=W, remote_pending=True, vigil_ready=True))
        is Source.REMOTE,
    )
    check(
        "the machine goes last",
        pick_next(Snapshot(state=W, vigil_ready=True)) is Source.VIGIL,
    )
    check(
        "nothing pending: the state machine owns the frame",
        pick_next(Snapshot(state=W)) is Source.NONE,
    )

    print("\nthe quiet rule (timers, keyboard, page)")
    for name, snap in [
        ("BUSY", Snapshot(state=B, timers_due=True, typed_pending=True,
                          web_pending=True)),
        ("mid-utterance", Snapshot(state=L, endpointer_speaking=True,
                                   timers_due=True, typed_pending=True,
                                   web_pending=True)),
        ("a held partial thought", Snapshot(state=L, held_thought=True,
                                            timers_due=True, typed_pending=True,
                                            web_pending=True)),
    ]:
        check(f"{name} silences the quiet lanes", pick_next(snap) is Source.NONE)
    check(
        "a quiet LISTENING window lets the keyboard claim the slot",
        pick_next(Snapshot(state=L, typed_pending=True)) is Source.TYPED,
    )

    print("\nthe WAITING-only rule (phone, machine)")
    check(
        "a follow-up window outranks the phone",
        pick_next(Snapshot(state=L, remote_pending=True)) is Source.NONE,
    )
    check(
        "a follow-up window outranks the machine",
        pick_next(Snapshot(state=L, vigil_ready=True)) is Source.NONE,
    )
    check(
        "a running turn outranks the phone",
        pick_next(Snapshot(state=B, remote_pending=True)) is Source.NONE,
    )

    print("\nthe confirmation exception")
    check(
        "a confirmation is served even mid-utterance",
        pick_next(Snapshot(state=L, endpointer_speaking=True,
                           confirm_active=True)) is Source.CONFIRM,
    )

    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
