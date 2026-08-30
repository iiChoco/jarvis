# Changelog

Notable changes to Ciel. Newest first.

## 2026-08-29 — the audit's flagged items, closed

**Why.** The documentation audit surfaced five findings too behavioral to
fix as prose. Four were worth closing (the fifth — the unused CoreLocation
dependency — is a design decision still owed).

**What.** *Undo can read its own snapshots*: the undo prompt sends the
model to read a snapshot in `~/.ciel/undo/snapshots`, which the workspace
guard denied under any narrow workspace. Kept the journal's
no-side-channel philosophy — undo stays ordinary tools — and gave the
guard one carve-out instead: reads (never writes) under the snapshots
directory, with a suffix check so a stray snapshot of a forbidden file
stays buried. *Confirmations are lane-matched*: `remote()` now records
which lane asked ("discord" or "web"), and a Discord text answers only a
Discord-origin question — a question shown on the loopback page was never
shown to the phone. The page still answers either origin, because every
question is mirrored to it as a ciel-confirm row. *Backfill drains*: the
reconnect sweep paged once (25 oldest) and silently lost the tail of a
deeper gap; it now pages until drained, with a flood cap. *Two logs tell
the truth*: the wake ready-log prints a custom model's phrase (the path
stem), not its path; the speaker gate says "centroid" when the centroid
row wins instead of naming a take that does not exist. Full scripted
suite passes: 666 checks across nine probes, plus a direct exercise of
the carve-out (read allowed, write denied, forbidden suffix denied,
other outside paths still denied).

## 2026-08-29 — the documentation audit: every claim checked against the code

**Why.** "Go through all the documentation and make sure it is all up to
date." Docs drift; this codebase documents itself unusually heavily, which
means unusually many claims that can silently stop being true.

