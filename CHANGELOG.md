# Changelog

Notable changes to Ciel. Newest first.

## 2026-08-18 — Vigil phase three: done means observed

**Why.** The wishlist's second structural item: Ciel reports success
because a tool returned without error, "which is a weaker claim than it
sounds." And nothing on the machine could say "that thing you started has
finished."

**What.** The verification loop closes. After any confirm-gated action a
present user approved, the recorder files a read-back check (the recorder
still only observes — the emitter can note that an action deserves
checking, never affect it). The check runs as a new kind of proactive turn:
a silent "note" — no audio, no budget, quiet-hours-exempt because it
disturbs nobody — where an unattended turn re-observes with read-only
tools and writes down what is actually true. The finding replaces the
instruction and rides a held note into the next conversation: "the message
to Sam did send", "the calendar event never appeared — it may need
redoing." SKIP means checked-and-nothing-worth-relaying; a failed check
degrades to holding the instruction so the next attended conversation can
verify with full tools.

And the WorkWatcher: `watch_for_completion` lets an attended turn register
a file that should appear or a process that should exit, with a deadline —
persisted like timers, polled every fifteen seconds, kicked on
wake-from-sleep. Completion files the labeled event through the normal
interruption policy; a missed deadline files its own honest event ("never
finished"). The tool is Witness-denied unattended — arming a watch is
scheduling future unprompted speech, which is the budget bypass the rule
exists to prevent — and withheld entirely when Vigil is off.

## 2026-08-18 — Vigil phase two: the away outlet and the morning brief

**Why.** Phase one could speak or hold; it had no way to reach you when
you're out, and no standing morning ritual. And Ciel now lives on a laptop,
which sleeps — every monotonic deadline in the process quietly stretches by
the length of each nap.

**What.** Three additions and a reconciliation. (1) The away outlet: an
urgent event (importance 3+) arriving while nobody is around is composed by
an unattended turn and *texted to you* — to the one handle pinned in
`[proactive] owner_handle`, through a pipeline-owned send the model cannot
reach (send_message stays Witness-denied inside the turn). Three switches
must agree (`messages.enabled`, `allow_send`, `owner_handle`), texting has
its own daily budget (3), and quiet hours hold texts exactly as they hold
speech — a 2am buzz violates sleep the same way a voice does. Send failure
holds the event for the next conversation rather than losing it. (2) The
morning brief: `[proactive] brief_time = "07:45"` arms a ScheduleWatcher
that fires one brief event per day (deduped by date, expired after four
hours — a brief is breakfast, not dinner); the brief turn receives today's
remaining agenda from EventKit plus everything held overnight, and consumes
those held notes as its material. (3) Wake-from-sleep catch-up: a
wall-clock jump between frames kicks every watcher awake (a nudge due at
lid-open lands *now*, not after the remainder of a paused poll interval),
routes the next timer poll through the grace-filtered missed sweep, and
clears reflect/rotate deadlines a long nap already mooted. Plus
`deploy/ai.ciel.plist`: Ciel as a KeepAlive LaunchAgent, the restart story
the mic-stall detector was designed around.

## 2026-08-18 — lessons taken from hermes-agent

**Why.** A deep read of NousResearch's hermes-agent (see
`reports/2026-08-18-hermes-agent-steals.md`) confirmed Vigil is ahead on
interruption policy — Hermes has none — but Hermes carries scar tissue
worth having: rules about what a self-improving agent must *not* save,
provenance on machine-written memory, and watchers that report their own
failure.

