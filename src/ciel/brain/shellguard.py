"""The shell gate: command classification plus a voice-confirmed hook.

Codename: **Trichotomy** — every command falls into exactly one of three
classes: forbidden, allowed silently, or allowed once confirmed.

The workspace guard next door confines paths; it cannot confine a shell,
because a shell reaches any path by construction (``sh -c 'cat > ~/.zshrc'``).
So Bash gets its own gate with a different shape: instead of asking "where does
this path land", it asks "what is this command about to do" — and, for anything
it cannot prove harmless, it asks *the user*, out loud, and waits for the
answer.

Three tiers, deny wins:

* **deny** — never runs, even if the user says yes. Privilege escalation,
  credential and startup-file paths, pipe-a-download-into-a-shell, persistence
  mechanisms. A "yes" to a misheard question must not be able to do these.
* **quiet** — a conservative allowlist of read-only prefixes (``git status``,
  ``ls``) runs silently. Confirming every harmless status check aloud would
  teach the user to say yes reflexively, which defeats the gate.
* **confirm** — everything else. The hook blocks while the pipeline speaks the
  command aloud and listens for yes or no; "no", silence, and anything
  unparseable all deny.

The classifier is best-effort string analysis, not a shell parser, and it is
honest about that: anything with structure it can't see through — quoting
tricks, substitutions, redirections — lands in *confirm*, where a human hears
it before it runs. Deny matching errs the other way: over-matching a rare
legitimate command into a refusal is an acceptable cost, under-matching is not.

Like the workspace guard, this is a PreToolUse hook rather than a
``can_use_tool`` callback — an ``allowed_tools`` entry auto-approves before
that callback runs, so only the hook is consulted for the tool it constrains.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from typing import Any, Awaitable, Callable, Literal

from claude_agent_sdk import HookContext, HookMatcher

from ciel.brain.permissions import FORBIDDEN_NAMES
from ciel.config import ShellConfig

log = logging.getLogger(__name__)

Tier = Literal["deny", "quiet", "confirm"]

GLOB_CONFIRM_REASON = "a pattern in it could match credential or startup files"
"""The one confirm reason that changes the spoken question. Shared as a
constant so the guard can recognize it without string-matching prose: a
confirmation whose whole point is "this glob might touch secrets" must say
so out loud — especially since long commands are truncated in the prompt
and the suspicious pattern may be exactly the part that got cut."""

# Leading tokens refused outright, whatever follows. Each is a capability no
# voice-driven assistant needs: root, the keychain, login persistence, system
# state, or scripting other applications (osascript reaches Mail, Messages,
# and System Events — every boundary the tool configs draw, erased).
_DENY_TOKENS = frozenset({
    "sudo", "doas", "su",
    "launchctl", "security", "osascript",
    "systemsetup", "shutdown", "reboot", "halt",
    "nvram", "csrutil", "spctl",
})

# Interpreters that turn piped-in text into execution. curl | sh is the classic
# install-anything pattern, and exactly the shape a prompt-injected page asks
# for.
_PIPE_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "python", "python3", "perl", "ruby", "node"})
_DOWNLOADERS = frozenset({"curl", "wget"})

# Transparent prefixes: they run the command that follows, so the deny scan has
# to see *through* them to the real command (``env sudo``, ``nohup rm -rf ~``).
# Deliberately excludes xargs/find/sh -c, which take their own operands and can
# execute an allowlisted-looking inner command — peeling those would *widen* the
# quiet tier, so they stay at confirm (they are absent from the allowlist).
_WRAPPERS = frozenset({
    "env", "command", "nohup", "exec", "builtin", "setsid", "stdbuf",
    "nice", "time", "ionice",
})
# Wrapper options that consume the following token as their value, so it is not
# mistaken for the wrapped command (``nice -n 10 sudo`` → sudo, not 10).
_WRAPPER_VALUE_OPTS = frozenset({
    "-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-n", "--adjustment",
})

# Glob metacharacters. A component carrying one may expand to a forbidden path,
# so it is matched against the credential names with the component as the
# pattern (``.??h`` can match ``.ssh``).
_GLOB_CHARS = re.compile(r"[*?\[]")

# Structure the classifier cannot see through. Redirection can write anywhere,
# substitution hides a second command inside the first, & backgrounds it out
# from under the confirmation. None of these are denied — they escalate a
# would-be quiet command to confirm, where the user hears it.
_SUBSTITUTION = re.compile(r"\$\(|`")
_REDIRECTION = re.compile(r"[<>]")
_BACKGROUND = re.compile(r"(?<![&>])&(?![&])")  # bare &, not && or >&

# Redirections that cannot write anywhere: merging one stream into another
# (2>&1) and discarding a stream into /dev/null. Erased before the structure
# check — the model reflexively appends 2>&1 to read-only commands, and
# confirming "git log 2>&1 | head" aloud teaches the user to say yes without
# listening, which is the exact failure the quiet tier exists to prevent.
_HARMLESS_REDIRECT = re.compile(r"\d*>&\d+|\d*>>?\s*/dev/null|&>\s*/dev/null")

# Command separators the deny scan must see across: ``&&``/``||``/``;``/newline,
# and a *bare* ``&`` (``A & B`` runs B after backgrounding A — B would otherwise
# hide inside A's segment, invisible to the deny checks). The bare-``&``
# alternative excludes ``&&`` and the fd forms ``2>&1``/``&>`` so those stay
# whole for the structure check rather than splitting a redirection in two.
_PIPELINE_SPLIT = re.compile(r"&&|\|\||[;\n]|(?<![&>])&(?![&>])")


def _unquote(token: str) -> str:
    """Strip the shell quoting the classifier would otherwise be fooled by.

    Stripping only the *outer* quotes left ``.a"w"s`` reading as a literal
    ``.a"w"s`` — which is not ``.aws`` — so a quoted spelling of a credential
    path slipped past the names while the shell went on to expand it anyway.
    Interior quotes and backslashes are removed so the comparison happens on the
    string the shell will actually use.
    """
    return token.replace('"', "").replace("'", "").replace("\\", "")


def _normalize(token: str) -> str:
    """Unquote a command token and reduce it to its basename.

    So ``/usr/bin/sudo``, ``\\sudo`` and ``"sudo"`` all reduce to ``sudo`` for
    the deny comparison, which matches on the bare name.
    """
    return os.path.basename(_unquote(token))


def _forbidden_component(command: str) -> tuple[str, bool] | None:
    """The first credential/startup-file name the command mentions, if any,
    as ``(component, exact)``.

    Matched against path components of every whitespace token, so
    ``cat ~/.ssh/id_rsa`` and ``echo x >> ~/.zshrc`` both trip on the name
    alone — no path resolution, which a shell could defeat anyway. Quoting is
    removed first (``cat ~/.a"w"s/credentials`` still trips on ``.aws``).

    A glob component that could expand to a forbidden name trips too. Which
    *tier* it earns depends on how much the pattern says: a broad listing
    glob (``.*``, ``.??*`` — at most one literal character once wildcards
    are removed) matches every dotfile, ``.ssh`` incidentally among them, so
    it is reported inexact and the caller downgrades it to confirm — the
    user hears the command, and an ordinary ``ls -d .*`` stops landing in
    the tier with no escape hatch. A *targeted* glob (``.ss?``, ``id_*`` —
    real literal characters spelling out most of a forbidden name) is the
    obfuscated form of that name, exactly what injected text would choose,
    and stays an exact-grade deny. An exact hit anywhere outranks a glob
    hit, so ``ls .* ~/.ssh/id_rsa`` still denies.
    """
    glob_hit: str | None = None
    for token in command.split():
        cleaned = _unquote(token)
        for part in cleaned.split("/"):
            if part in FORBIDDEN_NAMES:
                return part, True
            if (
                _GLOB_CHARS.search(part)
                and part.strip("*?[]")  # skip a bare `*`, which matches everything
                and any(fnmatch.fnmatch(name, part) for name in FORBIDDEN_NAMES)
            ):
                literals = re.sub(r"[*?\[\]]", "", part)
                if len(literals) > 1:
                    return part, True  # targeted: an obfuscated spelling
                if glob_hit is None:
                    glob_hit = part
    return (glob_hit, False) if glob_hit is not None else None


def _effective_tokens(tokens: list[str]) -> list[str]:
    """Peel transparent wrappers and normalize the command to its basename.

    ``env sudo rm`` → ``[sudo, rm]``, ``/bin/rm -rf ~`` → ``[rm, -rf, ~]`` — so
    the deny scan sees the command that will actually run rather than the
    wrapper or the path it was spelled with. The allowlist still matches on the
    original tokens, so a wrapper never *widens* the quiet tier.
    """
    toks = list(tokens)
    while toks:
        base = _normalize(toks[0])
        if base in _WRAPPERS and len(toks) > 1:
            rest = toks[1:]
            lookup = False  # `command -v/-V`: prints a path, executes nothing
            while rest:
                arg = rest[0]
                if arg.startswith("-"):
                    # Flag-character match, not whole-token: bash accepts the
                    # clustered `command -pv name` too.
                    lookup = lookup or bool(
                        base == "command" and {"v", "V"} & set(arg.lstrip("-"))
                    )
                    rest = rest[1:]
                    if arg in _WRAPPER_VALUE_OPTS and rest:
                        rest = rest[1:]  # its value is not the wrapped command
                    continue
                if "=" in arg:  # env VAR=value assignment
                    rest = rest[1:]
                    continue
                break
            if lookup:
                # Peeling made a pure lookup deny on the name it was looking
                # up — stop peeling and let it classify as the lookup it is
                # (not allowlisted, so confirm, never quiet).
                return [base, *toks[1:]]
            toks = rest
            continue
        return [base, *toks[1:]]
    return toks


def _pipelines(command: str) -> list[list[list[str]]]:
    """Split into pipelines of tokenized segments, plus substitution bodies.

    ``a && b | c`` becomes ``[[a-tokens], [b-tokens, c-tokens]]`` — the pipe
    relationship is kept within a pipeline so curl-into-sh is detectable.
    Substitution bodies are appended as their own pipelines: what runs inside
    ``$(...)`` runs, and must be judged like anything else.
    """
    text = command
    extras: list[str] = []
    for pattern in (re.compile(r"\$\(([^()]*)\)"), re.compile(r"`([^`]*)`")):
        for match in pattern.finditer(text):
            extras.append(match.group(1))

    pipelines: list[list[list[str]]] = []
    for chunk in (*_PIPELINE_SPLIT.split(text), *extras):
        segments = [seg.split() for seg in chunk.split("|")]
        segments = [tokens for tokens in segments if tokens]
        if segments:
            pipelines.append(segments)
    return pipelines


def _dangerous_rm(tokens: list[str]) -> bool:
    """Whether this is ``rm`` aimed recursively and forcibly at home or root.

    Plain ``rm`` (even ``rm -rf ./build``) stays confirm-tier — the user hears
    it. What must never ride on a "yes" to a half-heard question is the
    unrecoverable case: recursive, forced, and rooted at ``~`` or ``/``.

    ``tokens`` is expected to be wrapper-peeled and basename-normalized, so
    ``/bin/rm`` and ``env rm`` reach here as ``rm``.
    """
    if not tokens or tokens[0] != "rm":
        return False
    flag_chars = set("".join(t.lstrip("-") for t in tokens[1:] if t.startswith("-")))
    if not ({"r", "R"} & flag_chars and "f" in flag_chars):
        return False
    for target in (t for t in tokens[1:] if not t.startswith("-")):
        if target.rstrip("/") in {"", "~", "$HOME", "${HOME}"}:
            return True
    return False


def classify(command: str, config: ShellConfig) -> tuple[Tier, str]:
    """Sort a command line into deny, quiet, or confirm, with a spoken reason."""
    text = command.strip()
    if not text:
        return "deny", "it is empty"

    forbidden = _forbidden_component(text)
    if forbidden is not None and forbidden[1]:
        return "deny", "it touches credentials, shell configuration, or agent state"

    deny_tokens = _DENY_TOKENS | set(config.deny_extra)
    pipelines = _pipelines(text)
    for pipeline in pipelines:
        interpreter_stage = False
        for tokens in pipeline:
            effective = _effective_tokens(tokens)
            head = effective[0] if effective else ""
            if head in deny_tokens:
                return "deny", "it escalates privileges or changes system state"
            if _dangerous_rm(effective):
                return "deny", "it recursively deletes your home directory or worse"
            if interpreter_stage and head in _PIPE_INTERPRETERS:
                return "deny", "it pipes downloaded content into an interpreter"
            if head in _DOWNLOADERS:
                interpreter_stage = True

    if forbidden is not None:
        # A broad glob that could expand to a credential or startup-file
        # name, with no exact or targeted mention anywhere. Confirm, not
        # deny — and the reason is spoken (see GLOB_CONFIRM_REASON): the
        # user must hear *why* this listing needs a yes, or the question
        # sounds like any other harmless command.
        return "confirm", GLOB_CONFIRM_REASON

    harmless_stripped = _HARMLESS_REDIRECT.sub(" ", text)
    has_structure = bool(
        _SUBSTITUTION.search(harmless_stripped)
        or _REDIRECTION.search(harmless_stripped)
        or _BACKGROUND.search(harmless_stripped)
    )
    if has_structure:
        return "confirm", "it redirects, substitutes, or backgrounds"

    allow = [tuple(entry.split()) for entry in config.auto_allow]
    for pipeline in pipelines:
        for tokens in pipeline:
            if not any(tuple(tokens[: len(entry)]) == entry for entry in allow):
                return "confirm", "it is not on the quiet allowlist"
    return "quiet", ""


def deny_decision(reason: str) -> dict[str, Any]:
    """The PreToolUse deny shape, shared by every voice-gated guard."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class ShellGuard:
    """PreToolUse hook enforcing the three tiers on every Bash call."""

    def __init__(
        self,
        config: ShellConfig,
        confirm: Callable[[str], Awaitable[bool]],
    ) -> None:
        self._config = config
        self._confirm = confirm

    async def __call__(
        self,
        payload: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        if payload.get("tool_name") != "Bash":
            return {}
        tool_input = payload.get("tool_input") or {}
        command = str(tool_input.get("command") or "")

        if tool_input.get("run_in_background"):
            # A backgrounded command outlives the confirmation that approved
            # it and reports through tools we keep disallowed.
            return deny_decision(
                "Background shell commands are not available. "
                "Run it in the foreground instead."
            )

        tier, reason = classify(command, self._config)
        if tier == "deny":
            log.warning("denied shell command (%s): %s", reason, command)
            return deny_decision(
                f"That command is off limits for Ciel — {reason}. Do not retry "
                "or rephrase it; briefly tell the user why, and suggest they "
                "run it themselves in a terminal if they want it."
            )
        if tier == "quiet":
            return {}

        limit = self._config.max_command_display_chars
        shown = command if len(command) <= limit else f"{command[:limit]}, and so on"
        question = f"Run: {shown} — okay?"
        if reason == GLOB_CONFIRM_REASON:
            question = f"Run: {shown} — careful, {reason} — okay?"
        try:
            approved = await self._confirm(question)
        except Exception:  # noqa: BLE001 - the hook must always return a decision
            log.exception("confirmation failed — denying %s", command)
            approved = False
        if approved:
            log.info("user approved shell command: %s", command)
            return {}
        log.info("user declined shell command: %s", command)
        return deny_decision(
            "The user was asked out loud and declined, or didn't answer. Do "
            "not retry the command; acknowledge briefly and move on."
        )

    def as_hooks(self) -> dict[str, list[HookMatcher]]:
        """The PreToolUse matcher list, scoped to Bash.

        The generous timeout is deliberate: a confirmation legitimately blocks
        for the spoken prompt plus the answer window plus one retry, and the
        SDK killing the hook mid-question would approve-by-crash or
        deny-by-crash on its own schedule rather than the user's. Raised
        past the *remote* gate's worst case — two 120-second texted answer
        windows back to back — for exactly that reason.
        """
        return {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[self], timeout=600)]}


__all__ = ["ShellGuard", "classify", "deny_decision", "Tier"]