**What.** Five parallel read-only audits swept every module docstring,
config field doc, and the README against the source, and everything found
was fixed. The README got the most: the intro and config example now name
mlx-whisper as the shipped STT default (faster-whisper is the CPU
fallback), the custom wake-word path is present tense (trained, probed,
ready-line aware) instead of future work, the codename table gained its
five missing rows (Singularities, Tower Clearance, Chart, Vigil, Witness)
and un-conflated Compact Support from Singularities, the status-pill table
gained the reasoning state, a full Vigil section now exists (watchers,
policy order, budgets, the hold brake), the probe list gained its five
missing scripts, enrollment documents all six modes and the real take
count, and the module table covers the tree as it is. Two dozen docstring
corrections landed across the packages — stale defaults (500 ms
endpointing, say-as-fallback), phantom references (`build_memory_tools`,
`probe_bargein.py`, the "plan"), wrong lists (Witness's deny list, Vigil's
event sources, the grantable catalog's reload promise), and the confirm
broker's Discord-only framing (the web lane shares it).

Where the doc was right and the code was wrong, the code moved instead:
the amara.org hallucination entry violated the list's own normalized-form
invariant and could never match; `origin_allowed` judged a portless
https Origin as port 80; the grants tool promised a reload that
`[dev] autoreload = false` would never deliver; the system prompt told the
model it cannot read outside the workspace even when `read_only_outside`
says it can (now threaded through); and the fast-path grammar's address
list lacked "jarvis" — the default wake phrase — so "hey jarvis, ten
minute timer" always paid for the brain. Two probe fixes: the Vigil
probe's minimal pipeline predated the web tap, and the Oura probe's
junk-days check hard-coded the wrong weekday, latent until the real date
moved past its fake one. Full scripted suite passes: 666 checks across
nine probes.

## 2026-08-29 — the voice speaks from the ceiling

**Why.** "Give my current piper voice the post processing that JARVIS
gets." The cinematic-assistant sound is mostly not a different voice but
a treatment: small-speaker low-end rolloff, a presence lift where
consonants live, and fast, quiet early reflections that place the voice
in the room's architecture instead of nowhere.

**What.** `tts/effect.py` — a `VoiceEffect` wrapper speaking the
`TextToSpeech` protocol on both sides, so neither the pipeline nor the
player learns it exists. One linear-phase FIR carries the whole tonal
tilt (~6 ms group delay, normalized to unity at 1 kHz); twelve sparse
early reflections at prime-ish spacings with alternating signs supply
the room (~-12 dB wet, ringing ~130 ms past each sentence into a
flushed tail); a tanh limiter with measured make-up gain keeps treated
loudness within 0.6 dB of the dry voice. Pure numpy on purpose — scipy
is only in the venv as a transitive — and stateful overlap-save per
chunk, so time-to-first-audio pays only the FIR delay. Wired as
`[tts] effect = "jarvis"` (default `"none"`, Literal-validated), applied
in `build_tts` and re-applied on the runtime `say` fallback: the
treatment belongs to the voice's character, not to whichever engine is
producing it. Verified by A/B synthesis: sub-100 Hz -8 dB, presence
+3.7 dB, peaks soft-limited under full scale, RMS matched.

## 2026-08-28 — the chart shows who is working

**Why.** "I want to be able to see all active agents on the UI." Ciel
does things between and during turns — a deep-thought pass someone is
sitting in silence for, watches armed on background work, timers
counting down — and the page showed none of it: a running watch was
invisible until it spoke, and the only trace of deep thought was one
event row scrolling away.

**What.** The active-agent roster. A new `agents` frame in the web
protocol (and a roster in the hello), carrying everything working on
the user's behalf right now: `deep` (the brain now stamps the
escalation in flight and clears it when the parent turn resumes —
`Brain.deep_thought_since`), `watch` (the work watcher's registrations,
labeled with their completion sentence), `timer`/`alarm`. Deliberately
*not* the Vigil watchers themselves — standing infrastructure that is
always on stops meaning anything in a roster; this lists work with an
end. The pipeline polls the roster once a second at the mute sentinel's
cadence (polling beats wiring change hooks through three sources) and
the link dedupes, so an unchanged second costs one list build. On the
page: a chip beside the state pill counts ("2 working", breathing while
a deep pass runs), and expands into a strip with per-kind icons, the
watched target, and live countdowns ("5m in · 24m left"). Unknown kinds
render as plain rows — the transcript-label rule, because new agent
sources will add new kinds. Verified scripted and live: `probe_web.py`
grew roster checks (broadcast, dedup, hello carriage) and a `--port`
flag plus a "deep" toggle in live mode, and the page was driven in a
real browser — chip, panel, countdown tick, deep engage/clear.

## 2026-08-26 — a page to talk through; a switch that keeps the room quiet

**Why.** "This GUI is meant for when I'm outside, in class, at the
library." Ciel had lanes for the keyboard and for Discord, but no way to
*see* the conversation, and no way to be in the room with it without it
making sound — a lecture hall tolerates neither a spoken reply nor a
false wake.

**What.** The web GUI (`remote/web.py`, codename **Chart** — a local
coordinate window onto the same manifold; a native app later is another
chart of the same atlas). A loopback aiohttp server, off by default
(`[web] enabled = true`, `uv sync --extra web`): the page shows every
lane's transcript rows live (a tap in `_record`), mirrors the HUD's
state pill (a `TeeIndicator` fans the announcements), takes typed turns
through the Discord lane's queue-and-send surface, and renders
confirm-tier questions as Yes/No buttons (`confirm.remote`, the
texted-question road). Web turns count as presence — reaching a
loopback port means being at the machine, the keyboard's trust — and
the one browser-shaped hole, cross-site WebSocket access, is closed by
an Origin check before a frame is read. The WebSocket protocol is
documented in the module as the contract a SwiftUI client builds
against later. And the mute switch: one gate at the top of
`Player.play()` silences everything (greeting, acks, timer rings,
sentences, apologies) with no call site knowing mute exists; the frame
loop stops watching for the wake word; Vigil "speak" decisions become
held notes; timers surface as text on the page. State lives in
`~/.ciel/mute` — the hold sentinel's trick — so the autoreloader's
re-execs can't un-mute a lecture, and `touch ~/.ciel/mute` works from a
hotkey with the page closed. Verified scripted and live:
`probe_web.py`, plus the page driven in a real browser (send, echo,
confirm banner, mute round-trip, reconnect with the draft preserved).

