"""Fake-driven verification of Vigil — the proactive layer.

No audio, no model, no Quartz, no EventKit: the queue takes a temp file, the
policy takes every fact as an argument, the presence probe takes injected
readers, and the Witness guard is invoked directly with fake payloads — so
every branch of "when may Ciel speak unprompted" runs on scripted values,
the same way the confirmation choreography is verified. Run it directly:

    uv run python scripts/probe_vigil.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from ciel.brain.prompt import proactive_prompt
from ciel.brain.witness import UnattendedMode, WitnessGuard, witness_allowed
from ciel.config import Config, MCPServerConfig, ProactiveConfig, load_config
from ciel.memory.store import MemoryStore
from ciel.pipeline import proactive_valve
from ciel.proactive.events import EventQueue, ProactiveEvent
from ciel.proactive.policy import InterruptionPolicy, parse_hhmm
from ciel.proactive.presence import PresenceProbe, PresenceState
from ciel.proactive.watchers import WatcherHealth

CHECKS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(name)
    marker = "ok  " if ok else "FAIL"
    print(f"  {marker} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        sys.exit(1)


# ── fakes and fixtures ───────────────────────────────────────────────────────

def event(
    key: str,
    *,
    importance: int = 2,
    created: float = 100.0,
    expires: float | None = None,
    summary: str = "Standup starts soon.",
) -> ProactiveEvent:
    return ProactiveEvent(
        id=key,
        source="calendar",
        importance=importance,
        created_at=created,
        expires_at=expires,
        summary=summary,
        dedupe_key=key,
    )


PRESENT = PresenceState(
    screen_locked=False, seconds_since_input=1.0,
    seconds_since_conversation=None, present=True,
)
AWAY = PresenceState(
    screen_locked=True, seconds_since_input=9999.0,
    seconds_since_conversation=None, present=False,
)

NOON = 12 * 60


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vigil-probe-"))
    try:
        await run_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nall {len(CHECKS)} checks passed")


async def run_checks(tmp: Path) -> None:
    # ── the event queue ──────────────────────────────────────────────────────
    print("event queue:")
    path = tmp / "proactive.json"
    q = EventQueue(path, held_max_age_s=3600.0)

    e1 = event("cal:1:200", expires=200.0)
    check("push queues a fresh event", q.push(e1) and q.pending)
    check("push drops a pending duplicate", not q.push(event("cal:1:200")))
    popped = q.pop_next(now=150.0)
    check("pop_next returns the live event", popped is not None and popped.dedupe_key == "cal:1:200")
    check("pop_next empties pending", not q.pending)

    q.hold(e1)
    check("push drops a held duplicate", not q.push(event("cal:1:200")))
    taken = q.take_held(now=150.0)
    check("take_held delivers the live note", [e.dedupe_key for e in taken] == ["cal:1:200"])
    check("take_held consumes", q.take_held(now=150.0) == [])
    check("push drops a delivered duplicate", not q.push(event("cal:1:200")))

    e2 = event("cal:2:300", expires=300.0)
    e3 = event("cal:3:900", expires=900.0)
    q.push(e2)
    q.push(e3)
    popped = q.pop_next(now=350.0)
    check(
        "pop_next skips past an expired event to a live one",
        popped is not None and popped.dedupe_key == "cal:3:900",
    )
    check("an expired drop is remembered", not q.push(event("cal:2:300")))
    q.hold(popped)

    stale = event("work:old", created=100.0)
    q.hold(stale)
    live_after_age = q.take_held(now=100.0 + 3601.0)
    check(
        "over-age held notes die unspoken",
        "work:old" not in [e.dedupe_key for e in live_after_age],
    )
    check(
        "expired held notes die unspoken",
        "cal:3:900" not in [e.dedupe_key for e in q.take_held(now=1000.0)],
    )

    check("budget starts at zero", q.spoken_count("2026-08-18") == 0)
    q.mark_spoken(event("b:1"), now=500.0, date="2026-08-18")
    q.mark_spoken(event("b:2"), now=501.0, date="2026-08-18")
    check("spoken deliveries charge the budget", q.spoken_count("2026-08-18") == 2)
    check("midnight rolls the budget over", q.spoken_count("2026-08-19") == 0)
    q.mark_messaged(event("b:m1"), now=502.0, date="2026-08-19")
    check("texted deliveries charge their own budget", q.messaged_count("2026-08-19") == 1)
    check("the texting budget rolls over too", q.messaged_count("2026-08-20") == 0)

    # Persistence: a second queue over the same file is the re-exec case.
    q.push(event("cal:4:9000", expires=9000.0))
    q.hold(event("hold:1", created=8000.0))
    q.mark_spoken(event("b:3"), now=8000.0, date="2026-08-20")
    q2 = EventQueue(path, held_max_age_s=3600.0)
    check("pending survives reconstruction", q2.pending)
    check(
        "held notes survive reconstruction",
        [e.dedupe_key for e in q2.take_held(now=8001.0)] == ["hold:1"],
    )
    check("the budget survives reconstruction", q2.spoken_count("2026-08-20") == 1)
    check("the dedupe record survives reconstruction", not q2.push(event("b:3")))

    corrupt = tmp / "corrupt.json"
    corrupt.write_text("{not json")
    q3 = EventQueue(corrupt, held_max_age_s=3600.0)
    check("a corrupt file starts empty, not dead", not q3.pending and q3.push(event("c:1")))

    # ── quiet hours and the policy table ─────────────────────────────────────
    print("interruption policy:")
    check("parse_hhmm reads a clock time", parse_hhmm("22:00") == 1320)
    check("parse_hhmm rejects garbage", parse_hhmm("8") is None and parse_hhmm("25:00") is None)

    cfg = ProactiveConfig(
        quiet_hours_start="22:00", quiet_hours_end="08:00",
        max_spoken_per_day=2, min_importance_to_speak=2,
    )
    pol = InterruptionPolicy(cfg)
    check("quiet hours cover the late evening", pol.in_quiet_hours(23 * 60 + 30))
    check("quiet hours wrap past midnight", pol.in_quiet_hours(6 * 60))
    check("midday is not quiet", not pol.in_quiet_hours(NOON))
    check("the quiet window starts inclusive", pol.in_quiet_hours(1320))
    check("the quiet window ends exclusive", not pol.in_quiet_hours(8 * 60))
    same = InterruptionPolicy(replace(cfg, quiet_hours_start="09:00", quiet_hours_end="09:00"))
    check("start == end means no quiet window", not same.in_quiet_hours(9 * 60))
    broken = InterruptionPolicy(replace(cfg, quiet_hours_start="whenever"))
    check("an unparseable window disables itself", not broken.in_quiet_hours(NOON))

    live = event("p:1")

    def decide(ev=live, presence=PRESENT, spoken=0, now=500.0, minutes=NOON):
        return pol.decide(
            ev, presence=presence, spoken_today=spoken, now=now, local_minutes=minutes
        )

    check("a clear moment speaks", decide().action == "speak")
    check(
        "an expired event drops",
        decide(ev=event("p:2", expires=400.0), now=500.0).action == "drop",
    )
    check(
        "expiry outranks quiet hours (drop, not hold)",
        decide(ev=event("p:3", expires=400.0), now=500.0, minutes=1400).action == "drop",
    )
    check("quiet hours hold", decide(minutes=23 * 60).action == "hold")
    check(
        "routine importance holds",
        decide(ev=event("p:4", importance=1)).action == "hold",
    )
    check("absence holds", decide(presence=AWAY).action == "hold")
    check("a spent budget holds", decide(spoken=2).action == "hold")
    check(
        "urgency does not break the quiet window",
        decide(ev=event("p:5", importance=3), minutes=23 * 60).action == "hold",
    )

    # The away outlet: message only when urgent, wired, and within budget.
    def decide_away(ev, *, messaged=0, can=True):
        return pol.decide(
            ev, presence=AWAY, spoken_today=0, messaged_today=messaged,
            can_message=can, now=500.0, local_minutes=NOON,
        )

    check(
        "urgent + away + wired routes to message",
        decide_away(event("m:1", importance=3)).action == "message",
    )
    check(
        "away without wiring holds",
        decide_away(event("m:2", importance=3), can=False).action == "hold",
    )
    check(
        "timely-but-not-urgent never texts",
        decide_away(event("m:3", importance=2)).action == "hold",
    )
    check(
        "a spent texting budget holds",
        decide_away(event("m:4", importance=3), messaged=3).action == "hold",
    )
    check(
        "quiet hours hold texts too",
        pol.decide(
            event("m:5", importance=3), presence=AWAY, spoken_today=0,
            messaged_today=0, can_message=True, now=500.0, local_minutes=23 * 60,
        ).action == "hold",
    )

    # ── presence ─────────────────────────────────────────────────────────────
    print("presence:")
    pcfg = ProactiveConfig(
        presence_idle_s=300.0, conversation_presence_s=180.0, presence_mode="any"
    )
    at_desk = PresenceProbe(pcfg, lock_reader=lambda: False, idle_reader=lambda: 10.0)
    check("recent input at an unlocked screen is presence", at_desk.state(1000.0).present)

    locked = PresenceProbe(pcfg, lock_reader=lambda: True, idle_reader=lambda: 10.0)
    check("a locked screen is absence", not locked.state(1000.0).present)
    locked.note_conversation(900.0)
    check(
        "recent conversation is presence even locked (mode any)",
        locked.state(1000.0).present,
    )
    check(
        "conversation recency expires",
        not locked.state(900.0 + 181.0).present,
    )

    hid_only = PresenceProbe(
        replace(pcfg, presence_mode="hid"),
        lock_reader=lambda: True, idle_reader=lambda: 10.0,
    )
    hid_only.note_conversation(990.0)
    check("mode hid ignores conversation recency", not hid_only.state(1000.0).present)

    conv_only = PresenceProbe(
        replace(pcfg, presence_mode="conversation"),
        lock_reader=lambda: False, idle_reader=lambda: 1.0,
    )
    check("mode conversation ignores the keyboard", not conv_only.state(1000.0).present)
    conv_only.note_conversation(990.0)
    check("mode conversation trusts the mic", conv_only.state(1000.0).present)

    def boom() -> bool:
        raise RuntimeError("no quartz here")

    broken_readers = PresenceProbe(pcfg, lock_reader=boom, idle_reader=boom)
    state = broken_readers.state(1000.0)
    check(
        "failed readers degrade their signals, not the snapshot",
        not state.screen_locked and state.seconds_since_input == float("inf"),
    )
    broken_readers.note_conversation(990.0)
    check("conversation recency survives dead readers", broken_readers.state(1000.0).present)

    # ── the Witness rule ─────────────────────────────────────────────────────
    print("witness rule:")
    wcfg = Config(mcp={
        "gcal": MCPServerConfig(
            command="npx", tools=("list-events",), unattended=("list-events",)
        ),
    })
    allowed = witness_allowed(wcfg)
    for name in ("Read", "Glob", "Grep", "mcp__ciel__recall", "mcp__ciel__remember",
                 "mcp__ciel__read_messages", "mcp__gcal__list-events"):
        check(f"witness allows {name}", name in allowed)
    for name in ("Bash", "Write", "Edit", "WebSearch", "mcp__ciel__send_message",
                 "mcp__ciel__set_timer", "mcp__ciel__look_at_screen",
                 "mcp__gcal__create-event"):
        check(f"witness denies {name}", name not in allowed)

    mode = UnattendedMode()
    guard = WitnessGuard(mode, allowed)
    check(
        "disengaged, everything passes",
        await guard({"tool_name": "Bash"}, None, None) == {},
    )
    with mode.engage():
        check(
            "engaged, an observer passes",
            await guard({"tool_name": "Read"}, None, None) == {},
        )
        verdict = await guard({"tool_name": "mcp__ciel__send_message"}, None, None)
        check(
            "engaged, a send denies",
            verdict.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
        )
        with mode.engage():
            check("nested engagement stays engaged", mode.engaged)
        check("inner exit keeps the outer engaged", mode.engaged)
    check("exit disengages", not mode.engaged)
    try:
        with mode.engage():
            raise RuntimeError("turn blew up")
    except RuntimeError:
        pass
    check("an exception cannot leave the mode stuck", not mode.engaged)
    check("attended context is None", mode.context is None)
    with mode.engage("reflection"):
        check("engagement carries its label", mode.context == "reflection")
        with mode.engage("proactive"):
            check("the innermost label wins", mode.context == "proactive")
        check("inner exit restores the outer label", mode.context == "reflection")

    # ── watcher health: the failure-streak self-report ───────────────────────
    print("watcher health:")
    hq = EventQueue(tmp / "health.json", held_max_age_s=3600.0)
    health = WatcherHealth("calendar", hq, threshold=3)
    health.failed(100.0)
    health.failed(101.0)
    check("below the threshold, nothing is reported", not hq.pending)
    health.failed(102.0)
    check("the threshold pushes one report", hq.pending)
    report = hq.pop_next(103.0)
    check(
        "the report names the watcher, importance 1",
        report is not None and "calendar" in report.summary and report.importance == 1,
    )
    health.failed(104.0)
    health.failed(105.0)
    check("a continuing streak does not nag", not hq.pending)
    health.succeeded()
    health.failed(200.0)
    health.failed(201.0)
    health.failed(202.0)
    check("a fresh streak after recovery reports again", hq.pending)

    # ── wake-from-sleep catch-up ─────────────────────────────────────────────
    print("sleep catch-up:")
    from ciel.pipeline import IdleSchedule
    from ciel.proactive.calendar import CalendarWatcher

    sched = IdleSchedule(60.0, 600.0)
    sched.conversation_ended(100.0)
    sched.clear()
    check("clear() drops both armed deadlines", sched.due(1e9) is None)
    sched.conversation_ended(100.0)
    check("re-arming after clear works", sched.due(200.0) == "reflect")

    watcher = CalendarWatcher(
        replace(ProactiveConfig(), calendar_poll_s=999.0),
        EventQueue(tmp / "kick.json", held_max_age_s=3600.0),
    )
    scans: list[float] = []
    watcher._scan_sync = lambda: scans.append(0) or []  # type: ignore[method-assign]
    await watcher.start()
    await asyncio.sleep(0.05)
    check("the poll scans once at start", len(scans) == 1)
    watcher.kick()
    await asyncio.sleep(0.05)
    check("kick() ends the poll wait early", len(scans) == 2)
    await asyncio.sleep(0.05)
    check("no extra scan without another kick", len(scans) == 2)
    await watcher.close()

    # ── the morning brief's clock ────────────────────────────────────────────
    print("schedule watcher:")
    from ciel.proactive.watchers import ScheduleWatcher

    bq = EventQueue(tmp / "brief.json", held_max_age_s=3600.0)
    brief = ScheduleWatcher(replace(ProactiveConfig(), brief_time="07:45"), bq)
    # Build concrete instants on a fixed local date.
    def at(hour, minute):
        return time.mktime((2026, 8, 18, hour, minute, 0, 0, 0, -1))

    brief.check(at(7, 0))
    check("before its time, no brief", not bq.pending)
    brief.check(at(7, 45))
    check("at its time, the brief arms", bq.pending)
    brief.check(at(7, 46))
    popped_brief = bq.pop_next(at(7, 46))
    check(
        "the minutes after re-offer nothing (day dedupe)",
        popped_brief is not None and not bq.pending,
    )
    bq.drop(popped_brief, at(7, 47))
    brief.check(at(9, 0))
    check("a delivered brief stays delivered all day", not bq.pending)
    late = ScheduleWatcher(replace(ProactiveConfig(), brief_time="07:45"),
                           EventQueue(tmp / "brief2.json", held_max_age_s=3600.0))
    late.check(at(19, 30))
    check("a stale brief is skipped, not served at dinner", not late._queue.pending)
    check(
        "the brief event expires with its freshness window",
        popped_brief.expires_at is not None
        and popped_brief.expires_at == at(7, 45) + 4 * 3600,
    )

    check(
        "the message outlet prompt names the phone",
        "iMessage" in proactive_prompt("X.", outlet="message"),
    )
    check(
        "extra material lands in the prompt",
        "Today's calendar" in proactive_prompt("X.", extra="Today's calendar:\n- 9:00 AM — Standup"),
    )

    # ── the verification loop ────────────────────────────────────────────────
    print("read-back verification:")
    verify_ev = ProactiveEvent(
        id="v1", source="verify", importance=1, created_at=100.0,
        expires_at=2000.0, summary="Read-back check.", dedupe_key="verify:x:1",
    )
    check(
        "verify events route to a silent note turn",
        decide(ev=verify_ev, now=500.0).action == "note",
    )
    check(
        "verification ignores quiet hours (it disturbs nobody)",
        decide(ev=verify_ev, now=500.0, minutes=23 * 60).action == "note",
    )
    check(
        "an expired check still drops",
        decide(ev=verify_ev, now=3000.0).action == "drop",
    )
    check(
        "the note outlet prompt aims at the next conversation",
        "next conversation" in proactive_prompt("X.", outlet="note"),
    )

    from ciel.brain.recorder import ActionRecorder
    from ciel.config import JournalConfig
    from ciel.journal import ActionJournal

    journal = ActionJournal(JournalConfig(dir=tmp / "undo"))
    journal.ensure()
    emitted: list[str] = []
    recorder = ActionRecorder(
        journal, frozenset({"mcp__ciel__send_message"}),
        verify_emitter=lambda tool, args: emitted.append(tool),
    )
    await recorder.after(
        {"tool_name": "mcp__ciel__send_message",
         "tool_input": {"to": "x", "text": "hi"}, "tool_response": "sent"},
        "t1", None,
    )
    check("a confirmed send emits a verify event", emitted == ["mcp__ciel__send_message"])
    await recorder.after(
        {"tool_name": "Bash", "tool_input": {"command": "ls"},
         "tool_response": "ok"}, "t2", None,
    )
    check("journal-only tools do not emit checks", emitted == ["mcp__ciel__send_message"])

    # ── the work watcher ─────────────────────────────────────────────────────
    print("work watcher:")
    import subprocess

    from ciel.proactive.work import WorkWatcher

    wq = EventQueue(tmp / "work-events.json", held_max_age_s=3600.0)
    ww = WorkWatcher(tmp / "watches.json", wq)
    target = tmp / "export.zip"
    ww.add_file(str(target), "The export has finished.", 60.0)
    ww.check(1000.0)
    check("an absent file keeps its watch", not wq.pending and len(ww.active()) == 1)
    ww2 = WorkWatcher(tmp / "watches.json", wq)
    check("watches survive reconstruction", len(ww2.active()) == 1)
    target.write_text("x")
    ww2.check(1001.0)
    done = wq.pop_next(1001.0)
    check(
        "the file appearing files the labeled event",
        done is not None and done.summary == "The export has finished.",
    )
    check("a resolved watch is gone", not ww2.active())

    ww2.add_file(str(tmp / "never.zip"), "The never-job is done.", 1.0)
    ww2.check(time.time() + 120.0)
    timed_out = wq.pop_next(time.time() + 121.0)
    check(
        "a missed deadline files its own honest event",
        timed_out is not None and "never finished" in timed_out.summary,
    )

    proc = subprocess.Popen(["true"])
    proc.wait()
    ww2.add_pid(proc.pid, "The background job has exited.", 60.0)
    ww2.check(time.time())
    pid_done = wq.pop_next(time.time())
    check(
        "a vanished pid completes its watch",
        pid_done is not None and pid_done.summary == "The background job has exited.",
    )
    import os as _os
    ww2.add_pid(_os.getpid(), "Should not fire.", 60.0)
    ww2.check(time.time())
    check("a live pid keeps its watch", not wq.pending and len(ww2.active()) == 1)
    check(
        "arming a watch is denied unattended",
        "mcp__ciel__watch_for_completion" not in allowed,
    )

    # ── memory write provenance ──────────────────────────────────────────────
    print("memory provenance:")
    mstore = MemoryStore(tmp / "memory")
    mstore.write("User's name is Choco", "The user is called Choco.", "identity")
    saved = mstore.get("User's name is Choco")
    check(
        "a plain write reads back as conversation",
        saved is not None and saved.context == "conversation",
    )
    mstore.write(
        "The export job finished", "Observed at 3am: export.zip exists, 2.1 GB.",
        "fact", context="proactive",
    )
    observed = mstore.get("The export job finished")
    check(
        "a proactive write keeps its provenance through the file",
        observed is not None and observed.context == "proactive",
    )
    check(
        "procedure is a valid memory kind",
        mstore.write("How the brief is structured", "Calendar first.", "procedure").kind
        == "procedure",
    )

    # ── the one-way valve and the prompt ─────────────────────────────────────
    print("one-way valve:")
    check("a bare SKIP is a decline", proactive_valve("SKIP") == "SKIP")
    check("punctuation and case are tolerated", proactive_valve('"Skip."') == "SKIP")
    check("a bare HOLD de-escalates", proactive_valve("hold.") == "HOLD")
    check("SKIP inside a sentence is a reply", proactive_valve("Skip the meeting.") is None)
    check("a real reply passes through", proactive_valve("Your 2pm is in ten minutes.") is None)
    check("an empty reply is not a valve token", proactive_valve("") is None)
    prompt = proactive_prompt("Meeting {weird} starts at 2:00 PM.")
    check("watcher text with braces cannot break the prompt", "{weird}" in prompt)
    check("the prompt defines both de-escalations", "SKIP" in prompt and "HOLD" in prompt)

    # ── the config section round-trips ───────────────────────────────────────
    print("config round-trip:")
    toml_path = tmp / "config.toml"
    toml_path.write_text(
        """
[proactive]
enabled = true
quiet_hours_start = "23:15"
max_spoken_per_day = 3
presence_mode = "conversation"
calendars = ["Work", "Home"]

[mcp.gcal]
command = "npx"
tools = ["list-events"]
unattended = ["list-events"]
"""
    )
    loaded = load_config(toml_path)
    check("proactive.enabled loads", loaded.proactive.enabled is True)
    check("quiet_hours_start loads", loaded.proactive.quiet_hours_start == "23:15")
    check("max_spoken_per_day loads", loaded.proactive.max_spoken_per_day == 3)
    check("presence_mode loads", loaded.proactive.presence_mode == "conversation")
    check("calendars load as a tuple", loaded.proactive.calendars == ("Work", "Home"))
    check("defaults survive a partial table", loaded.proactive.calendar_lead_minutes == 10.0)
    check(
        "mcp unattended loads as a tuple",
        loaded.mcp["gcal"].unattended == ("list-events",),
    )


if __name__ == "__main__":
    asyncio.run(main())
