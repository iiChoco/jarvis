"""Configuration for Ciel.

Every swappable choice in the system lands here as a plain value: which STT
engine, which voice, which model, where memory lives. Changing an engine should
mean editing one line in this file, never touching the pipeline.

Values come from defaults, overridden by ``~/.ciel/config.toml`` if it exists,
overridden by ``CIEL_*`` environment variables.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

log = logging.getLogger(__name__)

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

    vad_floor_ratio: float = 1.5
    """The endpointer's energy gate: a frame counts as speech only when VAD
    says so AND its loudness clears the ambient noise floor by this factor —
    the same floor-times-ratio shape barge-in uses. Exists because webrtcvad
    calls steady broadband noise (fans, white noise) speech, which kept
    utterances from ever endpointing in a noisy room. In a quiet room the
    floor is near zero and the gate changes nothing. Raise it if noise still
    reads as speech; lower toward 1.0 if soft speech from across the room is
    being clipped."""

    silence_ms: int = 500
    """How long the user must stop talking before the turn is considered over.

    Short on purpose — this wait is the single largest piece of perceived
    latency, paid on every turn. The cost of cutting in on a genuine
    mid-sentence pause is softened by the hold below: a transcript that reads
    as unfinished gets ``filler_extend_ms`` instead of this. Raise it back
    toward ~700 if Ciel still interrupts you."""

    filler_extend_ms: int = 1500
    """Extra listening when what you've said so far ends mid-thought. The
    partial utterance is held rather than answered; speak again within the
    window and both halves are joined into one turn. If the window lapses,
    Ciel answers what it has. Set to 0 to disable holding entirely.

    "Mid-thought" is judged from the transcript, pessimistically: a trailing
    ellipsis, a transcript with no closing punctuation at all, or a final word
    from ``dangling_words`` below. The pessimism is deliberate — Whisper
    strips fillers ("um", "uh") from transcripts, so a hold that waits to
    *see* a filler misses exactly the pauses fillers announce. The worst case
    of holding a finished-but-unpunctuated sentence is this many milliseconds
    of extra wait; the worst case of the optimistic reading is answering half
    a thought."""

    dangling_words: tuple[str, ...] = (
        "um", "umm", "uh", "uhh", "er", "erm", "hmm", "hm",
        "and", "or", "but", "because",
        "the", "a", "an", "my", "your", "our", "their", "his",
        "to", "of",
    )
    """Words that cannot end a finished thought, checked against the last
    transcribed word even when the transcriber closed the sentence with a
    period — Whisper appends one to fragments out of habit, and "check my."
    is not a closed thought. Deliberately absent: "so" ("I think so.") and
    particles like "on"/"in"/"up" ("turn it on."), which end real sentences
    all the time. A ``?`` or ``!`` is always trusted as complete — Whisper
    doesn't punctuate fragments as questions, and "what are you waiting for?"
    must not hang on its preposition."""

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
    hears Ciel's own voice. On this hardware the room measured ~0.015 RMS
    quiet (~0.085 with background noise) and Ciel speaking reached ~0.18 —
    with the background up, barely a two-to-one separation, and well inside
    the range ordinary speech occupies. Any threshold sensitive enough to
    catch you interrupting also catches Ciel interrupting itself, which is a
    far worse failure than not being interruptible.

    **On headphones there is no echo path and this works properly — turn it
    on.** On speakers, leave it off unless you have tuned the values below
    against your own room using `scripts/probe_voice.py barge`."""

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
    The pretrained default answers to "hey jarvis"; a trained "hey ciel"
    model dropped in as a path answers to the name — see the README's
    wake-word section, and qualify a candidate model with
    ``scripts/probe_wake_model.py`` before pointing this at it."""

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

    greeting_phrases: tuple[str, ...] = ("Ciel here.",)
    """Spoken once, at process launch, before anything else — picked at
    random so every boot doesn't sound identical. Unlike ``ack_phrases``
    nothing is waiting on this one to finish (no command follows in the same
    breath), so a slightly longer phrase is fine."""


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """Speaker verification — only the enrolled voice gets a response.

    Codename Barn Door. Every utterance is embedded (~30 ms, local ONNX
    model) and
    compared against the enrolled profile before transcription; strangers,
    the TV, and background conversations are dropped silently. This is a
    *filter*, not authentication — it will not resist a recording of your
    voice — but combined with Tower Clearance it means a stranger can
    neither start an action nor plausibly confirm one."""

    enabled: bool = False
    """Off by default because it needs enrollment first: run
    ``scripts/enroll_voice.py``, then turn this on. Enabled without a
    profile, the gate fails open and says so at startup — a filter that
    silently bricks the assistant would be worse than the strangers it
    filters."""

    threshold: float | None = None
    """Cosine similarity at or above which an utterance counts as you.

    ``None`` — the default, and the right setting — uses the threshold the
    enrollment script *calibrated* and stored with the profile: it scores a
    set of impostor voices against your actual takes and places the bar
    between their best score and your worst, which no fixed number can do.
    (0.5 is the fallback for an uncalibrated profile.) Set a number here
    only to override the calibration, and prefer re-running
    ``enroll_voice.py --calibrate`` instead: best-match scoring over a
    growing take set shifts the impostor statistics, so any hand-set number
    quietly goes stale as the profile grows."""

    min_utterance_ms: int = 600
    """Where the short-utterance leniency reaches its maximum: at and below
    this duration the full ``short_discount`` applies, tapering away toward
    ``full_confidence_s``. Short utterances are still *judged* — weak
    evidence is not zero evidence, and a half-second "yes" from a clearly
    different voice scores low enough to reject even against a lenient
    bar."""

    min_judge_ms: int = 350
    """Below this there is genuinely too little audio to embed — a cough, a
    syllable — and no bar would mean anything. Such blips pass only inside
    the grace window of a verified conversation (where "yes" and "no"
    actually matter) and are ignored cold: an unverifiable sound from
    nowhere is not a command."""

    short_discount: float = 0.12
    """Extra threshold leniency for short utterances, at its maximum right
    at the ``min_utterance_ms`` floor and tapering to zero by
    ``full_confidence_s``. An embedding of one second of speech is a much
    weaker measurement than one of three — the same speaker legitimately
    scores lower on short commands — and demanding full-sentence confidence
    from "what time is it" is what makes a gate feel broken. A clearly
    different voice still fails the relaxed bar by a wide margin."""

    full_confidence_s: float = 2.5
    """Speech duration at which the embedding is trusted in full and the
    short-utterance discount reaches zero. Beyond a couple of seconds, more
    audio stops changing the embedding much."""

    recent_margin: float = 0.05
    """How much the threshold relaxes shortly after a verified pass. The
    costliest false rejection is the mid-conversation one — you asked,
    Ciel answered, your follow-up bounces — and the person speaking two
    sentences after a verified sentence is overwhelmingly the same person.
    Set to 0 for a strict gate."""

    recent_window_s: float = 30.0
    """How long the relaxed threshold lasts after a verified pass. A pass
    within the window extends it, so an ongoing conversation stays easy and
    the margin evaporates once you walk away."""

    keep_rejected: int = 10
    """Rejected utterances kept as wav files next to the profile (a ring of
    the newest N; 0 disables). This is the training loop: run
    ``enroll_voice.py --adopt`` to listen to each rejection and fold the
    ones that are actually you into the profile — the gate learns precisely
    the conditions it failed in. Local, bounded, and worth knowing about:
    a stranger's rejected sentence sits in that ring until it ages out."""

    profile: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "voice" / "profile.npz"
    )
    """Where enrollment lives: the per-phrase embeddings and their mean.
    Delete the file to unenroll; re-running the script overwrites it."""

    model: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "models"
        / "speaker_campplus_voxceleb_16k.onnx"
    )
    """The ONNX speaker model, downloaded here on first use (~28 MB)."""


