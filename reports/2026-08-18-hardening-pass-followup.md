# Follow-up review: hardening pass, second look (2026-08-18)

Scope: same uncommitted working-tree diff at `~/jarvis`, now grown to 33 files,
~1071 insertions / 232 deletions, still on top of commit `8fb6652`.

This is a follow-up to `2026-08-18-hardening-pass-review.md`. Read that first — it
covers the bulk of the pass. This document records (1) the status of the four issues
raised there, and (2) what is new since, with findings.

Summary: the four issues are all still open; issue B has grown a second consumer.
The new material is mostly sound, with one real concern around the Contacts framework
lookup.

---

## 1. Status of the four open issues

### A. Glob over-deny in `shellguard._forbidden_component` — UNTOUCHED

`src/ciel/brain/shellguard.py` lines 136-155 are byte-identical. `part.strip("*?[]")`
still only skips a bare `*`, so `ls -d .*` gives `part=".*"` → strip → `"."` (truthy)
→ `fnmatch(".ssh", ".*")` → True → `classify` returns `deny` at lines 238-240, the
tier with no confirmation escape.

`scripts/probe_shellguard.py` gained glob cases, but only the benign
`grep -r foo *.py` — so the false positive is now untested as well as unfixed.

The related over-deny on `command -v sudo` / `time sudo --version` via
`_effective_tokens` also stands.

### B. `Player.play` conflates barge-in with device loss — UNTOUCHED, AND DUPLICATED

`play` in `src/ciel/audio/output.py` still returns a bare `bool`. The `device_lost`
flag exists but is local, used only to skip `abort()/start()` on a closed stream
(line 157). The docstring now openly documents the conflation rather than resolving it.

Worse, the diff added a *second* consumer with the same misreading:
`Pipeline._respond_stream` (`src/ciel/pipeline.py:941-955`), the deep-thought
escalation announcement, now treats `not completed` as barge-in — sets `_interrupted`,
cancels the confirmation, calls `brain.interrupt()`, prints "(interrupted)".

So an unplugged output device kills the turn and misreports the cause in two places
now (the other is line 1000). A `device_lost` signal or a small result enum is still
the fix.

### C. `Player.stop()` outside `_play_lock` — UNTOUCHED, now declared intentional

New comment at `output.py:42-47` says "stop() stays outside the lock: barge-in must be
able to interrupt the play that holds it." That reasoning is correct, but it does not
address the actual failure: a `stop()` landing while a second `play()` is queued on the
lock is lost, because the queued call does `self._stop.clear()` at line 112 and speaks
anyway.

A stop-generation counter, or a "stop requested at" timestamp compared against the
play's start, would keep both properties.

### D. Broker phase drops to IDLE early — UNCHANGED, now deliberate and well-motivated

`VoiceConfirmBroker._announce` (`src/ciel/confirm.py:335-350`) still sets
`_phase = IDLE` before playing the closing line, but for a stated reason: a typed line
during "Okay, skipping it." is the next request, not an answer. That is a defensible
trade. The residual — mic frames route to the normal barge-in path while the broker is
still speaking — is the same benign state as before. Consider this closed unless it
misbehaves in practice.

Related additions here are correct: the `attempt < attempts - 1` guard stops the broker
asking "Yes or no?" with nothing left to listen with, and the new `asked_at` timestamp
plus `deque[tuple[float, str]]` in the pipeline correctly stops a pre-question typed
line being eaten as an answer.

---

## 2. New material since the first report

### Contacts via pyobjc — the one worth attention

`pyproject.toml` / `uv.lock` add `pyobjc-framework-Contacts>=10.1`, backing a new
`MessagesClient._contacts_framework_sync` (`src/ciel/messages/imessage.py:334-413`)
that replaces the AppleScript-first contact lookup. The lock entry is consistent.
Three concerns:

- **The fallback isn't load-bearing.** Only `MessagesUnavailable` is caught at the call
  site (lines 464-472). Any pyobjc-level failure — `objc.error`, an `AttributeError`
  from a missing symbol on older bindings, `TypeError` if `phoneNumbers()` returns
  `None` — escapes and kills the turn, bypassing the AppleScript fallback the design
  exists for. Broadening that `except` to `Exception` would fix it.
- **TCC termination cannot be caught.** Calling a TCC-protected Contacts API from a
  process whose bundle lacks `NSContactsUsageDescription` is terminated by the OS
  (SIGABRT), not raised — no `try` helps. Worth verifying under the real launch path
  (`python -m ciel`), because it would be an unrecoverable startup-class crash rather
  than a graceful fallback.
- **Run-loop assumption.** `requestAccessForEntityType_completionHandler_` blocks a
  `to_thread` worker on `answered.wait(script_timeout_s)`. Correct in principle, since
  the handler fires on a private queue — but if it ever needs a main run loop this
  silently degrades to a timeout on every first launch. The timeout bound keeps it
  non-fatal.

### `stt/__init__.py` `build_stt`

Switching to `importlib.util.find_spec("mlx_whisper")` is a genuine fix: the old
`try/except ImportError` around the constructor could never fire, since `MlxWhisperSTT`
imports lazily in `warm_up`. The new comment overstates it, though — a
*present-but-broken* mlx install still passes `find_spec`, and its `ImportError`
surfaces from `warm_up` with no fallback left. If that case matters, `warm_up`'s import
needs its own guard.

### Whisper temperature ladders

`local_whisper.py` and `local_mlx.py` both now pass a two-step fallback ladder.
`faster_whisper.transcribe` takes a list; `mlx_whisper.transcribe` accepts a tuple and
indexes through it in its `temperature_fallback` logic. Both fine. Note the two engines'
behavior on degenerate repetition now differs slightly from library defaults — stated
intent, not a bug.

### Correct as far as I can tell

- `HotkeyWake` now holds only a bool armed by the pipeline's single stdin reader
  (`pipeline._read_stdin`, lines 763-767) — properly resolves the two-readers-of-fd-0
  bug flagged in the first report.
- `Transcript.rotate` is now actually called from `Pipeline._rotate_session`, after
  `_record("event", "session rotated")` — matches the ordering previously verified.
- `commit_pending` wrapped in try/except in both `finally` blocks. Real crash path: an
  `OSError` there was re-raised by `turn.result()`.
- `_run_reflection` now interrupts the brain on `asyncio.TimeoutError`.
- `SourceWatcher._snapshot` per-file `stat` guard fixes a genuine spurious-reload path.
- `_run_local_command` sets `_spoke = True` unconditionally — right for the follow-up
  window.
- `SayTTS` kills `say` and unlinks the temp WAV on `BaseException`.
- `PiperTTS` refetches a corrupt model once.
- `SpeakerGate.warm_up` fails open, per its documented contract.
- `IdleSchedule.conversation_ended(brain=...)` correctly leaves an already-pending
  reflect deadline alone.

### Minor note

`MicStream.frames` logs "waiting for the device" on the first stall and returns on the
second, so effective tolerance is six seconds. Matches the comment — just note that
`_STALL_LIMIT = 2` means one retry, not two.
