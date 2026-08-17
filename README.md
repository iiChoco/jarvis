# Ciel

A local-first voice assistant. Speech in, speech out, running on your machine.

Speech recognition and synthesis are local and free. Only the reasoning goes to
Claude, through your existing subscription — there is no API key.

```bash
uv run ciel
```

Say **"hey jarvis"**, then talk.

> Ciel is *named* Ciel but answers to "hey jarvis" for now. openWakeWord ships a
> pretrained model for that phrase and none for "Ciel", so v1 borrows it rather
> than blocking on training. See [Personalizing the wake word](#personalizing-the-wake-word).

## What it does

- **Hears you** — `openWakeWord` for the wake phrase, WebRTC VAD for knowing when
  you've stopped talking, `faster-whisper` for transcription. All on-device.
- **Thinks** — Claude Opus 5 via the Claude Agent SDK, with web search.
- **Answers out loud** — Piper, a local neural voice.
- **Remembers you** — durable memory that survives restarts, written unprompted.
- **Judges its sources** — web answers distinguish primary sources from
  aggregators, flag conflicts of interest, and state confidence rather than
  delivering everything in the same certain tone.
- **Shows what it's doing** — a floating pill in the corner of the screen,
  visible from any app.

## The protocols

The subsystems carry mathematical codenames. The identifiers in the code
stay literal (`WorkspaceGuard` is a `WorkspaceGuard`); these are the names
the docs — and you — get to use.

| Codename | System |
|---|---|
| **Proof Obligation** | The voice-confirmation gate (`confirm.py`, `shellguard.py`, `toolguard.py`) — nothing side-effectful proceeds without a spoken proof |
| **Trichotomy** | The shell's three tiers — deny / quiet / confirm, every command in exactly one |
| **Compact Support** | The workspace guard and `FORBIDDEN_NAMES` — file access vanishes outside a bounded region |
| **Inverse** | The action journal and snapshots (`journal.py`, `recorder.py`) — kept so operations can be run backwards |
| **Trace** | Conversation transcripts — the record of the path actually taken |
| **Barn Door** | Speaker verification — turns away the TV and the guests, and everyone knows a barn door only half-latches |
| **Characteristic** | The wake word — membership test for "being addressed" |
| **Cauchy** | The filler hold — a trailing "um…" means the sequence hasn't converged |
| **Discontinuity** | Barge-in — a jump that ends the current segment |
| **Analytic Continuation** | Autoreload — the process is replaced, the conversation extends through it |
| **Neighborhood** | The follow-up window — an open ball around the last turn, no wake word inside |
| **Invariant** | Long-term memory — what survives every session transformation |
| **Closure** | End-of-conversation reflection — capturing the limit points before the session is discarded |
| **Atlas** | Projects — durable charts of ongoing work, with an index that says which chart to open |

Closure and Atlas together are the continuity design (the "loving partner
protocol"): sessions are deliberately short-lived, and continuity comes from
what Ciel chooses to remember, not from an ever-growing context.

## Setup

```bash
uv sync --extra piper
uv run ciel
```

First run downloads a Whisper model (~500 MB), a Piper voice (~60 MB), and the
wake-word models (~5 MB). After that it's offline except for Claude.

**Do not set `ANTHROPIC_API_KEY`.** The Agent SDK inherits Claude Code's
subscription login. Setting that variable silently overrides it and bills you
pay-as-you-go API usage instead. Ciel warns you at startup if it's set.

macOS will ask for microphone permission the first time.

## Usage

```bash
uv run ciel                  # wake word
uv run ciel --wake hotkey    # press Enter to talk — reliable in a noisy room
uv run ciel --wake always    # respond to any speech (quiet rooms only)
uv run ciel --new            # ignore the previous conversation, start fresh
uv run ciel --tts say        # macOS built-in voice instead of Piper
uv run ciel -v               # debug logging
```

## Configuration

Everything lives in `~/.ciel/config.toml`, or as `CIEL_<SECTION>_<FIELD>`
environment variables for one-off runs.

```toml
[brain]
model = "claude-opus-5"      # "claude-sonnet-5" is ~3x cheaper and faster
resume_window_minutes = 10.0  # silence beyond this rotates to a fresh session

[stt]
model = "small.en"           # base.en is 3x faster and noticeably worse
initial_prompt = "A spoken conversation with an assistant named Ciel."

[tts]
engine = "piper"
piper_voice = "en_US-lessac-medium"

[wake]
mode = "wakeword"
threshold = 0.5              # raise if the TV sets it off, lower if it ignores you

[audio]
silence_ms = 700             # how long a pause ends your turn
barge_in = false             # see below
```

```bash
CIEL_WAKE_MODE=hotkey CIEL_BRAIN_MODEL=claude-sonnet-5 uv run ciel
```

## The status pill

A small always-on-top pill sits in the corner of the screen and tells you what
Ciel is doing without your having to find the terminal.

| State | Looks like |
|---|---|
| Idle | Dim grey, "Ciel" — running, waiting for the wake word |
| Listening | Green, pulsing — capturing your speech |
| Thinking | Amber, pulsing — transcribing, or Claude is working |
| Speaking | Blue, pulsing — talking |
| Error | Red — the turn failed |

It ignores mouse clicks, never takes focus, and follows you across Spaces. Both
the dot and the pill's outline carry the state colour — a dark pill with only a
small dot vanishes against a dark terminal, which is exactly where it sits.

```toml
[ui]
indicator = "hud"           # or "terminal" (for SSH), or "none"
position = "bottom-right"   # any corner
margin = 24
opacity = 0.92
scale = 1.0                 # make it bigger
hide_when_idle = false      # true = vanish entirely when idle
```

```bash
uv run ciel --indicator terminal   # console status line instead
uv run ciel --indicator none
```

It runs as a separate process, because AppKit permanently claims whichever
thread its run loop starts on and Ciel's asyncio loop wants the main thread.
The side benefit is fault isolation: if the pill dies, Ciel keeps talking.

## File access

Off by default. Turn it on and Ciel can read, write, and edit files — inside
one directory and nowhere else.

```bash
uv run ciel --files                        # ~/.ciel/workspace
uv run ciel --workspace ~/Documents/notes  # somewhere specific
```

```toml
[files]
enabled = true
workspace = "~/Documents/notes"
read_only_outside = false   # true = read anywhere, still write only in workspace
```

**Why it's confined rather than just switched on.** Ciel acts on transcribed
speech with no confirmation step, and it reads web pages into its context. That
combination — instructions that can be misheard, untrusted text in context, and
silent execution — makes unbounded file write a genuinely bad idea. So every
path the model supplies is resolved and checked against the workspace before
the tool runs. Resolution happens first, which is what defeats `..` traversal
and symlinks pointing out of the tree.

Credentials and shell startup files (`.ssh`, `.env`, `.zshrc`, …) are refused
even if they somehow appear inside the workspace.

**Bash is guarded separately** — see the shell section below. A shell walks
straight around path confinement — `sh -c 'cat > ~/.zshrc'` — so it never gets
a place on the file allowlist. With `[shell]` disabled it sits in
`disallowed_tools` explicitly rather than merely left out, so a stray config
edit can't quietly re-enable it.

> The check is a `PreToolUse` hook, not a `can_use_tool` callback. An entry in
> `allowed_tools` auto-approves its tool *before* that callback is consulted,
> so a guard wired there never runs for the tools it's meant to constrain. If
> you extend this, keep the enforcement in the hook.

### With `workspace = "~/"`

Setting the workspace to your home directory removes the *directory* boundary,
which makes the blocklist the whole of the defense rather than a backstop. It's
correspondingly thorough. Refused anywhere under home:

| Refused | Why |
|---|---|
| `.zshrc`, `.zshenv`, `.bash_profile`, … | A writable shell config is code execution on your next login |
| `.ssh`, `.aws`, `.gnupg`, `.env`, `.npmrc`, `.docker`, `.kube` | Credentials and keys |
| `.claude`, `.claude.json`, `.config` | Agent state and auth tokens — `.claude` also holds session transcripts |
| `~/Library` | Keychains, browser cookies and history, Messages, Mail |
| `LaunchAgents`, `LaunchDaemons` | Login persistence |

Everything else under home is fair game, including every repo you have checked
out — Ciel can edit her own source at `~/jarvis`.

## Voice identity (Barn Door)

Off by default; needs a one-time enrollment. With it on, every utterance is
embedded by a local speaker model (CAM++ via sherpa-onnx, ~30 ms on CPU) and
compared against your enrolled profile *before* transcription — a stranger's
sentence never reaches Whisper, the brain, or a follow-up window.

```bash
uv sync --extra voice
uv run python scripts/enroll_voice.py          # record 4 phrases (Ciel stopped)
uv run python scripts/enroll_voice.py --test   # score yourself live
```

```toml
[voice]
enabled = true
# threshold: leave unset — enrollment *calibrates* one and stores it with
# the profile (impostor voices vs your takes); a number here overrides it.
```

The threshold is measured, not guessed: every profile change ends with a
calibration pass that scores a set of macOS `say` voices against your takes
exactly the way the gate scores, compares that with your takes'
leave-one-out agreement, and places the bar between the two distributions.
Takes that disagree with the rest of the profile get flagged for pruning —
with best-match scoring, every take is another chance for a lucky impostor
hit.

Honesty section: an embedding is a filter, not authentication. It turns away
the TV, guests, and background chatter; it will not resist a recording of
your voice, and a heavy cold moves your own score (lower the threshold that
week). Short utterances are judged against a lenient bar rather than waved
through — the threshold tapers between ~2.5 s and 600 ms, since a short
embedding is a weaker measurement — and below ~350 ms, where nothing can be
measured, *context* decides: a blip passes inside the grace window of a
verified conversation and is ignored cold. Perfect separation on short words
is information-theoretically off the table; the layering (lenient judging,
context, and Tower Clearance for anything with consequences) is the design
answer to that. No profile enrolled? The gate fails open
and says so at startup, because an identity filter that silently bricks the
assistant is worse than the strangers it filters.

## Shell access

Off by default. Turn it on and Ciel can run shell commands — behind a voice
gate that the model cannot bypass.

```toml
[shell]
enabled = true
```

Every command is classified into one of three tiers:

- **Denied outright**, even if you say yes: privilege escalation (`sudo`,
  `launchctl`, `security`, `osascript`, …), anything touching credential or
  shell-startup paths, `curl | sh` shapes, and recursive-forced `rm` aimed at
  home or root. A "yes" to a misheard question must not be able to do these.
- **Quietly allowed**: a conservative read-only allowlist (`git status`,
  `git log`, `ls`, `pwd`, …) runs without asking. Configurable via
  `auto_allow`; asking aloud for every status check would train you to say
  yes reflexively.
- **Confirmed**: everything else. Ciel reads the command aloud — *"Run: touch
  notes dot txt — okay?"* — and waits for a spoken yes or no. Silence (about
  eight seconds), a "no", or two unclear answers all refuse the command.

The confirmation is enforced, not requested: it lives in a `PreToolUse` hook
that blocks the tool call until the pipeline has captured and transcribed your
answer, so neither a misheard instruction nor a prompt-injected web page can
run a side-effectful command without your voiced yes. Commands the classifier
can't see through — redirections, substitutions, backgrounding — always land
in the confirm tier. `scripts/probe_shellguard.py` drives the whole
choreography with fakes.

### macOS may block folders independently

`~/Desktop`, `~/Documents`, and `~/Downloads` are TCC-protected. Whether Ciel
can reach them depends on permissions granted to the *terminal app* you launch
her from, not on anything in this config. macOS prompts on first access; if it
was denied once, grant it again under System Settings → Privacy & Security →
Files and Folders.

This also makes verification confusing: a shell without Desktop permission
reports "Operation not permitted" for a file Ciel wrote perfectly well. Check
with `stat` rather than `ls`, or just ask Ciel to read it back.

## Memory

Ciel saves things it learns, without being asked, as one Markdown file per fact
in `~/.ciel/memory/`. Open the directory to see exactly what it believes about
you; delete a file to make it forget.

Three mechanisms, deliberately separate:

- **Conversation continuity** — a thread survives 10 minutes of silence
  (resume across restarts included); past that the session is rotated away
  and the next conversation starts on a clean, fast context. This is what
  `--new` forces early.
- **Durable knowledge (Invariant)** — outlives any session. About a minute
  after each conversation ends, Ciel runs one silent reflection turn
  (Closure — you'll see `[reflecting]` in the console) and commits anything
  durable to these files while the session still remembers it.
- **Projects (Atlas)** — durable working state for anything spanning
  conversations, one file per project in `~/.ciel/projects/`. Say "let's get
  back to the wake word project" and Ciel opens the file rather than trusting
  its recollection; it updates the state and logs milestones as work moves.

Only the one-line summaries go into the prompt each turn. Full contents load on
demand, which is what keeps memory affordable as it grows.

## Barge-in (off by default)

Talking over Ciel to interrupt it is implemented but **disabled**, because on
laptop speakers it doesn't work reliably. There's no acoustic echo cancellation,
so the microphone hears Ciel's own voice. Measured on this hardware: a silent
room sits around 0.085 RMS and Ciel speaking reaches 0.18 — not enough
separation to reliably tell "the user is interrupting" from "Ciel is talking".

**On headphones there's no echo path and it works properly.** Turn it on:

```toml
[audio]
barge_in = true
```

The detector adapts to your room's noise floor rather than using a fixed
threshold, so it survives both a silent room and a noisy one.

## Extending it

**A new capability Ciel can invoke** — add a module under
`src/ciel/brain/tools/` with `@tool`-decorated functions and append them to
`TOOLS` in that package's `__init__.py`. Nothing else changes; they're exposed
through an in-process MCP server and auto-allowed.

**An external service** (Google Calendar, Gmail, Slack) — those are MCP servers,
so they're a connection entry in config and need no code at all.

**A different speech engine** — implement the `SpeechToText` or `TextToSpeech`
protocol and change one line in `config.py`. The protocols exist precisely so
`faster-whisper` → `mlx-whisper`, or Piper → ElevenLabs, isn't a rewrite.

## Personalizing the wake word

To make it answer to "Ciel", train a custom openWakeWord model — free and
offline; their notebook generates synthetic clips with Piper and outputs
`.onnx`. Point `wake.model` at the file.

Train **"hey ciel"**, not bare "Ciel". One-syllable wake words have much higher
false-trigger rates; the two-syllable prefix gives the detector enough to work
with. You'd still address it as Ciel.

## Development probes

Each isolates one layer, so when something misbehaves you can tell which half to
blame:

```bash
uv run scripts/probe_audio.py vad     # endpointing, synthetic speech, no mic
uv run scripts/probe_audio.py mic     # live capture -> /tmp/ciel_capture.wav
uv run scripts/probe_voice.py speak   # TTS + playback only
uv run scripts/probe_voice.py barge   # interrupt path
uv run scripts/probe_voice.py echo    # mic -> STT -> TTS, no model in the loop
```

## Cost

Roughly **$0.011 per conversational turn** on Opus 5 in steady state, dominated
by a ~20 k-token cached prompt prefix that's re-read every turn. The first turn
of a session costs more (~$0.043) because it writes that cache; a gap longer
than five minutes pays it again.

Web-search turns are much more expensive — around **$0.21** — because search
results land in the context.

Switching `brain.model` to `claude-sonnet-5` cuts this substantially at some
cost in research judgment.

> A monthly Agent SDK credit (Pro $20 / Max 5x $100 / Max 20x $200) has been
> announced but isn't live on all accounts yet. Until it is, this usage draws on
> your plan's ordinary limits — the same pool as Claude Code.

## How it fits together

```
mic ──▶ wake word ──▶ VAD capture ──▶ whisper ──▶ Claude ──▶ piper ──▶ speaker
                                                     │
                                          memory + web search
```

One loop reads the microphone and routes each frame by state: to the wake
detector when idle, the endpointer while you're talking, the barge-in check
while Ciel is. Replies stream back sentence by sentence, so Ciel starts speaking
as soon as the first complete thought exists rather than after the whole answer.

| Module | Role |
|---|---|
| `pipeline.py` | The loop and the state machine |
| `config.py` | Every swappable choice, in one place |
| `audio/` | Capture, endpointing, playback, wake |
| `stt/`, `tts/` | Engine protocols and implementations |
| `brain/` | Claude client, system prompt, sessions, tools |
| `memory/` | The durable file-backed store |
| `ui/` | The status pill (`hud.py` is its own process) |