@dataclass(frozen=True, slots=True)
class STTConfig:
    engine: Literal["faster-whisper", "mlx-whisper"] = "mlx-whisper"
    """``mlx-whisper`` runs on the GPU via Metal; ``faster-whisper`` is the
    CPU fallback (CTranslate2 has no Metal backend). If mlx-whisper isn't
    installed, the factory falls back to faster-whisper on its own."""

    mlx_model: str = "mlx-community/whisper-small.en-mlx"
    """A Hugging Face repo of MLX-converted weights, downloaded on first use.
    ``whisper-small.en-mlx`` matches the faster-whisper ``small.en`` default.
    ``mlx-community/whisper-large-v3-turbo`` is the accuracy upgrade — try it
    if the GPU keeps decode latency acceptable (~1.6 GB download)."""

    model: str = "small.en"
    """faster-whisper only. ``base.en`` is faster and noticeably worse;
    ``medium.en`` is better and roughly triples latency. ``small.en`` is the
    sweet spot on Apple Silicon."""

    compute_type: str = "int8"
    """faster-whisper only. int8 is ~2x faster than float32 on CPU with
    negligible accuracy loss."""

    device: str = "cpu"
    """faster-whisper only, and "cpu" is the only real option there — GPU on
    Apple Silicon is what the mlx-whisper engine is for."""

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

    effect: Literal["none", "jarvis"] = "none"
    """Post-processing on the synthesized voice, engine-agnostic.

    ``jarvis`` is the installed-speaker treatment (``tts/effect.py``): low
    end rolled off, a presence lift, a breath of early reflections — the
    voice reads as coming from the room's architecture rather than from a
    file. Pure numpy, ~6 ms of added latency. ``none`` is the untouched
    voice."""


@dataclass(frozen=True, slots=True)
class FilesConfig:
    """Whether Ciel can touch the filesystem, and where."""

    enabled: bool = False
    """Off by default. Ciel acts on transcribed speech, reads web pages into
    its context, and file tools run without a confirmation step (shell
    commands, confirm-tier connector tools, message sends, and capability
    grants get one; file writes do not) — so file access is a real decision,
    not a default. Turn it on deliberately."""

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
        # Version control, read-only.
        "git status", "git log", "git diff", "git show", "git branch",
        "git blame",
        # The machine describing itself.
        "ls", "pwd", "date", "whoami", "uname", "id", "hostname", "uptime",
        "df", "du", "ps", "pmset -g",
        # Pure filters: they transform stdin to stdout and can't write a file.
        # These are what make quiet *pipelines* real — "ps | head" was landing
        # in confirm because head had no entry, and a gate that confirms
        # harmless status checks teaches reflexive yeses. Deliberately absent:
        # awk and sed (both can write files), tee (exists to write), xargs and
        # find (both can execute), and sort/uniq — despite reading like pure
        # filters, "sort -o FILE" and "uniq IN OUT" write a file, so they escape
        # to confirm where the write is heard.
        "head", "tail", "wc", "cut", "tr", "column", "nl",
        "grep", "echo", "printf",
        # File reads. A widening, chosen with eyes open: quiet shell reads
        # reach anything your user account can, beyond the file gate's
        # workspace. What still holds is the credential blocklist — a command
        # naming .ssh, .aws, .zshrc and kin is denied outright, quiet tier or
        # not — and redirection still confirms, so reading is all these get.
        "cat", "file", "stat", "which",
    )
    """Command prefixes that run without a spoken confirmation. Matched per
    pipeline segment against leading whole tokens — "git status" covers
    "git status -sb", and every segment of a pipe must match for the pipeline
    to stay quiet. Only for commands with no redirection, substitution, or
    backgrounding, which escalate to a confirmation regardless (merging or
    discarding a stream — 2>&1, >/dev/null — doesn't count; those can't
    touch a file)."""

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
class ReflectionConfig:
    """Closure — commit a conversation's limit points before it's discarded.

    Sessions are short-lived by design (see ``brain.resume_window_minutes``);
    what makes that safe is this: shortly after a conversation ends, Ciel
    gets one silent turn to review what was said and commit anything durable
    to memory and projects, while the session still remembers it. Always
    reflect, rarely save — the scheduler never guesses importance, the model
    does."""

    enabled: bool = True
    """On by default: with short sessions, reflection is where continuity
    comes from. Turn off only if the one extra model turn per conversation
    bothers you more than Ciel forgetting your afternoon."""

    idle_delay_s: float = 60.0
    """Silence after a conversation before reflection runs. Long enough that
    an ordinary conversational pause doesn't trigger it, short enough that
    the reflection lands well before the session can rotate away."""

    timeout_s: float = 120.0
    """Hard cap on one reflection turn. A hung reflection holds the turn
    lock, and a held turn lock is a mute assistant — the cap converts that
    failure into a logged shrug."""


@dataclass(frozen=True, slots=True)
class ProjectsConfig:
    """Atlas — durable named workspaces, the charts covering ongoing work.

    Projects hold working *state* ("attempt three, threshold zero point
    seven"), memories hold *facts* ("the user is training a wake word
    model"). A project survives any number of session rotations: the system
    prompt carries a one-line index, and the model opens a project before
    working on it."""

    enabled: bool = True

    dir: Path = field(default_factory=lambda: Path.home() / ".ciel" / "projects")
    """One Markdown file per project — inspectable, editable, greppable,
    same reasoning as the memory directory."""

    max_index_entries: int = 30
    """The index rides in every system prompt. Past this, old projects need
    closing out (status done), not more index budget."""

    max_state_chars: int = 4000
    """An opened project's state lands in context whole. Updates beyond this
    are refused with an instruction to condense — a refusal teaches the
    model to summarize; a silent truncation just loses whatever was last."""

    log_tail: int = 15
    """Log lines returned when a project is opened. The file keeps all of
    them; the tail is what fits a working context."""


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
    """Where the journal and its snapshots live. The workspace guard carves
    out read-only access to the ``snapshots`` folder under here, so restoring
    a file stays the model's ordinary Read + Write even when the workspace is
    narrow."""