**Why.** "Put a watcher on my ring stats and my location." The readiness
watcher covered one number; a bad night is usually the *sleep* score, and
a still day is the activity one. And location had no source at all —
Ciel could not answer "where am I", let alone notice a move.

**What.** The Oura watcher now files at most two notes a day: one in the
morning when readiness or sleep (or both — "rough night by the ring")
sits at or under its threshold, with hours asleep from the session and
the weakest contributor named; one from `activity_check_after` when the
activity score is still low ("a walk would fix it", importance 1). Each
threshold is its own switch. And a location layer (`location.py`,
`[location]`): two sources, honestly labelled. The Wi-Fi network the Mac
is on (`system_profiler` — CoreWLAN and `ipconfig` redact the SSID now,
and needs no permission) locates the laptop; Find My's device cache
locates the phone, behind two gates the code names in its one-time
warning — Full Disk Access for the app running Ciel, and a macOS that
still writes the cache as JSON (14.4 and later don't). CoreLocation was
tried and is out: macOS never shows a Python process the permission
dialog (`kCLErrorDenied`, no prompt). `[location.places]` turns either
reading into a name; the `where_am_i` tool (Witness-listed, read-only)
speaks the place, the device it came from, and the reading's age; the
Vigil watcher notes moves between named places as importance-1 notes
after a silent startup baseline. Also repaired in passing: a bare
`uv sync` while adding the CoreLocation bindings dropped every optional
extra — the Piper voice, the voice gate, Discord — and Ciel fell back to
`say` mid-conversation; `uv sync --all-extras` put them back. Verified
scripted: `probe_location.py` and `probe_oura.py`.

## 2026-08-21 — the ring answers "how did I sleep"

**Why.** The most-asked morning question had no source: Ciel could read a
calendar and a message thread but not the one sensor worn all night. And
the ring's own verdict — a low readiness score — is exactly the kind of
thing worth one unprompted sentence, which is what Vigil exists for.

**What.** The Oura integration, end to end. `oura.py` is the client: Oura
Cloud API v2 over OAuth2, stdlib urllib on a worker thread, read-only by
construction (daily readiness, sleep, activity, and the sleep *sessions* —
the summaries carry scores, but the number people want is hours, and that
lives on the session). Oura stopped issuing personal access tokens in
December 2025, so the way in is an application of your own: `[oura]`
holds the client id (the secret by environment), `probe_oura.py
--authorize` runs the browser approval once — `extapi:daily` scope only,
PKCE, a localhost callback with a state check, the port bound only for
the duration — and writes `~/.ciel/oura.json`, a new `FORBIDDEN_NAMES`
entry written owner-only. The endpoints are the ones
`api.ouraring.com/.well-known/oauth-authorization-server` advertises
(`/v2/oauth/authorize`, `/v2/oauth/token`), not the legacy pair in the
older docs: an application from the new developer portal is registered
with this server only, and the legacy approval page mints a code the
legacy token endpoint then rejects as `invalid_client` — observed live
during setup, and the reason the module names its sources. Ciel owns that file: Oura's refresh tokens are
single-use, so every refresh (lazy, a day before the month-long access
token expires, serialized across the tool's and the watcher's threads) is
persisted atomically before anything else happens; a spent or revoked
refresh says how to repair it instead of failing forever. A legacy token
still works as a static bearer until Oura shuts it off; the OAuth file
wins. The `oura_summary` brain tool answers for a day or a range (up to
two weeks), labelled as a person would read it back ("Today: sleep score
79, 7 hours 12 minutes asleep, in bed 11:42 pm to 7:31 am…"), and is on
the Witness list so an unattended morning turn may check the ring before
it speaks. The Vigil watcher polls today's readiness every half hour and,
at or under `low_readiness`, queues one importance-2 nudge per day naming
the weakest contributor; a fine morning is silent. Armed is the one test
everywhere — enabled *and* holding a way in — so the registry, the prompt,
and the watcher can't disagree about whether the ring exists; enabled
without an authorization warns once and arms nothing. Verified scripted:
`probe_oura.py` (shaping, the tool through a fake client, the nudge, the
watcher's lifecycle and failure streak, the token store's refresh and
rotation, the live localhost callback, config).

## 2026-08-20 — the wall of "clear" dies in the filter

**Why.** Observed live: Whisper (large-v3-turbo) locked onto one token and
transcribed a spoken sentence as "I'm going to use the same way to make a
clear clear clear…" — three hundred clears. It sailed past both existing
decode-time mitigations (`condition_on_previous_text=False` and the
temperature fallback — the retry can loop too), then the Cauchy hold
faithfully held the wall as a "partial thought" and merged it into the
next utterance, doubling it. The model, to its credit, answered "That one
came through garbled, sir" — but a thousand tokens of noise per garbled
turn is a cost and a bug.

**What.** A deterministic backstop in the shared filter layer
(`stt/hallucinations.py`, both engines): transcripts that are *nothing
but* a short n-gram repeated are dropped as hallucinations, and
`collapse_repetition` salvages the head-and-wall case — real speech
survives untouched, two copies of the looping phrase remain as evidence,
the wall is gone before the hold, the merge, or the model ever see it.
The bar is six consecutive copies of an n-gram (n ≤ 4): "no no no" and
"very very very very" pass through untouched; no human utterance clears
six. Punctuation variants ("clear, clear. clear!") count as one run.
Verified scripted: `probe_stt.py`, 21 checks, including the observed
failure verbatim and a 2000-word wall collapsing in well under a second.


## 2026-08-20 — powers by text, mentions in servers, the token off-limits

**Why.** With the Discord lane live, two asks followed immediately: change
what Ciel may do from a phone ("enable your shell" from the couch across
town), and answer `@ciel` in a server channel. And the lane's own token
was sitting in model-readable config — a credential in a readable file is
a credential in every future context window.

**What.** Three pieces. *Grants* (`[grants]`, `brain/tools/grants.py`):
list/grant/revoke over a fixed catalog of capability switches. Enabling
passes the enforced confirmation gate ("Enable shell access — okay?" —
voiced at home, texted away) before one byte of config changes; disabling
runs instantly, because de-escalation with friction teaches people to
leave things on. Edits are surgical (comments survive, parse-verified,
rolled back on failure), journaled both directions but filing no read-back
event (the tool verifies its own edit), and trip the reload that makes
them real. The catalog is deny-by-construction: the Discord pinning, the
voice gate, tool tiers, and the workspace path are simply not in it.
*Mentions*: `@ciel` in a server the bot was invited to is a turn — owner
account only, reply to that channel, and the turn is told it's in public:
discretion note instead of the DM note, held Vigil notes never delivered
outside DMs. No privileged intent needed (mention content is exempt).
*Hardening*: the token moved to `~/.ciel/discord.token`, now a
FORBIDDEN_NAMES entry, so file tools and the shell gate both refuse it;
the DM last-seen mark persists (`discord.json`) so the catch-up sweep
spans restarts — texts sent during a grant's own reload come back out of
the gap, capped at ten minutes so stale instructions die unexecuted. Also
fixed in passing: both confirm-gate hook timeouts (120 s) sat *below* the
remote gate's worst case (two 120 s texted windows) — raised to 600 s, so
a slow answer can no longer let the SDK time the hook out mid-question.
Verified scripted: probe_grants (32 checks), probe_discord (37), and the
full suite still green (148 shellguard, 56 closure, 171 vigil).


## 2026-08-20 — the conversation follows you out the door

**Why.** Ciel only existed inside earshot: away from the machine there was
no way to ask it anything, and the away outlet (when armed at all) was
one-way. "Text Ciel when I'm not home" is the missing half of living with
an assistant.

**What.** The Discord lane, codename Parallel Transport (`remote/discord.py`,
`[discord]` in config, `uv sync --extra discord`). A DM from the pinned
`owner_id` becomes an ordinary turn — same brain, same session, same
transcript, held Vigil notes delivered — and the reply rides back as a
text, buffered into one message rather than streamed as notifications.
Identity is deterministic where the Barn Door is probabilistic: only the
pinned account's DMs are read, and everything else is dropped before it
reaches anything with tools. The Proof Obligation travels too: a
confirm-tier action mid-remote-turn texts its question over the same DM
and waits on notification time (~2 min) for a yes or no — and remote
answers are accepted *only* for remote-origin questions, because a yes to
a question voiced into a room you're not in is not an answer. Remote turns
are deliberately not presence: their transcript rows carry remote labels,
so Vigil keeps treating the room as empty and texts instead of speaking to
nobody. When Vigil wants to message and iMessage isn't configured, the
link is the fallback outlet (`discord.proactive` opts out). The lane
degrades like the indicator — missing dependency, bad token, dead network
are each one warning and a working voice assistant. Verified scripted
(`probe_discord.py`, 29 checks: the gate, the queue contract, every
walk-away path of the remote confirmation denying), and the existing
shellguard/closure/vigil probes still pass. The live half —
`probe_discord.py --live` against a real bot token — is the first thing
to run after the five-minute Discord setup in the README.


## 2026-08-19 — nudges read Google Calendar at the source

**Why.** This Mac syncs no calendars into Calendar.app, so the EventKit
watcher was watching an empty store — nudges would never have fired. The
real calendar lives in Google.

**What.** `[proactive] calendar_source = "google"` routes the calendar
watcher to Google's API directly, piggybacking the auth the [mcp.gcal]
connector already established: same OAuth client keys, same saved refresh
token, nothing new to authorize and no Calendars permission dialog. Access
tokens live in memory only — the connector's token file is never written,
because two writers of one token file is how refresh races start. Same
watcher contract as EventKit (nudges, agenda for the brief, kick on wake,
health streaks), stdlib urllib on worker threads, read-only endpoints
only. Verified live: token refresh, a real scan, and tomorrow's lectures
in the agenda. Adding a fourth watcher also tipped the deferred cleanup:
the poll heartbeat (tick, sleep-or-kick, clear-after-wait) now lives once
in watchers.poll_loop, used by all four.

## 2026-08-18 — the night review: ten findings, ten fixes

**Why.** A day that shipped three Vigil phases deserved an adversarial
pass before anyone slept on it. An eight-angle review of the day's diff
surfaced ten confirmed-or-plausible defects — several of them exactly the
class of bug an unattended layer must not have.

**What.** The worst: a user turn hastening the maintenance slot could ship
a *truncated* iMessage (interrupt ends the stream normally; nothing
checked); the brief consumed its overnight held notes before knowing
whether it would deliver, so a SKIP burned them; cancelling an unattended
turn (autoreload, shutdown) lost its event entirely; the WorkWatcher
mutated the queue from a worker thread, able to corrupt the whole state
file; and the new glob leniency in the shell gate let *targeted*
credential globs (`.ss?/id_*`) escape deny. All fixed — the unattended
choreography now lives once in `_compose_unattended` (timeout-interrupt
inside the suppression block, hastening detection, hold-on-cancel), the
queue moved to a peek/claim lifecycle with an in-flight dedupe horizon,
the brief peeks and consumes only on delivery, watcher threads only read,
and the glob downgrade applies to broad listing patterns alone, with the
reason spoken in the confirmation. Also: cold starts are no longer misread
as wakes from sleep, verify failures hold an honest fallback instead of an
internal directive, read-back checks cap their backlog at three, and the
older hardening reviews' four open issues are closed (resolution appended
to the report). Probes grew to 153 + 148 + 56 checks.

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
