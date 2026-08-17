"""Configuration for Ciel.

Every swappable choice in the system lands here as a plain value: which STT
engine, which voice, which model, where memory lives. Changing an engine should
mean editing one line in this file, never touching the pipeline.

Values come from defaults, overridden by ``~/.ciel/config.toml`` if it exists,
overridden by ``CIEL_*`` environment variables.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

# ── Audio constants ──────────────────────────────────────────────────────────
# 16 kHz mono int16 is the common denominator: webrtcvad requires it, Whisper
# wants it, and openWakeWord expects it. Resampling anywhere in the chain is
# pure loss, so the microphone is opened at this rate and it never changes.
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # bytes per sample (int16)
CHANNELS = 1

# webrtcvad only accepts 10, 20, or 30 ms frames. 30 ms is the cheapest to
# process and plenty precise for detecting the end of an utterance.
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH  # 960


@dataclass(frozen=True, slots=True)
class AudioConfig:
    """Microphone capture and utterance endpointing."""

    input_device: int | str | None = None
    """Input device index or substring of its name. None = system default."""

    output_device: int | str | None = None

    vad_aggressiveness: int = 2
    """webrtcvad mode, 0-3. Higher filters more non-speech but clips soft
    speech. 2 is a reasonable desk-microphone default."""

    silence_ms: int = 500
    """How long the user must stop talking before the turn is considered over.

    Short on purpose — this wait is the single largest piece of perceived
    latency, paid on every turn. The cost of cutting in on a genuine
    mid-sentence pause is softened by the filler extension below: a pause
    right after "um" or a dangling "and" gets ``filler_extend_ms`` instead of
    this. Raise it back toward ~700 if Ciel still interrupts you."""

    filler_extend_ms: int = 1500
    """Extra listening when what you've said so far ends mid-thought — a
    filler ("um", "uh") or a dangling conjunction ("and", "so"). The partial
    utterance is held rather than answered; speak again within the window and
    both halves are joined into one turn. If the window lapses, Ciel answers
    what it has. Set to 0 to disable holding entirely."""

    filler_words: tuple[str, ...] = (
        "um", "umm", "uh", "uhh", "er", "erm", "hmm", "hm",
        "and", "or", "but", "so", "because",
    )
    """Trailing words that signal an unfinished thought, matched against the
    last transcribed word. A trailing ellipsis counts as trailing off too —
    but an utterance the transcriber closed with ``.``/``!``/``?`` is taken as
    complete regardless, which is what keeps "I think so." from being held as
    a dangling "so"."""

    followup_ms: int = 5000
    """How long to keep listening after Ciel speaks before the wake word is
    required again.

    An answer usually invites a reply, and demanding "hey jarvis" before every
    one of them turns a conversation into a series of unrelated queries. The
    window only covers *starting* to speak: once you've begun, the ordinary
    endpointer takes over and a long sentence is never cut off at the deadline.

    Set to 0 to disable and require the wake word every turn."""

    wake_timeout_ms: int = 8000
    """How long to keep listening after a wake with no speech before giving up
    and requiring the wake word again. A false wake — the TV, a similar-sounding
    phrase — would otherwise leave Ciel latched in "listening" indefinitely.
    Set to 0 to disable the timeout."""

    max_utterance_s: float = 30.0
    """Hard cap so a stuck VAD can't record forever."""

    min_utterance_ms: int = 250
    """Utterances shorter than this are discarded as coughs, clicks, and door
    slams rather than sent to a transcription model."""

    preroll_ms: int = 300
    """Audio retained from *before* speech was detected. Without this the first
    phoneme is clipped, and Whisper mistranscribes the opening word."""

    barge_in: bool = False
    """Let the user interrupt Ciel mid-sentence by talking over it.

    Off by default, and that default is measured rather than cautious. There is
    no acoustic echo cancellation here, so on open speakers the microphone
    hears Ciel's own voice. On this hardware, a silent room measured ~0.085 RMS
    and Ciel speaking measured ~0.18 — barely a two-to-one separation, and well
    inside the range ordinary speech occupies. Any threshold sensitive enough
    to catch you interrupting also catches Ciel interrupting itself, which is a
    far worse failure than not being interruptible.

    **On headphones there is no echo path and this works properly — turn it
    on.** On speakers, leave it off unless you have tuned the values below
    against your own room using `scripts/probe_bargein.py`."""

    barge_in_ratio: float = 2.5
    """How far above the ambient noise floor a sound must be to count.

    A ratio rather than an absolute level because the floor is not a constant:
    the same room measured 0.015 RMS quiet and 0.085 with background noise, so
    any fixed threshold is either deaf in one condition or hair-triggered in
    the other. The floor is tracked continuously and this multiplies it."""

    barge_in_floor: float = 0.02
    """Absolute minimum, regardless of ratio. Stops a pathologically silent
    room (floor near zero) from making any tiny sound qualify."""

    barge_in_frames: int = 8
    """Consecutive qualifying frames required (~240 ms). A single loud frame is
    a cough or a door; sustained speech is someone actually talking."""


