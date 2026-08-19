# Hermes-Agent: What We Took, What We Skipped, What We Already Do Better

Date: 2026-08-18
Source: https://github.com/NousResearch/hermes-agent (reviewed at depth —
cron, skills, memory, gateway, approval subsystems read in code, not README).

Hermes is a chat-first, multi-platform, self-improving agent framework. Its
proactive surface is a cron package; it has **no interruption policy at all**
— no quiet hours, no presence, no delivery budget, no importance model. On
"when may an assistant speak unprompted," Vigil is categorically ahead. What
Hermes does have is years of scar tissue around *suppression*, *provenance*,
and *self-improvement*, and that is what we took.

## Adopted (landed today)

1. **Reflection negative-capture list** (`brain/prompt.py`). Closure now
   explicitly refuses to save: negative claims about our own tools (they
   harden into refusals cited long after the fix), environment problems the
   user can fix, transient failures that resolved (the workaround is the
   lesson), and unresolved failures written up as working methods.
2. **Procedural memory** — kind `procedure` (`memory/store.py`), with the
   who-vs-how split stated in the `remember` tool and in Closure's prompt:
   facts say who the user is, procedures say how a class of task is done for
   this user, named for the class, updated in place. Frustration ("stop
   doing X") is named a first-class procedure signal.
3. **Memory write provenance** (`memory/store.py`, `brain/tools/memory.py`,
   `brain/witness.py` labels). Every memory records `context:` — 
   `conversation` (live turns, and reflection distilling one) vs `proactive`
   (an unattended Vigil turn, nobody around). Recall renders the hedge
   deterministically: "(fact, noted unattended — unconfirmed by the user)".
   The runtime states provenance; the model doesn't get to narrate it.
   First concrete step on wishlist items 4/5 (freshness, "I checked" vs
   "I recall").
4. **Source-first recall rule** (`recall` tool description): memory is what
   was true at save time, never evidence about current external state; check
   the live source before asserting absence; say whether you checked or
   recalled.
5. **Watcher failure-streak self-report** (`proactive/watchers.py`
   `WatcherHealth`, wired into the calendar watcher): after 3 consecutive
   scan failures, one importance-1 event enters the queue — the *watcher*
   gets reported to the user via a held note, instead of a log line nobody
   reads. One report per streak, keyed by the streak's first failure.
6. **Emergency brake** — `touch ~/.ciel/hold` pauses all proactive delivery
   (events queue, nothing dropped) until removed; logged once per
   engagement. File sentinel so it works from outside the process and
   survives restarts. Timers and in-flight speech untouched.

## Deferred, assigned to phases

- **Phase 2 (iMessage outlet):** the `RECOVERED_MARKER` pattern — a crash
  mid-delivery replays with a visible "may be a duplicate" prefix (honest
  at-least-once); the **dead-target registry** — record a proven-unreachable
  handle, short-circuit sends, self-heal on any success.
- **Phase 2 (morning brief) / Phase 3 (work watcher):** the **per-watcher
  notepad** — a size-capped KV scratchpad (cursors, watermarks) that renders
  to `""` when empty (prompt-cache safe); **`context_from: self`** — inject
  the previous run's output with "don't repeat what was already reported."
- **Phase 3/4 (mail and other monitor-shaped watchers):** the **hash
  -suppression tier** — a deterministic source runs first, output hashed;
  unchanged means the brain is never invoked, so quiet ticks cost nothing.
  Two invariants to keep: persist the hash *before* the turn runs, and a
  source failure is an error, never a "change".
- **Phase 3+ (self-improvement):** **suggestions** — when reflection notices
  a recurring pattern, propose a proactive rule (one spoken yes/no through
  the confirm broker), never auto-enable; latch dismissals forever by dedup
  key; cap pending suggestions (~5) so the list can't become a nag wall.
- **Endgame (hub-and-spoke):** the **wake contract** — a device result
  re-enters as a *new turn when idle*, never spliced mid-turn; delivery
  failure raises so the sender rewinds its cursor. **Device pairing** via
  short codes from an unambiguous alphabet (doubly right for codes read
  aloud). One shared event queue as the only rail — never a second queue.
- **If we ever add an LLM risk-classifier to auto-clear low-risk
  confirmations:** strip comments first, delimit the untrusted command,
  operator policy in the system channel only, three verdicts
  (approve/deny/escalate), any error = escalate.

## Considered and rejected

The multi-process cron scheduler (7k lines of locking one asyncio process
doesn't need), the skills-hub distribution pipeline, Honcho cloud user
modeling (network round-trip on the recall path), container isolation (the
journal + undo is the better recovery story for an assistant that must touch
Messages and the filesystem), the 300-line shell-regex danger table (we gate
on typed tool semantics — a stronger position than regex-over-strings), and
the 30k-line gateway god-file.

## What Ciel does better (kept honest, from the code)

- **The interruption policy exists.** Quiet hours, presence, importance,
  a hard daily budget, per-event expiry — Hermes has none of it; its
  anti-spam is per-job suppression tricks. The wishlist called the policy
  the hard part, and only one of the two projects has built it.
- **Presence.** Hermes cannot know whether anyone is there; it texts a
  channel. Ciel routes on screen lock, input recency, and conversation
  recency — and holds what shouldn't interrupt.
- **The whole voice loop.** Wake word, endpointing, barge-in, speaker
  verification, the Cauchy hold, mic-drain discipline, latency attribution
  (reconnect vs model time). Hermes bolts TTS onto chat; Ciel is built
  from the microphone up.
- **Spoken confirmation as an enforced gate.** Hermes prompts in chat with
  buttons; Ciel voices the actual action ("Send an email to Sam — okay?")
  through a broker that handles barge-in, suppression, and stacked
  questions — and the deny path is hook-enforced, not description-suggested.
- **Deterministic unattended routing.** The one-way valve means injected
  text (calendar titles, mail subjects) can only make Ciel quieter. Hermes'
  equivalent decisions lean on an LLM guardian — a softer boundary.
- **Undo.** The action journal with pre-edit snapshots is a regret layer
  Hermes simply lacks (its execution ledger records attempts; it cannot
  undo anything).
- **Session lifecycle as design, not coping.** Short sessions + Closure +
  rotation solve the permanent-session problem structurally; Hermes carries
  compression machinery and 30k-token background review forks to manage
  sessions that never end.
- **Zero-LLM conversational paths.** The local command grammar answers
  "ten minute timer" with no model turn at all; Hermes has no equivalent.
- **Legibility.** Reasoned per-field config docstrings, codenamed
  mechanisms, probe scripts that run without hardware, and modules a
  person can read end to end.