@dataclass(frozen=True, slots=True)
class BrainConfig:
    model: str = "claude-opus-5"

    personality: Literal["jarvis", "ciel"] = "jarvis"
    """Who Ciel is being — an entry in prompt.PERSONALITIES. "jarvis" is an
    unflappable English butler with a dry wit; "ciel" is the original
    persona, warm and direct, kept in storage. Only the persona changes:
    speech rules and conversation mechanics are properties of the medium and
    stay identical underneath. A Literal so a typo warns at load instead of
    silently falling back."""

    allowed_tools: tuple[str, ...] = ("WebSearch", "WebFetch")
    """The Agent SDK ships the full Claude Code toolset — Bash, Write, Edit and
    all. A voice assistant that can silently run shell commands is not what we
    want, so this is an explicit allowlist, paired with permission_mode
    "dontAsk" to deny everything absent from it. File tools ride in via
    ``[files]`` and Bash via ``[shell]`` (with its voice gate) rather than
    this list, so enabling those features can't be done without also enabling
    their guards."""

    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    """Reasoning depth for ordinary turns. ``None`` uses the model default
    (high).

    Low by default because conversation rarely needs depth — chat turns emit
    only 6-20 output tokens since adaptive thinking correctly declines to
    deliberate over "what's my name" — and a voice assistant lives on
    snappiness. Depth is not lost, just moved: when a question genuinely
    needs it, the model escalates itself via the deep-thought agent below."""

    deep_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    """The escalation valve: effort for the deep-thought agent.

    Ciel runs quick by default, but hard questions deserve real deliberation
    — so a subagent with this effort level (same model, web tools) is
    registered, and the prompt tells her to hand genuinely hard problems to
    it. She decides per-question; nothing is escalated automatically. Raise
    to ``xhigh`` if deep answers still feel shallow — at the cost of longer
    silences. ``None`` removes the agent entirely."""

    deep_model: str = "inherit"
    """Which model the deep-thought agent runs on.

    ``"inherit"`` keeps it on the same model as ordinary turns, which is
    right when that model is already the strongest one available. When the
    driver is a fast, cheap model, this is the other half of the escalation
    valve: name a stronger model here ("opus", say) and hard questions get
    both more effort *and* more model, while conversation stays snappy.
    Aliases ("opus", "sonnet") and full model ids both work."""

    ack_phrases: tuple[str, ...] = ("Hmm.", "Let me see.", "Let me think about that.")
    """Spoken when a brain turn is taking a moment — the audible "I heard
    you". The brain's first sentence can be one to several seconds away, and
    that silence reads as not having been heard. One of these plays (chosen
    at random) overlapped with the brain thinking, so it fills the gap
    rather than adding to it. Only brain turns: local commands answer
    instantly and need no filler. Empty disables."""

    ack_delay_s: float = 0.6
    """How long the brain may stay silent before an ack phrase covers the
    gap. The filler exists for the *long* thinks; a reply that arrives
    inside this window plays immediately and the filler is skipped —
    otherwise "Let me think about that." chased instantly by the answer is
    theater, and worse, it delays the actual reply behind its own playback.
    0 restores speak-always."""

    speak_thinking: bool = True
    """Speak the model's reasoning aloud as it thinks, before the answer.

    On research turns the model can reason for many seconds, and that silence
    reads as a hang; verbalizing the reasoning fills it and shows where the
    answer is coming from. Turn off for terse turns that jump straight to the
    answer — with ``show_thinking`` on, the reasoning still streams to the
    console, just not through the speakers."""

    show_thinking: bool = True
    """Stream the model's reasoning to the console and transcript.

    Independent of the voice: this is the reading channel, ``speak_thinking``
    is the listening one. Whatever is spoken is always shown too — the
    console is the record of what was said — so the meaningful combinations
    are both (hear it and read it), show-only (silent deliberation you can
    watch), and neither (thinking text isn't even requested from the model).
    """

    thinking_chime: bool = True
    """Play a soft tone when the reasoning ends and the answer begins.

    With spoken reasoning the same voice says both, so the ear needs a
    boundary marker. With show-only reasoning the chime earns its keep
    differently: the deliberation was silent, and the tone is what says the
    coming sentence is the answer, not a long think still warming up. Idle
    when the turn had no reasoning phase at all."""

    max_turns: int | None = None

    resume_window_minutes: float = 10.0
    """How long a conversational thread survives silence. Within the window,
    a restart resumes the thread and a running process keeps it; past it, the
    session is rotated away and continuity comes from memory instead — the
    reflection pass has already committed anything durable by then. Short on
    purpose: context is for the *current* conversation, and a session that
    accumulates all day is why response time degrades by evening. Ten
    minutes still forgives "wait, one more thing"."""

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
class OuraConfig:
    """The Oura Ring — readiness, sleep, and activity (``oura.py``).

    Oura Cloud API v2 over OAuth2: an application of your own (client id
    and secret from developer.ouraring.com/applications), one browser
    approval via ``probe_oura.py --authorize``, and a token file Ciel keeps
    fresh from then on. Read-only by construction — the client only ever
    GETs the daily summaries and sleep sessions. Two consumers: the
    ``oura_summary`` brain tool ("how did I sleep"), and a Vigil watcher
    that flags a low readiness or sleep score in the morning and a still
    day's low activity score in the evening."""

    enabled: bool = False
    """Off by default: without an authorization there is nothing to fetch,
    and an always-failing tool wastes turns. Turn on after authorizing."""

    client_id: str = ""
    """Your Oura application's client id, from
    developer.ouraring.com/applications. Not a secret — it appears in the
    approval URL — but specific to you: each application connects to one
    developer's ring(s), and an unreviewed one is limited to ten accounts,
    which is nine more than this needs. Needed for the authorize step; the
    step copies it into ``token_file`` so the runtime needs only that."""

    client_secret: str = ""
    """The application secret. A credential: prefer ``CIEL_OURA_CLIENT_SECRET``
    in the environment of the authorize step over a value here — after
    that step it lives in ``token_file`` (owner-only, on the blocklist) and
    this field can stay empty. Revocable from the Oura application page."""

    token_file: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "oura.json"
    )
    """Where the authorization lives — client credentials, access and
    refresh tokens — written by the authorize step and rewritten by Ciel
    on every refresh, because Oura's refresh tokens are single-use and the
    file must follow them. Named in ``FORBIDDEN_NAMES``, so the model's
    file tools and the shell gate both refuse to read it."""

    redirect_port: int = 8791
    """The localhost port the one-time authorize step listens on; register
    ``http://localhost:<port>/callback`` as the application's redirect URI
    (it must match exactly). Bound only for the duration of that step —
    nothing in the running assistant ever listens."""

    token: str = ""
    """A legacy personal access token. Oura stopped issuing these in
    December 2025 and has said the issued ones will be shut off; kept so
    one already in hand keeps working until then. The OAuth tokens in
    ``token_file`` win whenever they exist."""

    poll_s: float = 1800.0
    """How often the Vigil watcher rescans, in seconds. Half an hour: the
    ring syncs opportunistically over the phone, so anything tighter just
    re-reads the same numbers."""

    low_readiness: int = 60
    """Readiness score at or below which the morning nudge fires — Oura's
    own "pay attention" band starts under 70; 60 keeps the nudge for
    genuinely rough mornings. 0 disables this check while keeping the
    tool. One nudge per morning covers readiness and sleep together."""

    low_sleep: int = 60
    """Sleep score at or below which the morning nudge fires (with hours
    asleep, when the session has synced). 0 disables this check."""

    low_activity: int = 40
    """Activity score at or below which an evening note is filed — a still
    day, mentioned at the next conversation rather than announced. 0
    disables this check."""

    activity_check_after: str = "18:00"
    """HH:MM local time from which the activity check runs. Earlier in the
    day a low score is just the day not having happened yet."""

    def authorized(self) -> bool:
        """Whether ``token_file`` holds a complete OAuth authorization — a
        refresh token plus the credentials to use it. A JSON peek rather
        than an import of ``oura.py``: config stays dependency-free."""
        try:
            data = json.loads(self.token_file.read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or not data.get("refresh_token"):
            return False
        return bool(
            (data.get("client_id") or self.client_id)
            and (data.get("client_secret") or self.client_secret)
        )

    @property
    def armed(self) -> bool:
        """Enabled *and* holding a way in — the one test the tool registry,
        the system prompt, and the watcher all make, so they agree."""
        return self.enabled and (self.authorized() or bool(self.token))


@dataclass(frozen=True, slots=True)
class SectionsConfig:
    """Watching the course section-signup site for an open spot
    (``sections.py``, watcher in ``proactive/sections.py``).

    Berkeley's *sections* app (sections.datastructur.es for CS 61B) fills
    sections first-come-first-served, and a dropped spot is gone in
    minutes. This watcher polls the site's ``refresh_state`` API and,
    when a watched section stops being full, files an importance-3 event
    — spoken immediately if you're around, texted if you're away and the
    messaging outlet is armed. Read-only: it never joins a section.

    The site only shows sections to a signed-in browser, and its Canvas
    OAuth can't be automated, so setup is: sign in once in a browser,
    copy the ``Cookie`` request header from any ``/api/`` call in
    DevTools' Network tab into ``cookie`` here, then run
    ``uv run scripts/probe_sections.py --live`` to list section ids and
    pick the ones for ``watch``. When the cookie eventually expires, the
    watcher's health streak files a note saying so."""

    enabled: bool = False
    """Off by default, like every watcher that earns unprompted speech."""

    url: str = "https://sections.datastructur.es"
    """The deployment to watch — other courses run the same app under
    other names."""

    cookie: str = ""
    """The browser's ``Cookie`` header for the site, as a *seed*. A session
    credential: anyone holding it is signed in as you there, so prefer
    ``CIEL_SECTIONS_COOKIE`` in the launch environment if you'd rather it
    not live in the config file. Whenever ``cookie_file`` holds a value it
    wins over this — the refresh job (below) writes there, and the site's
    session slides its own two-hour expiry forward on every poll, so a
    continuously-running watcher never needs this touched again."""

    cookie_file: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "sections-cookie"
    )
    """Where the live cookie lives once the refresh job is set up — the
    same owner-only-file pattern as the Oura tokens. The site's session
    dies only after two hours with no request, i.e. an overnight sleep;
    ``scripts/refresh_sections_cookie.py`` re-mints it through a browser
    profile that still holds a bCourses login and writes it here, and the
    watcher reads it fresh on every scan, so a refresh in that other
    process is picked up on the next poll with no reload."""

    watch: tuple[str, ...] = ()
    """Section ids worth interrupting for, as the probe's listing prints
    them (``watch = ["12", "31"]``). Empty watches nothing — this filter
    is the consent, and "any section anywhere" is a notification storm,
    not a wish."""

    poll_s: float = 30.0
    """How often the site is asked. Spots race by in minutes, so the
    default leans quick; one small JSON read per poll keeps that
    neighborly."""

    auto_refresh: bool = False
    """When on, a signed-out scan spawns ``refresh_cmd`` once (then waits
    out ``refresh_cooldown_s`` before trying again) instead of only filing
    the re-login note — self-healing the midday case where the machine was
    off past the two-hour window. The scheduled launchd job covers the
    ordinary overnight death; this covers the rest. Off until the refresh
    job is actually set up, or every signed-out scan would spawn a doomed
    browser."""

    refresh_cmd: tuple[str, ...] = ()
    """The command ``auto_refresh`` runs, argv-style
    (``["/Users/.../python", "scripts/refresh_sections_cookie.py"]``). Its
    job is to write a fresh cookie to ``cookie_file``; its output is
    logged, not parsed. Empty disables the spawn even when
    ``auto_refresh`` is on."""

    refresh_cooldown_s: float = 600.0
    """The shortest gap between two spawned refreshes. A dead cookie fails
    every 30-second poll; without a cooldown that would launch a browser
    every 30 seconds. Ten minutes is longer than a refresh takes and short
    enough that a genuine re-login lands promptly."""

    email_on_opening: int = 0
    """How many emails an opening sends — an alarm, not a notice: several
    distinct messages minutes apart make a phone buzz until it's looked
    at, where one message is a single buzz easily missed. Sent by the
    watcher itself the moment the event is queued, outside the speak/text
    policy and its quiet hours on purpose: a spot is gone in minutes and
    the user asked to be woken for it. 0 sends nothing. Needs the
    ``[mcp.gmail]`` connector authorized — its tokens are borrowed
    (``gmail.py``), the same way the Google Calendar watcher borrows."""

    email_interval_s: float = 60.0
    """Seconds between the alarm's messages. Each carries its own subject
    ("1 of 5") so Gmail doesn't fold them into one thread with one
    notification."""

    email_to: str = ""
    """Where the alarm goes. Empty means the Gmail account the connector
    is signed in as — "email me". Anything else is pinned here by you;
    deterministic code never chooses a recipient at send time."""

    email_from: str = ""
    """The From address — Ciel's own, e.g. ``ciel@example.com``, once that
    is a verified "send mail as" alias on the connector's account. Empty
    sends as the account itself. Unverified aliases are rewritten by
    Gmail to the primary address rather than rejected."""

    smtp_token: str = ""
    """Set this and the alarm goes out as Ciel through an SMTP relay
    instead of as you through Gmail (``mail.py``): for Cloudflare Email
    Service, an API token with the Email Sending permission — a
    credential, so ``CIEL_SECTIONS_SMTP_TOKEN`` in the environment is the
    tidier home. With it, ``email_from`` (Ciel's address on the onboarded
    domain) and ``email_to`` (a verified destination — sending there is
    free) are both required; nothing is looked up at send time."""

    smtp_host: str = "smtp.mx.cloudflare.net"
    """The relay. Cloudflare's speaks implicit TLS only."""

    smtp_port: int = 465
    """SMTPS. The relay has no STARTTLS on 587."""

    smtp_user: str = "api_token"
    """The login name — for Cloudflare, literally this string; the token
    is the password."""

    gmail_oauth_keys: Path = field(
        default_factory=lambda: Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"
    )
    """The Gmail connector's OAuth client — the same Desktop-app JSON."""

    gmail_token_file: Path = field(
        default_factory=lambda: Path.home() / ".gmail-mcp" / "credentials.json"
    )
    """Where the Gmail connector saved its refresh token. Read-only here."""

    def current_cookie(self) -> str:
        """The cookie to send: the live file the refresh job maintains
        wins, the config seed is the fallback. Read fresh on every call so
        another process's refresh needs no reload to take effect."""
        try:
            live = self.cookie_file.read_text().strip()
        except OSError:
            live = ""
        return live or self.cookie