**What.** Six adoptions. Closure's prompt gains a negative-capture list
(never save "tool X is broken" — transient failures harden into refusals
cited long after the fix; save the workaround, never an unresolved failure
as a method). Memory gains kind `procedure` — facts say who the user is,
procedures say how a class of task is done for this user, updated in place
— with frustration named a first-class procedure signal. Every memory now
records write provenance (`context: conversation | proactive`), stamped
through the Witness mode's new labels, and recall renders the hedge for
facts no user ever heard ("noted unattended — unconfirmed"): the runtime
states provenance, the model doesn't narrate it. The recall tool states the
source-first rule: memory is never evidence about current external state —
check the live source before asserting absence; "I recall" is not "I
checked". Watchers get a failure-streak self-report (three failed scans →
one importance-1 event → a held note names the broken watcher, instead of a
log line nobody reads). And `touch ~/.ciel/hold` is the emergency brake:
proactive delivery pauses, events queue, nothing is dropped, until the file
is removed. Deferred steals (duplicate-marked redelivery, dead-target
registry, watcher notepads, hash-suppression tier, propose-never-auto-enable
suggestions, the hub wake contract) are assigned to their phases in the
report.

## 2026-08-18 — Vigil: the right to speak unprompted, earned

**Why.** Ciel existed only while being spoken to — her own capability
wishlist put it first: between turns there is no process of hers watching
anything, so a meeting can draw near, a job can finish, and nothing is said
unless the user happens to ask at the right moment. Timers proved the
mechanism (speak later, unprompted); what was missing was everything around
it — event sources, and the policy for when interrupting is justified,
which the wishlist correctly called the hard part.