@dataclass(frozen=True, slots=True)
class WakeConfig:
    """How Ciel decides it's being addressed."""

    mode: Literal["wakeword", "hotkey", "always"] = "wakeword"
    """``hotkey`` is the reliable fallback and how the pipeline gets tested
    before the microphone is trusted. ``always`` skips waking entirely and
    responds to any speech — only sane alone in a quiet room."""

    model: str = "hey_jarvis"
    """An openWakeWord pretrained name, or a path to a custom ``.onnx``.
    Ciel is *named* Ciel but answers to "hey jarvis" until a custom model is
    trained — see the plan's wake-word section."""

    threshold: float = 0.5
    """0-1. Raise it if the TV sets Ciel off; lower it if it ignores you."""

    vad_threshold: float = 0.3
    """openWakeWord's built-in speech gate, which suppresses a good deal of
    non-speech noise before the wake model ever runs."""

    ack: bool = True
    """Speak a brief acknowledgement when the wake fires, so you know Ciel is
    listening without looking at the indicator. Ignored in ``always`` mode,
    where there is no wake moment to acknowledge."""

    ack_phrases: tuple[str, ...] = ("Uh huh?", "Yes?", "Hm?")
    """Acknowledgements to pick from at random ("Yes sir?" if that's more your
    style). Keep them to a syllable or two: the microphone is drained while one
    plays, so a long phrase eats the opening of a command spoken in the same
    breath as the wake word."""


@dataclass(frozen=True, slots=True)
class STTConfig:
    engine: Literal["faster-whisper"] = "faster-whisper"

    model: str = "small.en"
    """``base.en`` is faster and noticeably worse; ``medium.en`` is better and
    roughly triples latency. ``small.en`` is the sweet spot on Apple Silicon."""

    compute_type: str = "int8"
    """int8 is ~2x faster than float32 on CPU with negligible accuracy loss."""

    device: str = "cpu"
    """faster-whisper uses CTranslate2, which has no Metal backend — "cpu" is
    the only real option here. For GPU on Apple Silicon the answer is a
    different engine (mlx-whisper), which is why this sits behind a protocol."""

    language: str | None = "en"
    beam_size: int = 1
    """Greedy decoding. Beam search buys very little on short conversational
    utterances and costs latency we can't spare."""

    initial_prompt: str = "A spoken conversation with an assistant named Ciel."
    """Biases the decoder toward vocabulary it would otherwise mangle. Without
    it "Ciel" transcribes as "ZL" or "Seal" — the model has never seen the name
    and picks whatever is phonetically close. Costs nothing at inference time.
    Add domain jargon and proper nouns you use often for the same reason.

    Phrase it as a *description*, not as leading dialogue. Whisper treats this
    as preceding context, so a prompt ending in "Ciel," makes the decoder read
    a real leading "Ciel," in the audio as already-transcribed and drop it."""


@dataclass(frozen=True, slots=True)
class TTSConfig:
    engine: Literal["say", "piper"] = "piper"
    """``piper`` is a local neural voice; ``say`` is the macOS built-in.

    Piper is the default on measurement, not taste: it reached first audio in
    ~140 ms against ~500 ms for `say` — because it streams chunks as it
    synthesizes rather than rendering a whole file first — *and* it sounds
    considerably more human. The only cost is a one-time ~60 MB voice download,
    after which it is strictly better on both axes.

    Falls back to ``say`` automatically if the voice can't be loaded, so a
    failed download degrades to a working assistant rather than a broken one."""

    voice: str = "Samantha"
    """A macOS voice name for ``say``; ignored by Piper."""

    rate: int = 190
    """Words per minute for ``say``. macOS default is 175, which drags a little
    in conversation."""

    piper_voice: str = "en_US-lessac-medium"
    piper_voice_dir: Path | None = None

    piper_speed: float = 1.0
    """Speaking rate multiplier for Piper. Above 1 is faster, below is slower.

    Piper itself thinks in ``length_scale``, where *larger* means *slower* —
    the inverse of this. Exposed the other way round because "speed 1.2" is
    what a person means when they ask Ciel to talk faster, and an inverted knob
    invites setting it exactly backwards.

    Past about 1.3 the neural voice starts to slur; below 0.8 it drags."""


