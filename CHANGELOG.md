# Changelog

Notable changes to Ciel. Newest first.

## 2026-08-18 — thinking on the console, not through the speakers

**Why.** `speak_thinking` bundled two decisions into one switch: whether the
reasoning is audible and whether it is visible at all. Off meant the thinking
text wasn't even requested from the model — nothing on the console, nothing
in the transcript — and the only alternative was listening to every
deliberation read aloud.

**What.** New `[brain] show_thinking` (default on) streams reasoning to the
console and transcript independently of the voice. The local config now runs
show-only: reasoning is read, not heard, and the thinking chime is back on
with a new job — with silent deliberation, the tone is what tells the ears
"thinking over, answer next." The `reasoning` HUD state is reserved for
reasoning spoken aloud; show-only stays on `thinking`. "To first speech" in
the turn log now counts only sentences that actually reached the speakers.

## 2026-08-18 — split reconnect out of turn latency

**Why.** A "How are you?" at 09:17 took 80.9 seconds to first speech, after
the previous session had rotated the night before at 22:57. The existing turn
log reported one number — `turn: 80.90s to first speech` — which is
indistinguishable between a slow model and a cold client being rebuilt. The
timer in `_respond` starts *after* transcription, so wake and STT were already
ruled out; everything else was guesswork. Nothing is written to disk (logging
goes to stderr only), so there was no way to check after the fact either.

**Suspected cause, not yet confirmed.** Lazy reconnect. `Brain.ask()` calls
`_connect_locked()` when `self._client is None`, which builds a
`ClaudeSDKClient` and connects it — SDK subprocess plus every MCP server in
config (gmail and gcal both spawn via `npx`, the claude.ai connectors
handshake remotely). That whole startup is paid inside the first turn's
latency. A machine asleep overnight is exactly the case that leaves the client
dead and the next turn holding the bill.

**Changed.**

- `src/ciel/brain/agent.py`
  - `_connect_locked()` now times the connect, stores it on `_last_connect_s`,
    and logs `brain connected in %.2fs`.
  - New `_last_reconnect_s`, reset to 0.0 at the top of every `ask()` and set
    only when that turn actually rebuilt the client. Exposed as the
    `last_reconnect_s` property. Split the same way turn cost already is:
    most-recent-connect vs. what *this turn* paid.
  - Added `import time`.
- `src/ciel/pipeline.py`
  - The turn log line now reads
    `turn: %.2fs to first speech (%.2fs reconnect, %.2fs model), %.2fs total, $%.4f`.

**Note.** This is instrumentation, not a fix — the overnight wait still
happens, it just names itself now. If reconnect turns out to be the 78-ish
seconds, the real fix is reconnecting at idle (a heartbeat) rather than lazily
on the first thing the user says.