@dataclass(frozen=True, slots=True)
class LocationConfig:
    """Where the user is (``location.py``) — from what this machine can see.

    Two sources, used in order: the phone's position from Find My's cache
    (needs Full Disk Access for the app running Ciel, and a macOS that
    still writes the cache as JSON — 14.4 and later don't), then the Wi-Fi
    network this Mac is on (``system_profiler``, no permission needed).
    Places below turn either into a name. Two consumers: the ``where_am_i``
    brain tool, and a Vigil watcher that notes moves between places.
    CoreLocation is not an option: macOS never shows a Python process the
    location permission dialog."""

    enabled: bool = False
    """Off by default: knowing where the laptop is, even read-only, is a
    standing fact about the person, opted into deliberately."""

    places: dict[str, Any] = field(default_factory=dict)
    """``[location.places]`` — name → a Wi-Fi network name, a list of them,
    a ``[lat, lon]`` pair, or a table with ``network``/``networks`` and
    ``lat``/``lon``. A fix resolves to the first place it matches, so the
    spoken answer is "at home", not a pair of numbers."""

    place_radius_m: float = 200.0
    """How close a coordinate fix must be to a place's coordinates to count
    as there. Two hundred metres covers a Find My reading's usual error
    and a large building; tighten it for places that are neighbours."""

    poll_s: float = 300.0
    """How often the watcher re-reads the sources. Five minutes: a move
    takes longer than that, and ``system_profiler`` costs a few seconds
    of CPU each time."""

    findmy_device: str = ""
    """A fragment of the device name in Find My ("iPhone") whose position
    counts as the user's. Empty leaves the Find My source off entirely and
    the answer is the Mac's network only."""

    findmy_cache: Path = field(
        default_factory=lambda: Path.home() / "Library" / "Caches"
        / "com.apple.findmy.fmipcore" / "Devices.data"
    )
    """Find My.app's device cache. Read-only here, and inside ``~/Library``
    — the subtree the model's file tools can never reach; this reader is
    deterministic code, not a tool."""

    findmy_max_age_s: float = 1800.0
    """A phone position older than this no longer says where the person is
    — the Mac's network is used instead."""

    wifi: bool = True
    """Whether the Wi-Fi network counts as a source. Off on a machine that
    never moves and whose network is not worth a place name."""


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    """The Discord lane — texting Ciel from wherever you are.

    Codename Parallel Transport (see ``remote/discord.py``). A DM from the
    pinned owner account becomes an ordinary turn — same brain, same
    session, same transcript — and the reply comes back as a text. A
    confirm-tier tool call mid-remote-turn texts its question over the
    same channel and waits for a yes or no, so the Proof Obligation
    follows the user out the door instead of being voiced into an empty
    room.

    Setup lives in the README: a bot application, its token, a mutual
    server (Discord only delivers DMs between accounts that share one),
    and your user id."""

    enabled: bool = False
    """Off by default, like every switch that opens a path into the brain
    from outside the room. Turning it on without ``token`` and
    ``owner_id`` warns at startup and arms nothing."""

    token: str = ""
    """The bot token from the Discord developer portal. A credential:
    prefer ``CIEL_DISCORD_TOKEN`` in the launch environment if you'd
    rather it not live in the config file. Anyone holding this token can
    read what Ciel texts you and impersonate the bot — treat it like a
    password, and regenerate it in the portal if it leaks."""

    owner_id: int = 0
    """Your Discord user id — the identity gate. Only DMs from this
    account are read at all; everything else is dropped before it reaches
    anything that could act on it. Pinned in config for the
    ``owner_handle`` reason: the model never chooses who may speak here
    or where replies go. (Discord Settings → Advanced → Developer Mode,
    then right-click your own name → Copy User ID.)"""

    proactive: bool = True
    """Whether Vigil's away texts may ride this link when iMessage isn't
    configured. The link being armed at all is already the deliberate
    opt-in — this exists to keep the lane strictly two-way-conversational
    for anyone who wants that. iMessage, when fully configured, keeps
    priority; this is the fallback outlet, not a second one."""

    confirm_timeout_s: float = 120.0
    """How long a texted confirmation question waits for a yes or no
    before counting as no. Far longer than the spoken gate's eight
    seconds on purpose: a phone in a pocket answers on notification time,
    not conversation time — but not unbounded, because the turn holds
    the brain's lock while it waits. Also the deadline for questions
    asked on the web GUI — every text-lane confirmation shares this
    clock, Discord enabled or not."""

    max_inbound_chars: int = 4000
    """Longest inbound message accepted, truncated beyond. Twice
    Discord's own per-message cap, so nothing a normal client sends ever
    hits it — this bounds pastes-of-pastes, not conversation."""

    mentions: bool = True
    """Answer @mentions in server channels the bot has been invited to —
    still only from ``owner_id``; everyone else's mentions are dropped at
    the same gate as their DMs. Replies land in the channel, publicly,
    and the turn is told so. Off restricts the lane to DMs. (No
    privileged intent needed: Discord exempts messages that mention the
    bot from the message-content restriction.)"""

    token_file: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "discord.token"
    )
    """Where the bot token lives when ``token`` above is empty — a file
    named in ``FORBIDDEN_NAMES``, so the model's file tools and the shell
    gate both refuse to read it. Prefer this over ``token``: config.toml
    is model-readable (the workspace covers home), and a credential in a
    readable file is a credential in every future context window."""

    state_file: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "discord.json"
    )
    """The lane's tiny persistent state — the last DM actually seen —
    mirrored on every accepted message for the timers.json reason: the
    autoreloader re-execs constantly (a remote capability grant does it
    on purpose), and a text sent during the seconds of restart must be
    picked up by the catch-up sweep, not silently lost."""

    def resolved_token(self) -> str:
        """The bot token: the inline value if set, else ``token_file``'s
        contents. Empty when neither exists — the lane's unarmed state."""
        if self.token:
            return self.token
        try:
            return self.token_file.read_text().strip()
        except OSError:
            return ""