@dataclass(frozen=True, slots=True)
class FilesConfig:
    """Whether Ciel can touch the filesystem, and where."""

    enabled: bool = False
    """Off by default. Ciel acts on transcribed speech, reads web pages into
    its context, and file tools run without a confirmation step (only shell
    commands get one) — so file access is a real decision, not a default.
    Turn it on deliberately."""

    workspace: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "workspace"
    )
    """The only directory Ciel may write in. Every path the model supplies is
    resolved and checked against this tree, so `..` and symlinks cannot escape
    it. Point it at a notes folder or a scratch directory — pointing it at your
    home directory gives up the entire protection."""

    read_only_outside: bool = False
    """Allow reading anywhere while still confining writes to the workspace.
    Convenient for "summarize this file", and a meaningful widening: Ciel can
    then read anything you can, including things you would not have chosen to
    show it."""


@dataclass(frozen=True, slots=True)
class ShellConfig:
    """Whether Ciel may run shell commands, and which ones need a spoken yes.

    A shell walks around the file guard's path checks by construction, so this
    is not another file permission — it is its own gate with its own rules
    (``brain/shellguard.py``). Every command lands in one of three tiers:
    hard-denied (never runs, even if you say yes), quietly allowed (read-only
    prefixes below), or confirmed — Ciel reads the command aloud and waits for
    a spoken yes or no before running it. The confirmation is enforced in a
    PreToolUse hook, not requested in the prompt: the model cannot run a
    confirm-tier command without your voiced yes."""

    enabled: bool = False
    """Off by default for the same reason file access is: instructions arrive
    as transcribed speech and untrusted web text enters the context. The voice
    gate is what makes enabling this defensible at all — turn it on knowing
    the gate is the defense."""

    auto_allow: tuple[str, ...] = (
        "git status", "git log", "git diff", "git show", "git branch",
        "ls", "pwd", "date", "whoami", "uname",
    )
    """Command prefixes that run without a spoken confirmation. Matched per
    pipeline segment against leading whole tokens — "git status" covers
    "git status -sb" — and only for commands with no redirection,
    substitution, or backgrounding, which escalate to a confirmation
    regardless. Deliberately absent: ``cat``, ``head``, ``tail`` — a quiet
    read-anything is ``files.read_only_outside`` by another name, minus the
    path checks."""

    deny_extra: tuple[str, ...] = ()
    """Extra leading tokens to hard-refuse, on top of the built-in set (sudo,
    launchctl, security, osascript, shutdown, and friends). For tools you know
    you never want run by voice — "docker", say."""

    confirm_timeout_ms: int = 8000
    """How long a confirmation question hangs in the air before silence counts
    as no. Covers *starting* to answer only — once you're speaking, the
    endpointer takes over and a slow "hmm... yes, go ahead" is never cut off
    at the deadline. Matches ``wake_timeout_ms`` because it is the same social
    contract: a question waits about this long for an answer."""

    max_command_display_chars: int = 120
    """Commands longer than this are truncated in the spoken prompt. The point
    of reading a command aloud is that you hear what is about to run, but past
    a couple of lines nobody absorbs shell syntax by ear — the truncation
    ("...and so on") at least tells you there is more than you heard."""