**What.** A proactive layer, codename **Vigil** (`ciel.proactive`), off by
default behind `[proactive] enabled`. Watchers push events into one queue
persisted at `~/.ciel/proactive.json` — queue, held notes, dedupe record,
and the day's budget all survive the autoreloader's re-exec. The first
watcher reads the calendar through EventKit (new dep, one-time Calendars
permission) and nudges "your two o'clock is in ten minutes", expiring
unspoken at the meeting's start. A pure `InterruptionPolicy` — quiet hours
that may span midnight, an importance floor, presence, and a hard daily
budget of unprompted speech — picks each event's route: speak now, hold
for the start of the next conversation (delivered as a system note the
transcript never confuses with the user's words), or drop. Presence reads
the screen-lock state and keyboard recency through Quartz plus
conversation recency through the mic, selectable via `presence_mode`
because the deployment box is undecided; all of it is evaluated lazily,
never at frame rate — the voice hot path gained one boolean check.

The turn itself runs unattended under a new named rule, the **Witness
rule** (`brain/witness.py`): a turn with nobody listening may look and may
remember — read-only tools and Ciel's own memory/projects notebook — but
nothing outside the notebook changes; messages, timers, files, shell,
searches, and unlisted connector tools deny in a PreToolUse hook that
leads the guard chain. Reflection now runs under the same rule, so its
"no messages, no searches" is enforced rather than requested. Connector
entries gain an `unattended` list for reads a nudge may verify against
("check the meeting still exists before announcing it"). Routing is a
one-way valve: the policy picks the ceiling and the model may only decline
(SKIP) or de-escalate to a held note (HOLD) — event text is written by
other people, and words in a calendar title must never be able to argue
something off the machine. `scripts/probe_vigil.py` drives all of it —
queue persistence, the policy table, the midnight wrap, the Witness
matrix, the valve — on injected clocks and fakes, no audio required.

## 2026-08-18 — a fast driver and a slow specialist

**Why.** Opus drove every turn, including "set a timer" and "what's on my
calendar". That is the strongest model in the house spending its time on
work that cannot use it — paid for twice, once in time-to-first-speech
against a half-second target, and once in Max quota that then ran out
mid-afternoon. The escalation valve already existed for depth; the model
choice was still all-or-nothing.

**What.** The driver moves to Sonnet at `medium` effort (up from `low` —
low read as careless on the fiddlier turns, and medium still fits the
latency budget). Depth is not surrendered, it is relocated: a new
`[brain] deep_model` sets the model for the deep-thought agent, defaulting
to `"inherit"` and set to `"opus"` here, so the escalation valve now buys a
stronger model as well as more effort. Everything else about escalation is
unchanged — the same prompt rules, the same spoken "give me a moment", the
same per-question judgment — which is why this was one new setting rather
than a second escalation path. Fable left unwired pending a decision on
where it sits.

## 2026-08-18 — the sounds of listening and thinking

**Why.** Three silences were lying. After you spoke, nothing happened until
the brain's first sentence — one to several seconds that read as not having
been heard. When Ciel escalated to deep thought, the room went quiet for a
minute with no explanation unless the model happened to announce it. And
when she wrote "Hmm," the normalizer silently deleted it — the interjection
stripper assumed piper couldn't voice vowel-free words.

**What.** Three fixes. (1) A heard-ack: the moment a turn heads to the
brain, one of `[brain] ack_phrases` ("Hmm.", "Let me see.", ...) plays —
launched as a task overlapping the model's latency rather than adding to
it, with every later play awaiting it so voices never collide. Local
commands answer instantly and skip it. (2) Escalation is now verbalized
deterministically: the brain stream yields an `escalation` event when the
deep-thought agent spawns (detected on the wire, verified live), and if
nothing has been spoken yet that turn the pipeline says "Give me a moment.
I want to think this through properly." — skipped when the model already
announced it in its own words, so it is never said twice. (3) The
normalizer now *respells* the hum family to canonical forms instead of
stripping: measured on lessac-medium, piper renders "hmm" as a true 0.48s
hum (spelled-out letters measure 0.91s), so "Hm"/"Hmmmm"/"mhm" map onto
"hmm"/"mm-hmm"/"mmm" and are finally heard. "Shh"/"tsk"/"pfft" stay
stripped, still unverified as hums.

## 2026-08-18 — think quickly, escalate deliberately

**Why.** Effort sat at the model default (high) on the reasoning that
conversation barely pays for it under adaptive thinking. True, but it left
the depth decision entirely to the effort knob — one static level for both
"what's my name" and "which job offer leaves me ahead after ten years".
A voice assistant wants both: snappy by default, deliberate on demand.

**What.** `[brain] effort` now defaults to `low`, and a `deep-thought`
subagent — same model, `deep_effort` (default `high`), WebSearch/WebFetch —
is registered through the Agent SDK's per-agent effort support. The prompt's
new "Thinking harder" section tells Ciel to hand genuinely hard questions to
it and relay the conclusion, and to say "give me a moment" first, because a
deep pass measured at ~74 s on a real multi-step finance question and
silence that long reads as a hang. Escalation is her judgment call
per-question, not automatic: in testing she correctly answered a trick
arithmetic question directly at low effort without spawning, and spawned
when asked for real deliberation. Effort is fixed per SDK connection, which
is why escalation is a subagent rather than a mid-session switch.
`deep_effort = "xhigh"` buys more depth; `None` removes the valve.

## 2026-08-18 — local commands: the brain is not a regex

**Why.** "Ten minute timer" was paying a frontier model's latency and cost
to do what a regex does in microseconds — and mechanical requests are
exactly where seconds of thinking-pause feel most absurd.

**What.** A local command grammar (`commands.py`) runs on every transcript
before the brain sees it: plain duration timers ("set a timer for twenty
five minutes", "an hour and a half timer"), cancel ("stop the timer"),
status ("how much time is left"), and "reload" are handled entirely in the
pipeline — spoken reply included, model never invoked, session untouched.
The contract is conservative certainty: patterns fire only on whole
utterances that cannot mean anything else, with leading "hey ciel" (and
"seal", Whisper's favourite mishearing) stripped first; anything uncertain
falls through to the brain, so a miss costs the old latency, never a wrong
action. Labeled reminders and clock alarms stay brain-side on purpose —
"remind me when the rice is done" carries phrasing a model should own.
Spoken "reload" reuses the source-watcher's from-idle reload path. Local
timers still anchor "starting now" to the end of the reply, same as
brain-set ones. `[commands] enabled = false` turns the whole layer off.

Also local: the escape hatch. "Never mind", "forget it", "cancel", "stop",
"that's all" (and friends, with trailing thanks tolerated) end the exchange
instantly — any held partial thought is dropped, the follow-up window
closes, and Ciel says nothing back: the answer to "never mind" is the
absence of one, with the indicator going idle as the acknowledgement. A
dismissal is matched against the raw utterance *before* it merges into a
Cauchy-held thought ("set a timer for, um" ... "never mind" cancels the
held request rather than burying the dismissal inside it), and a dismissal
alone never arms reflection — an accidental wake ended with "never mind"
leaves nothing worth reflecting on. "Cancel the timer" still cancels the
timer; bare "cancel" dismisses.

## 2026-08-18 — timers, alarms, and the ability to speak later

**Why.** Every other capability answers inside a turn the user started. "Set
a ten minute timer" requires the opposite — speaking unprompted, minutes or
hours later — and Ciel had no machinery for it: the most-used feature of
every voice assistant had nowhere to live.

**What.** Three new tools (`set_timer`, `cancel_timer`, `list_timers`) drive
a `TimerService` (`timers.py`) that the frame loop polls: a due timer rings
— a two-note figure, deliberately distinct from the thinking chime — and
speaks its announcement the moment nothing else owns the audio (never over
Ciel's own turn, the user mid-utterance, or a Cauchy hold; at worst it waits
seconds for a turn to end). The label on a reminder *is* the announcement —
the model phrases "remind me when the rice is done" as the sentence "The
rice is done." at set time, so the fire path needs no brain call at all.
Timers persist to `~/.ciel/timers.json` and use wall-clock time, so
restarts, autoreloads, and a laptop asleep at 7 AM all keep their word;
timers that came due while Ciel was off are announced late-but-honestly
within a one-hour grace window and dropped past it. A duration's countdown
is anchored to the end of the turn's spoken reply, not to the tool call —
the call lands seconds before "ten minutes, starting now" reaches the
user's ears, and the count starts when "now" is heard, not when it was
decided. (Timers are born pending and the pipeline commits them as the
reply's audio ends; a crash in between falls back to the tool-call anchor.) This is the foundation
proactive announcements (calendar heads-up, message alerts) can build on.
Known v1 limit: an alarm rings once rather than nagging until acknowledged.

## 2026-08-18 — Ciel can look at the screen

**Why.** Natural speech points at the screen constantly — "put this event on
my calendar", "what does this error mean" — and those references cannot be
resolved from audio. Ciel's only move was to ask the user to read their own
screen aloud, which defeats the point of pointing.

**What.** New `look_at_screen` tool (`brain/tools/screen.py`) captures every
display via `screencapture`, downscales to 1568 px long edge with `sips`
(~200 KB JPEG per display), and returns the images straight into the model's
context — the Agent SDK's in-process MCP server passes image blocks through.
The prompt's new screen section sets the contract: resolve the reference from
the screen first, ask only if it isn't there, and never look unprompted.
Screen Recording permission is preflighted via Quartz because `screencapture`
without the grant exits zero with every window silently missing; a missing
grant comes back as words Ciel can relay (and triggers the system's own grant
dialog). `[screen] enabled = false` removes the tool entirely.

## 2026-08-18 — transcription moves to the GPU

**Why.** faster-whisper runs on CTranslate2, which has no Metal backend, so
every utterance was decoded on CPU cores that the rest of the pipeline also
wants. The STT protocol existed precisely so a GPU engine could slot in when
one arrived.

**What.** New `mlx-whisper` engine (`stt/local_mlx.py`) is the default; it
runs the same Whisper weights on the GPU via MLX. Measured on a 5.2 s
utterance with `small.en`: 0.24 s per decode against 1.39 s for
faster-whisper — 5.7x faster, identical transcript, and a shorter warm-up
(2.1 s vs 3.3 s). The hallucination filter moved to `stt/hallucinations.py`,
shared by both engines because the failure mode belongs to the weights, not
the runtime. Engine choice is now `build_stt` in `ciel.stt` — mirroring
`build_tts` — and falls back to faster-whisper if mlx-whisper is missing.
`[stt] mlx_model` selects the weights; `mlx-community/whisper-large-v3-turbo`
is the documented accuracy upgrade to try now that decode has ~20x-realtime
headroom.

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