@dataclass(frozen=True, slots=True)
class WebConfig:
    """The local GUI — a chat window onto the running assistant.

    Codename Chart (see ``remote/web.py``). A small local web server: the
    page it serves is a live view of the conversation — every lane's
    turns, the state pill, a mute switch — and a place to type when the
    room must stay quiet (a class, a library). The WebSocket protocol it
    speaks is documented in the module, deliberately client-agnostic: a
    native app later (SwiftUI, or anything else) connects to the same
    socket and the server never knows the difference."""

    enabled: bool = False
    """Off by default, like every switch that opens a path into the brain
    from outside the terminal — even one that only listens on loopback."""

    host: str = "127.0.0.1"
    """Loopback only by default, and think hard before widening it: this
    socket feeds text straight into a brain with tools, and unlike the
    Discord lane there is no account id here to gate on — being able to
    reach the port *is* the identity check. Browser pages from other
    origins are refused by the Origin check either way."""

    port: int = 8765
    """Where the GUI lives: http://127.0.0.1:8765 by default."""

    max_inbound_chars: int = 4000
    """Longest inbound message accepted, truncated beyond — the Discord
    lane's bound, for the Discord lane's reason: this limits pastes, not
    conversation."""

    history_lines: int = 200
    """How many recent transcript rows a connecting client is caught up
    with. Session-scoped and in-memory: the GUI opening mid-conversation
    should show the conversation, not a blank page — but it is a window,
    not the archive; the CSVs in ``[transcripts]`` remain the record."""