@dataclass(frozen=True, slots=True)
class JournalConfig:
    """The action journal: what Ciel did recently, kept so it can be undone.

    Mishearings surface *after* the action — the wrong file edited, an event
    on the wrong day — so alongside the confirmation gate (consent) there is
    a journal (regret). Every mutating tool call is recorded, and file edits
    snapshot the previous contents first. Undo is the model using its normal
    tools against this record, which keeps undo actions inside the same
    guards and confirmations as everything else."""

    enabled: bool = True
    """On by default, unlike the capabilities it watches: recording what
    happened has no blast radius, and the first time it is needed is exactly
    the moment it is too late to enable."""

    max_entries: int = 50
    """How many actions are kept. Floor of 10 enforced in the journal —
    fewer than that and "undo what you did a few minutes ago" already fell
    off the end. Snapshots are pruned with their entries, so this also
    bounds disk."""

    max_snapshot_kb: int = 1024
    """Files larger than this are not snapshotted before an edit; the journal
    entry says so instead. Bounds the cost of editing something huge — and a
    file that size is usually generated, not precious."""

    dir: Path = field(default_factory=lambda: Path.home() / ".ciel" / "undo")
    """Where the journal and its snapshots live. Inside the state dir, so
    Ciel can read its own snapshots back when restoring one."""


@dataclass(frozen=True, slots=True)
class BrainConfig:
    model: str = "claude-opus-5"

    allowed_tools: tuple[str, ...] = ("WebSearch", "WebFetch")
    """The Agent SDK ships the full Claude Code toolset — Bash, Write, Edit and
    all. A voice assistant that can silently run shell commands is not what we
    want, so this is an explicit allowlist, paired with permission_mode
    "dontAsk" to deny everything absent from it. File tools ride in via
    ``[files]`` and Bash via ``[shell]`` (with its voice gate) rather than
    this list, so enabling those features can't be done without also enabling
    their guards."""

    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    """Reasoning depth. ``None`` uses the model default (high).

    Measured on this setup, effort is *not* the cost lever you'd expect for
    conversation: chat turns emit only 6-20 output tokens because adaptive
    thinking correctly decides not to think hard about "what's my name", so
    low/medium/high cost the same. It starts to matter on research turns, where
    you actively want the depth. Left at the default for that reason — turn it
    down only if research answers feel over-deliberated."""

    speak_thinking: bool = True
    """Speak the model's reasoning aloud as it thinks, before the answer.

    On research turns the model can reason for many seconds, and that silence
    reads as a hang; verbalizing the reasoning fills it and shows where the
    answer is coming from. Turn off for terse turns that jump straight to the
    answer."""

    thinking_chime: bool = True
    """Play a soft tone when spoken reasoning ends and the answer begins.

    The same voice speaks both, so without a boundary marker the listener
    can't tell where deliberation stops and the conclusion starts. Only
    matters when ``speak_thinking`` is on."""

    max_turns: int | None = None

    resume_window_hours: float = 12.0
    """Resume the previous conversation if it's newer than this; otherwise
    start fresh. Stops Ciel from picking up a week-old thread mid-sentence."""

    sentence_flush_chars: int = 180
    """If a "sentence" runs past this without terminal punctuation, flush it to
    speech anyway. Guards against a long list or a code-ish response leaving
    the user in silence forever."""


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: bool = True

    dir: Path = field(default_factory=lambda: Path.home() / ".ciel" / "memory")
    """Deliberately outside the repo: deleting the code must not delete what
    Ciel knows about you. Also deliberately *not* ~/.claude/ — Ciel's memory is
    its own, not your Claude Code context."""

    max_index_entries: int = 200
    """The index goes into every system prompt, so it has a budget. Past this,
    memories need consolidating."""


