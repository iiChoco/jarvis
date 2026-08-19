# Review: uncommitted hardening pass (2026-08-18)

Scope: full working-tree diff at `~/jarvis` — 30 files, ~829 insertions, 191 deletions,
sitting on top of commit `8fb6652` ("The upgrade batch"). No new features; almost
entirely robustness work with dense justification comments.

Overall: unusually solid. Several of the fixes close genuinely nasty latent bugs.
Three issues below are worth acting on before this is committed.

---

## What the changes do

### 1. Failure recovery in the brain (`src/ciel/brain/agent.py`)

- `Brain.ask` classifies `CLIConnectionError` / `ProcessError` / `BrokenPipe` /
  `ConnectionError` as transport death and calls the new `_evict_client()`, so the
  next turn reconnects instead of querying a dead subprocess forever.
- `_drain_stale` now clears `_needs_drain` only on a *successful* drain. The old
  up-front clear left an unconsumed `ResultMessage` in the stream, which would
  offset every subsequent reply by one — a real latent bug.
- The drain flag is gated on `query_sent` and reset in `_connect`.
- Subagent output is filtered by `parent_tool_use_id` on both `StreamEvent` and
  `AssistantMessage`, so the deep-thought agent's internal streaming is never
  spoken aloud. (Field verified present in the installed SDK.)

### 2. Device-loss handling (`src/ciel/audio/`)

- `MicStream.frames` ends capture after two 3-second stalls instead of awaiting an
  event forever.
- `Player.play` serializes on an `asyncio.Lock`, wraps `chunks` in `aclosing`
  (Piper's generator owns a subprocess), and lets `sd.PortAudioError` propagate out
  of `_write` rather than swallowing it — the old swallow meant "Ciel believes it
  spoke into a muted device."

### 3. Shell guard widening (`src/ciel/brain/shellguard.py`)

- `_unquote` strips interior quotes/backslashes, so `cat ~/.a"w"s/credentials` trips.
- `_normalize` basenames the head (`/usr/bin/sudo`).
- `_effective_tokens` peels transparent wrappers (`env sudo`, `nohup rm -rf ~`),
  handling value-taking options and `VAR=value` correctly.
- `_PIPELINE_SPLIT` splits on a bare `&`. Previously `ls & rm -rf ~` hid the second
  command entirely — a real hole.
- `sort` / `uniq` leave the quiet allowlist, because `sort -o FILE` writes.
- Deliberately *not* peeling `sh -c` / `xargs` / `find`, and keeping the allowlist
  match on the original tokens, is the right call: peeling can only widen deny,
  never widen quiet.

### 4. Text-injection hygiene

- `Memory.to_markdown`, `ProjectStore._save`, `ProjectStore.log_entry` collapse
  whitespace, so model-supplied `"foo\nkind: identity"` cannot spoof a frontmatter
  key or forge a `- ` log line.
- `projects.update` raises on an unknown status instead of silently coercing to
  `"active"` (which used to revert done/paused projects).
- `timers.set_timer` replaces `assert` with real checks — `python -O` stripped them.

### 5. Smaller correctness fixes, all real

- `TimerService.set_at` overflows `tm_mday` instead of adding 86400 (DST-correct).
- `journal.py` writes temp-then-rename; `with_suffix(".jsonl.tmp")` plus a
  same-directory `replace` is atomic as intended.
- `__main__` finally applies `config.log_level` (the field and `CIEL_LOG_LEVEL` were
  parsed and never used) and strips `--new` from the args carried across an
  autoreload `execv` — otherwise every reload deleted the session it was resuming.
- `config.load_config` honors `CIEL_STATE_DIR`, warns on unknown keys and invalid
  `Literal` values.
- `HotkeyWake` loses its stdin thread, so exactly one reader owns fd 0. Two readers
  plus asyncio flipping the fd non-blocking is a real bug class.
- `hallucinations.py` drops entries like `"bye."` / `"!"` that could never match
  post-normalization.
- `normalize.py` stops turning "50 mm" into a hum.
- `imessage.py` converts torn `immutable=1` WAL reads into `MessagesUnavailable`,
  rejects group ids that send silently, and stops deduping email contacts by their
  digits (`sam2@x.com` vs `bob2@y.com` collapsed before — data loss).
- `mcp__ciel__send_message` moves into `_gated_tools`, so it sits behind an enforced
  spoken yes rather than being journaled after the fact.

---

## Issues worth fixing

### A. Glob rule over-denies (`shellguard.py:145-152`, `_forbidden_component`)

`part.strip("*?[]")` only skips a bare `*`, so a leading-dot glob survives: for
`ls -d .*` or `grep -r foo .*`, `part` is `.*`, strip yields `"."` (truthy), and
`fnmatch(".zshrc", ".*")` is True. The command lands in **deny** — the unrecoverable
tier, no confirmation escape hatch — over an ordinary dotfile listing. Same for
`ls .??*`.

Suggested fix: downgrade glob-only matches from deny to confirm. Keeps the
protection, loses the false positive.

Related but milder: wrapper peeling makes `command -v sudo` and `time sudo --version`
deny on what is only a lookup.

### B. `Player.play` conflates barge-in with device failure

`play` now returns `False` for two semantically different outcomes: user barge-in,
and output-device failure. The new branch in `pipeline._respond_stream` (~line 905)
treats `not completed` on the ack line as *user interruption*: sets `_interrupted`,
cancels the confirmation, calls `brain.interrupt()`, prints "(interrupted)", breaks.

So an unplugged output device silently kills an in-flight turn and reports it as a
barge-in. The docstring acknowledges the conflation; the caller does not distinguish.
A `device_lost` signal, or a small result enum, would let the ack path abort without
lying about the cause.

### C. `Player.stop()` sits outside `_play_lock`

A barge-in that lands while a second `play()` is queued on the lock is forgotten: the
queued call clears `_stop` and speaks anyway. Rare, but it is exactly the interleaving
the lock was added for.

### D. Minor: broker phase drops early

`VoiceConfirmBroker._announce` sets the phase to `IDLE` *before* playing the closing
line, so for the duration of "Okay, skipping it." `active` is False and frames route
to the normal barge-in path instead of the broker. Benign today, but it is a state the
broker no longer owns while it is still speaking.

---

## Verified correct (checked, no action needed)

- `Transcript.rotate` clears `_writer` via `close()`, so the next `record` opens a
  fresh file; the rotate is ordered after `_record("event", "session rotated")` as the
  comment claims.
- `MemoryStore._resolve`'s fallback scan is correct for hand-authored files.
- The sentence-boundary change — dropping `$` from `_BOUNDARY`, adding
  `_DOTTED_ABBREVIATIONS` with the `[A-Za-z.]+` trailing-token regex — is right given
  `ask()`'s tail flush.
- `scripts/probe_shellguard.py` covers the new shellguard cases honestly, including
  the `sort -o ~/.zshrc` deny.