@dataclass(frozen=True, slots=True)
class HubConfig:
    """The hub's wire server — the Chart socket, reachable beyond loopback.

    Codename Meridian (see ``wire.py``). Phase one of the hub-and-spoke
    move: the same process, the same ``/ws``, but bindable to the
    tailnet and gated by a token, so a phone on the Tailscale network
    can open the Chart while the laptop's lid is open. Later phases put
    the Mac spoke and the brain on opposite ends of this same socket."""

    bind: str = ""
    """The address the wire server listens on; empty means ``[web].host``
    (loopback). Set it to the machine's Tailscale address (``100.x.y.z``)
    to reach it from the tailnet and nowhere else — never a public
    interface. ``0.0.0.0`` works but then list the names clients will
    use in ``origins``, since the Origin gate can't infer them."""

    token: str = ""
    """The shared secret every non-loopback client must present in its
    hello. A credential: prefer ``CIEL_HUB_TOKEN`` in the environment or
    the file — this field exists so a one-off run can pin one. When both
    are empty and the bind is not loopback, Ciel mints one into
    ``token_file`` at start and logs where it went."""

    token_file: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "hub.token"
    )
    """Where the minted token lives — owner-only, and named in
    ``FORBIDDEN_NAMES`` so the model's file tools and the shell gate both
    refuse to read it. Paste its contents into the Chart once; the page
    remembers it."""

    require_token: bool = False
    """Ask even loopback clients for the token. Off by default: reaching
    a loopback port already means being at the machine, and the typed
    lane trusts that. On is for the probe, and for a shared machine."""

    origins: tuple[str, ...] = ()
    """Extra Origin hosts the socket accepts, beyond loopback and the
    bind address itself — the MagicDNS name (``ciel-hub.tail1234.ts.net``)
    when the page is opened by name rather than by address. Exact host
    match; the port must still be ``[web].port``."""

    hello_timeout_s: float = 5.0
    """How long a fresh socket may stay silent before the hub closes it:
    the client speaks first now (its hello carries the token and the
    resume claim), so a socket that never says hello is a port scan or
    a page too old to know."""

    resume_frames: int = 1024
    """How many recent broadcast frames the hub keeps for resume. A
    client that missed more than this gets a fresh hello with the
    ``[web].history_lines`` window instead — the same catch-up the
    Chart always had."""

    resume_grace_s: float = 600.0
    """How old a live confirm prompt may be and still be replayed to a
    resuming client — the Discord catch-up rule: a ten-minute-old
    question is still waited on; anything older has already been timed
    out by the broker, and a replayed banner would invite an answer to
    nothing."""

    speak_timeout_s: float = 120.0
    """How long the hub waits for the spoke to report one sentence (or
    one delivery) played before treating the spoke as gone and
    abandoning the turn. Generous: a long sentence through Piper plus a
    slow machine is tens of seconds; only a dead socket takes minutes."""

    confirm_timeout_s: float = 45.0
    """The hub-side deadline for one spoken answer from the spoke — the
    spoke speaks the question and listens for its own ``[shell]``
    window, then reports; this only catches a spoke that never reports
    at all. Text lanes keep ``[discord].confirm_timeout_s``."""

    def current_token(self) -> str:
        """The token to check against: the field, else the file, else
        empty (which, off loopback, means mint one)."""
        if self.token:
            return self.token
        try:
            return self.token_file.read_text().strip()
        except OSError:
            return ""


@dataclass(frozen=True, slots=True)
class SpokeConfig:
    """The spoke — the audio front end as a client of the hub.

    ``ciel spoke`` owns the microphone, the wake word, the endpointer,
    the speaker gate, STT, TTS, the player, the HUD, and the mute
    sentinel; everything it hears goes up the wire as text, everything
    it says comes down as text. The brain, memory, Vigil, Discord, and
    the Chart live in ``ciel hub``. Both on this machine for now; the
    hub moves to a server later and this section is what repoints."""

    hub: str = "ws://127.0.0.1:8765/ws"
    """The hub's socket. Loopback while both run here; the tailnet
    address (``ws://100.x.y.z:8765/ws``) once the hub is elsewhere."""

    token: str = ""
    """The hub token, when the hub is off loopback. Prefer
    ``CIEL_SPOKE_TOKEN`` in the environment or ``token_file``."""

    token_file: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "hub.token"
    )
    """Where to read the token — the hub's own minted file when both run
    on this machine, so nothing needs pasting."""

    client_id: str = "mac"
    """How this spoke names itself in the hub's log."""

    reconnect_max_s: float = 8.0
    """The ceiling of the reconnect backoff (starts at half a second)."""

    hello_timeout_s: float = 5.0
    """How long to wait for the hub's hello after connecting."""

    def current_token(self) -> str:
        if self.token:
            return self.token
        try:
            return self.token_file.read_text().strip()
        except OSError:
            return ""


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

    unattended: tuple[str, ...] = ()
    """Tools an *unattended* turn (reflection, a Vigil proactive turn) may
    call — your declaration that they are read-only enough for a turn nobody
    supervises, e.g. a calendar's ``list-events`` so a nudge can verify the
    meeting still exists before announcing it. Everything not listed here
    denies under the Witness rule while unattended, including the whole
    ``confirm`` tier (there is no one to say yes). These names must also be
    callable at all — on ``tools``, or on a server whose ``tools`` is None;
    this list never widens what the model may reach, only when."""