@dataclass(frozen=True, slots=True)
class MessagesConfig:
    """Reading and sending iMessage."""

    enabled: bool = False
    """Off by default. Reading the Messages database means handing Ciel your
    entire text history, which is a decision to make deliberately rather than
    inherit from a default."""

    allow_send: bool = False
    """Separate switch, and separate for a reason: reading a message back to
    you is recoverable, and sending one is not. Enabling this lets a
    misheard sentence become a text someone else receives."""

    db_path: Path = field(
        default_factory=lambda: Path.home() / "Library" / "Messages" / "chat.db"
    )
    """Messages' own SQLite database. Opened read-only. Reading it requires
    Full Disk Access for whatever runs Ciel — without it, every read fails with
    a permission error rather than an empty result."""

    max_history: int = 50
    """Ceiling on how many messages one read can return. These go into the
    model's context and get read aloud; a request for "my messages" that pulls
    five hundred is expensive and useless."""

    max_message_chars: int = 1000
    """Longest message Ciel will send. Dictated messages are short; a very long
    one means something has gone wrong upstream."""

    script_timeout_s: float = 20.0
    """How long to wait on osascript. A first call often blocks on a macOS
    permission dialog, which needs longer than a normal call but must not hang
    the assistant forever."""


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """One external MCP server — a connector to an outside service.

    Configured as ``[mcp.<name>]`` tables in ``~/.ciel/config.toml`` (TOML
    only; these are nested structures, so ``CIEL_*`` env vars don't reach
    them). Each server authenticates on its own terms — an OAuth flow, a
    token file, an API key — so the account it uses has nothing to do with
    the Claude subscription running the brain. Sign a calendar server into
    whichever Google account you like::

        [mcp.gcal]
        command = "npx"
        args = ["-y", "@cocal/google-calendar-mcp"]
        env = { GOOGLE_OAUTH_CREDENTIALS = "/Users/you/gcal-oauth.json" }
        tools = ["list-events", "search-events"]

        [mcp.notion]
        url = "https://mcp.notion.com/mcp"

    Remember what Ciel is before connecting anything that can *act*: it
    executes tools off transcribed speech, and tools on the ``tools`` list run
    with no confirmation step. Reading a calendar is comfortable that way;
    anything that sends, posts, or deletes belongs on the ``confirm`` list —
    allowed, but held for a spoken yes — or on no list at all."""

    command: str | None = None
    """Executable for a local (stdio) server. Mutually exclusive with url."""

    args: tuple[str, ...] = ()

    env: dict[str, str] = field(default_factory=dict)
    """Environment for the server process — token paths, API keys. These live
    here, not in Ciel's own environment, so each server sees only its own."""

    url: str | None = None
    """Endpoint for a remote (HTTP) server. Mutually exclusive with command."""

    headers: dict[str, str] = field(default_factory=dict)
    """Extra headers for a remote server, e.g. an Authorization token."""

    transport: Literal["http", "sse"] = "http"
    """Remote transport. Only consulted when ``url`` is set; older servers
    may still want ``sse``."""

    tools: tuple[str, ...] | None = None
    """Which of the server's tools Ciel may call *silently*. ``None`` allows
    all of them — fine for read-only servers, too generous for ones that can
    send or modify. Names are the server's own (unprefixed)."""

    confirm: tuple[str, ...] = ()
    """Tools Ciel may call only after your spoken yes — the same voice gate
    that fronts shell commands, enforced in a hook the model cannot skip.
    Allowed *in addition to* ``tools``, so the two lists split a server into
    a quiet tier and a confirmed tier: reads on ``tools``, writes here. This
    is the tier for actions that leave the machine — sending mail, changing
    a calendar — where "recoverable" stops being true."""


@dataclass(frozen=True, slots=True)
class DevConfig:
    """Working on Ciel while Ciel is running."""

    autoreload: bool = True
    """Watch Ciel's own source and restart automatically when it changes.

    The restart is a clean re-exec, deferred until Ciel is idle — never
    mid-conversation — and announced out loud. The previous conversation
    resumes through the session file, so the only cost is startup time.
    ``touch ~/.ciel/reload`` forces one without editing any source."""


@dataclass(frozen=True, slots=True)
class TranscriptConfig:
    """Local record of every conversation."""

    enabled: bool = True
    """Save what was said — one CSV file per conversation, created only once
    something is actually said. Delete a file to forget that conversation;
    nothing reads them back."""

    dir: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "transcripts"
    )
    """Where the CSVs land. Same reasoning as memory: outside the repo, so
    deleting the code doesn't delete the record."""


@dataclass(frozen=True, slots=True)
class UIConfig:
    """The on-screen status indicator."""

    indicator: Literal["hud", "terminal", "none"] = "hud"
    """``hud`` is a floating always-on-top pill; ``terminal`` prints state to
    the console (useful over SSH, where a window would appear on the wrong
    machine); ``none`` disables it."""

    position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "bottom-right"

    margin: int = 24
    """Distance from the screen edge, in points. Measured against the *visible*
    frame, so it already accounts for the menu bar and Dock."""

    opacity: float = 0.92

    scale: float = 1.0
    """Size multiplier, for high-density displays or simply wanting it bigger."""

    hide_when_idle: bool = False
    """Hide entirely when Ciel is idle rather than dimming.

    Off by default: a dimmed pill still answers "is Ciel running?", which is
    most of the reason to have an indicator at all."""