@dataclass(frozen=True, slots=True)
class GrantsConfig:
    """Remote capability granting — changing what Ciel may do, by asking.

    With this on, Ciel gets three tools over its own configuration:
    ``list_capabilities`` (what exists, what's on), ``grant_capability``
    (enable something, or set the morning brief), and
    ``revoke_capability`` (disable something). The asymmetry is the
    design: every grant passes the enforced confirmation gate — spoken at
    home, texted over the Discord lane — before a byte of config changes,
    while a revoke runs immediately, because de-escalation should never
    have friction. The catalog of grantable keys is fixed in code
    (``brain/tools/grants.py``); the security-critical ones — the Discord
    pinning, the voice gate, the tool tiers — are simply not in it, so no
    phrasing reaches them. Changes are edited into config.toml surgically
    (comments preserved, parse-verified, rolled back on failure),
    journaled like every confirmed action, and take effect on the reload
    the grant itself triggers."""

    enabled: bool = False
    """Off by default: a channel that widens Ciel's own permissions is
    itself a permission, and the biggest one here."""


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
class CommandsConfig:
    enabled: bool = True
    """Local commands: utterances the pipeline handles itself, brain never
    involved — "ten minute timer", "cancel the timer", "reload". Zero model
    latency and zero cost, at the price of fixed phrasing; anything the
    grammar in commands.py doesn't recognize with certainty still goes to
    the brain as before."""


@dataclass(frozen=True, slots=True)
class TimersConfig:
    enabled: bool = True

    file: Path = field(default_factory=lambda: Path.home() / ".ciel" / "timers.json")
    """Armed timers, mirrored here on every change so a restart (autoreload
    does this constantly) doesn't silently swallow a running timer."""

    missed_grace_s: float = 3600.0
    """How late a timer that came due while Ciel was off still gets announced
    at startup. Within the window: spoken, late but honest. Past it: dropped
    with a log line — a "your rice is done" from yesterday helps no one."""


@dataclass(frozen=True, slots=True)
class ProactiveConfig:
    """Vigil — the watching layer: events, presence, and the earned right to
    interrupt.

    Watchers (the calendar first) push events into one persistent queue; a
    deterministic policy decides whether a moment justifies speaking
    unprompted; an *unattended* brain turn — bound by the Witness rule, so it
    can look and remember but change nothing outside Ciel's own notebook —
    composes the words or declines. Everything that can't be spoken now is
    held for the start of the next conversation. The policy picks the
    ceiling and the model may only quiet it further, never escalate: event
    text (calendar titles!) is written by other people, and routing
    authority stays out of its reach.

    The emergency brake: ``touch <state_dir>/hold`` pauses all proactive
    delivery — events queue, nothing is dropped — until the file is removed.
    A sentinel rather than a config edit so it can be tripped from outside
    the process and survives restarts."""

    enabled: bool = False
    """Off by default. A standing right to speak unprompted is opted into
    deliberately, never inherited from an upgrade."""

    state_file: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "proactive.json"
    )
    """The queue, held notes, dedupe record, and daily budget, mirrored on
    every change — same reasoning as timers.json: the autoreloader re-execs
    the process constantly, and a restart must not swallow a nudge or reset
    the day's budget."""

    quiet_hours_start: str = "22:00"
    """Start of the window in which nothing is voiced, HH:MM local time.
    Events arriving inside it hold for the next conversation instead. Set
    equal to ``quiet_hours_end`` to have no quiet window at all."""

    quiet_hours_end: str = "08:00"
    """End of the quiet window. The window may span midnight — the default
    pair does — and the policy handles the wrap."""

    max_spoken_per_day: int = 6
    """The hard budget of unprompted speech per local day. Every
    interruption spends trust; when the budget is gone, Ciel holds the rest
    for the next conversation no matter how the day goes. Deliveries the
    user initiated (held notes, answers) never count against it."""

    min_importance_to_speak: int = 2
    """Events below this importance (watchers rate 1–3) never interrupt —
    importance 1 is news, and news waits until you're already talking."""

    presence_mode: Literal["any", "hid", "conversation"] = "any"
    """Which signals count as "the user is around": ``hid`` trusts the
    screen-lock state and keyboard/mouse recency (a machine somebody sits
    at), ``conversation`` trusts only having recently talked to Ciel (a
    box with no keyboard, where the mic is the interface), ``any`` accepts
    either. Default ``any`` because the deployment machine is undecided —
    tighten it once the box is chosen."""

    presence_idle_s: float = 300.0
    """Keyboard/mouse recency that still counts as "at the desk". Five
    minutes: long enough to survive reading something, short enough that a
    machine left for lunch stops counting as attended."""

    conversation_presence_s: float = 180.0
    """Having talked to Ciel this recently is presence too — the signal that
    works when there is no keyboard to watch."""

    policy_poll_s: float = 5.0
    """How often presence and policy are re-evaluated while events are
    pending. The frame loop itself only ever checks a boolean; the Quartz
    reads and policy walk happen at most this often, and only when
    something is actually waiting."""

    turn_timeout_s: float = 60.0
    """Hard cap on one unattended turn — reflection's reasoning exactly: a
    hung turn holds the brain's lock, and a held lock is a mute assistant.
    Shorter than reflection's cap because a nudge turn composes a sentence,
    not a review."""

    held_max_age_h: float = 18.0
    """Held notes older than this die unspoken — the missed-timer grace
    reasoning: "your two o'clock was about to start" delivered tomorrow
    helps no one. 18 hours lets an overnight hold survive to the morning
    conversation and nothing older."""

    calendar_enabled: bool = True
    """The first watcher. On when Vigil itself is enabled: proximity nudges
    are the whole reason to turn this layer on today. Off leaves the rest of
    the plumbing running for other watchers."""

    calendar_lead_minutes: float = 10.0
    """The nudge horizon — "your two o'clock is in ten minutes". A nudge
    expires at the event's start: better silent than late."""

    calendar_poll_s: float = 180.0
    """How often the calendar is rescanned. Scan timing no longer sets nudge
    timing — nudges are pushed as soon as a scan sees the event and held to
    their deliver_after moment, so "ten minutes before" lands on the policy
    poll (~5 s), not somewhere inside a scan interval. The cadence only has
    to catch events created or moved on short notice."""

    calendars: tuple[str, ...] = ()
    """Calendar names to watch, matched against calendar titles. Empty means
    all of them (the "google" source reads just the primary calendar when
    empty) — right until the first shared calendar full of other people's
    reminders, which is what this filter is for."""

    calendar_source: Literal["eventkit", "google"] = "eventkit"
    """Where nudges read the calendar. "eventkit" reads whatever macOS
    Calendar syncs — right when calendars live there. "google" goes to
    Google Calendar directly, piggybacking the [mcp.gcal] connector's OAuth
    (same client keys, same saved refresh token; nothing new to authorize) —
    right on a machine where Google is the calendar and no macOS account
    mirrors it, where EventKit would watch an empty store forever. A
    Literal so a typo warns at load."""

    google_oauth_keys: Path = field(
        default_factory=lambda: Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"
    )
    """OAuth client for the "google" source — the same Desktop-app JSON the
    Google connectors use (see the connector notes in config.toml)."""

    google_token_file: Path = field(
        default_factory=lambda: Path.home()
        / ".config" / "google-calendar-mcp" / "tokens.json"
    )
    """Where @cocal/google-calendar-mcp saves the tokens its OAuth flow
    produced. Read-only here: the watcher borrows the refresh token and
    keeps access tokens in memory, never writing back — two writers of one
    token file is how refresh races start. If the connector re-authorizes,
    the next refresh picks the new token up."""

    owner_handle: str = ""
    """Where "text me if I'm away" goes — one phone number or iMessage email,
    pinned here in config. Empty disables the outlet entirely. This is the
    third switch in the chain (after ``messages.enabled`` and
    ``messages.allow_send``), and the pin is the point: an unattended turn
    never chooses a recipient — the model composes words, deterministic code
    decides that a message happens and to whom."""

    min_importance_to_message: int = 3
    """Only this important or above may reach your phone when you're away.
    Texting is a bigger interruption than a sentence spoken into a room you
    might not even be in — the default reserves it for urgent (3)."""

    max_messaged_per_day: int = 3
    """The texting budget, separate from the spoken one. A phone that buzzes
    all day teaches you to ignore the buzzes; three is a lot of genuinely
    urgent things for one day."""

    brief_time: str = ""
    """HH:MM local time to arm the morning brief — one proactive turn that
    rounds up today's calendar and anything held overnight. Empty leaves it
    off. The brief goes stale like breakfast: if the machine was asleep
    through the morning, a brief more than a few hours late is skipped, not
    delivered at dinner."""

    watches_file: Path = field(
        default_factory=lambda: Path.home() / ".ciel" / "watches.json"
    )
    """Registered background watches (watch_for_completion), mirrored on
    every change — an export watched at noon must survive the autoreloader
    and a lid-close, same reasoning as timers.json."""