@dataclass(frozen=True, slots=True)
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    transcripts: TranscriptConfig = field(default_factory=TranscriptConfig)
    dev: DevConfig = field(default_factory=DevConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    mcp: dict[str, MCPServerConfig] = field(default_factory=dict)
    """External connectors, keyed by server name — see MCPServerConfig."""

    state_dir: Path = field(default_factory=lambda: Path.home() / ".ciel")
    log_level: str = "INFO"

    @property
    def session_file(self) -> Path:
        return self.state_dir / "session.json"


# ── Loading ──────────────────────────────────────────────────────────────────

_SECTIONS = {
    "audio": AudioConfig,
    "wake": WakeConfig,
    "stt": STTConfig,
    "tts": TTSConfig,
    "brain": BrainConfig,
    "memory": MemoryConfig,
    "files": FilesConfig,
    "shell": ShellConfig,
    "journal": JournalConfig,
    "messages": MessagesConfig,
    "transcripts": TranscriptConfig,
    "dev": DevConfig,
    "ui": UIConfig,
}


def _coerce(value: Any, target_type: Any) -> Any:
    """Best-effort coercion of TOML/env strings into the dataclass field type.

    Kept intentionally shallow: this handles the scalar cases that actually
    show up in a config file. Anything exotic is passed through untouched and
    will fail loudly at its use site rather than being silently mangled here.
    """
    if value is None:
        return None

    # Unwrap optionals and unions: `int | None` should still coerce to int.
    # Only unions are unwrapped — get_args on a subscripted type like
    # `tuple[str, ...]` would dissolve the type itself into its arguments.
    if get_origin(target_type) in (Union, UnionType):
        members = set(get_args(target_type))
    else:
        members = {target_type}
    members.discard(type(None))

    if Path in members:
        return Path(value).expanduser()
    if bool in members:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    # `int | str` device specs: a purely numeric string is a device index,
    # anything else is a device-name fragment.
    if int in members and str in members:
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
        return value
    if int in members:
        return int(value)
    if float in members and str not in members:
        return float(value)
    if any(get_origin(m) is tuple for m in members):
        # From TOML this arrives as a list; from an env var it is one
        # comma-separated string.
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return tuple(value)
    return value


def _apply(section: Any, overrides: dict[str, Any]) -> Any:
    # get_type_hints, not fields(): `from __future__ import annotations` makes
    # `field.type` a *string* ("int"), so matching on it silently coerces
    # nothing and env vars stay strings — which then explodes at arithmetic.
    types = get_type_hints(type(section))
    valid = {f.name for f in fields(section)}
    clean = {
        key: _coerce(value, types.get(key))
        for key, value in overrides.items()
        if key in valid
    }
    return replace(section, **clean) if clean else section


def load_config(path: Path | None = None) -> Config:
    """Assemble config from defaults, then TOML, then environment.

    Environment variables are named ``CIEL_<SECTION>_<FIELD>``, e.g.
    ``CIEL_TTS_ENGINE=piper`` or ``CIEL_WAKE_MODE=hotkey``. They win over the
    file, which makes one-off runs easy without editing anything.
    """
    cfg = Config()

    path = path or (Path.home() / ".ciel" / "config.toml")
    if path.exists():
        raw = tomllib.loads(path.read_text())
        updates: dict[str, Any] = {}
        for name, _ in _SECTIONS.items():
            if name in raw:
                updates[name] = _apply(getattr(cfg, name), raw[name])
        if "mcp" in raw and isinstance(raw["mcp"], dict):
            # Nested tables, so the flat-section machinery doesn't apply;
            # each [mcp.<name>] becomes one MCPServerConfig.
            updates["mcp"] = {
                name: _apply(MCPServerConfig(), entry)
                for name, entry in raw["mcp"].items()
                if isinstance(entry, dict)
            }
        for key in ("state_dir", "log_level"):
            if key in raw:
                updates[key] = _coerce(raw[key], Path if key == "state_dir" else str)
        cfg = replace(cfg, **updates)

    env_updates: dict[str, Any] = {}
    for name in _SECTIONS:
        section = getattr(cfg, name)
        prefix = f"CIEL_{name.upper()}_"
        found = {
            k[len(prefix):].lower(): v
            for k, v in os.environ.items()
            if k.startswith(prefix)
        }
        if found:
            env_updates[name] = _apply(section, found)
    if "CIEL_LOG_LEVEL" in os.environ:
        env_updates["log_level"] = os.environ["CIEL_LOG_LEVEL"]
    if env_updates:
        cfg = replace(cfg, **env_updates)

    return cfg