@dataclass(frozen=True, slots=True)
class ScreenConfig:
    enabled: bool = True
    """Whether Ciel may look at the screen. The tool is only offered to the
    model when this is on, so disabling it removes the capability entirely
    rather than leaving a tool that always refuses.

    Needs macOS Screen Recording permission (System Settings → Privacy &
    Security) granted to whatever app runs Ciel — without it, captures come
    back as wallpaper with every window silently missing, which is why the
    tool preflights the permission instead of trusting the output."""

    max_edge: int = 1568
    """Screenshots are scaled so their long edge is at most this many pixels
    before being sent to the model. 1568 is the most Claude's vision actually
    ingests — anything larger is downscaled server-side anyway, so sending
    more just costs upload time. On a 2x Retina display this lands close to
    1x logical resolution, which keeps on-screen text readable."""


@dataclass(frozen=True, slots=True)
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    projects: ProjectsConfig = field(default_factory=ProjectsConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    web: WebConfig = field(default_factory=WebConfig)
    hub: HubConfig = field(default_factory=HubConfig)
    spoke: SpokeConfig = field(default_factory=SpokeConfig)
    oura: OuraConfig = field(default_factory=OuraConfig)
    sections: SectionsConfig = field(default_factory=SectionsConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    grants: GrantsConfig = field(default_factory=GrantsConfig)
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    timers: TimersConfig = field(default_factory=TimersConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
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
    "voice": VoiceConfig,
    "stt": STTConfig,
    "tts": TTSConfig,
    "brain": BrainConfig,
    "memory": MemoryConfig,
    "reflection": ReflectionConfig,
    "projects": ProjectsConfig,
    "files": FilesConfig,
    "shell": ShellConfig,
    "journal": JournalConfig,
    "messages": MessagesConfig,
    "discord": DiscordConfig,
    "web": WebConfig,
    "hub": HubConfig,
    "spoke": SpokeConfig,
    "oura": OuraConfig,
    "sections": SectionsConfig,
    "location": LocationConfig,
    "grants": GrantsConfig,
    "commands": CommandsConfig,
    "screen": ScreenConfig,
    "timers": TimersConfig,
    "proactive": ProactiveConfig,
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

    # A Literal field ("say"/"piper", wake modes, indicator kinds) has a fixed
    # set of valid values. Coercion can't fix a wrong one, but it can say so:
    # otherwise a typo'd engine passes straight through and silently falls back
    # at the use site, which reads as "my config did nothing".
    literal_allowed = tuple(
        arg for m in members if get_origin(m) is Literal for arg in get_args(m)
    )
    if literal_allowed and value not in literal_allowed:
        log.warning(
            "config value %r is not one of %s — it may not take effect",
            value, literal_allowed,
        )

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
    clean: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in valid:
            # A misspelled key was silently dropped before, so "engien = piper"
            # looked accepted but changed nothing. Name it — a config that
            # ignores half of what you wrote should at least tell you which half.
            log.warning(
                "unknown config key %r in [%s] — ignored",
                key, type(section).__name__,
            )
            continue
        clean[key] = _coerce(value, types.get(key))
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
    # The two top-level fields aren't in _SECTIONS, so they need naming here to
    # honor the documented CIEL_* override contract — CIEL_STATE_DIR was
    # missing entirely, so the state directory could not be set from the
    # environment like everything else.
    if "CIEL_LOG_LEVEL" in os.environ:
        env_updates["log_level"] = os.environ["CIEL_LOG_LEVEL"]
    if "CIEL_STATE_DIR" in os.environ:
        env_updates["state_dir"] = _coerce(os.environ["CIEL_STATE_DIR"], Path)
    if env_updates:
        cfg = replace(cfg, **env_updates)

    return cfg
