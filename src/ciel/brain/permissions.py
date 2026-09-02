"""Tool permission guards.

Codename: **Compact Support** (the workspace boundary — identically zero
outside it) and **Singularities** (``FORBIDDEN_NAMES`` — points where Ciel is
simply undefined, however much you insist).

Ciel's threat model is unusual enough to be worth stating plainly, because it
is why this file exists rather than just adding ``Write`` to a list.

1. **Its instructions arrive as transcribed speech.** Whisper misheard "Ciel"
   as "ZL" until we biased the decoder. Anything it mishears becomes an
   instruction, and nobody reads it back to you before it runs.
2. **Untrusted text enters its context.** Ciel reads web pages. A page can
   contain text addressed to the model, and a model with unconstrained file
   write is a model that can be talked into writing wherever that text says.
3. **Nothing asks you first.** ``permission_mode="dontAsk"`` means tool calls
   execute silently — appropriate for a hands-free assistant, and exactly what
   makes an unbounded filesystem tool a bad idea.

So file access is confined to one directory. The guard resolves every path the
model supplies and rejects anything that lands outside — resolution first,
which is what defeats ``..`` traversal and symlinks pointing out of the
workspace.

**It is a PreToolUse hook, not a ``can_use_tool`` callback, and that
distinction is load-bearing.** An entry in ``allowed_tools`` auto-approves a
tool *before* ``can_use_tool`` is consulted, so a guard wired up that way is
never invoked for the very tools it exists to constrain — verified the hard
way: the callback logged nothing while the model wrote happily to ``/tmp``.
PreToolUse runs for every call regardless of the allowlist.

**Bash is guarded separately, not here.** A shell escapes path confinement
trivially (`sh -c 'cat > ~/.zshrc'`), so it never gets a place on the file
allowlist — it has its own gate in ``shellguard.py``, which hard-denies the
dangerous shapes (including these forbidden names, which it imports) and
requires a spoken yes from the user for anything not provably read-only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

log = logging.getLogger(__name__)

# Tools enabled when file access is on. Read/Glob/Grep are included because a
# write-only assistant is nearly useless — it cannot check whether it already
# wrote something, or amend a file it produced a minute ago.
FILE_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")

# Which input field carries the path, per tool. Anything absent from this map
# passes through untouched — every tool that can name a filesystem path is
# listed here, and it is the tool allowlist, not this map, that keeps
# unknown tools out.
_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path",),
    "Grep": ("path",),
}

# Off limits regardless of where the workspace points.
#
# This started as cheap insurance for a narrow workspace. With the workspace
# set to the home directory it becomes the *primary* defense rather than a
# backstop, which is why it is thorough rather than token: a home-wide
# workspace otherwise exposes shell startup files (persistent code execution),
# every stored credential, and the browser and Messages databases.
#
# Matched against any component of the resolved path, so `~/.ssh/id_rsa` and
# `~/backup/.ssh/id_rsa` are both refused. Public because the shell gate
# scans command strings against the same names — one list, one place to grow.
FORBIDDEN_NAMES = frozenset({
    # Shell startup — writable shell config is arbitrary code execution on the
    # user's next login, which makes it the highest-value target here.
    ".zshrc", ".zshenv", ".zprofile", ".zlogin",
    ".bashrc", ".bash_profile", ".profile", ".bash_login",
    # Credentials and keys
    ".ssh", ".aws", ".gnupg", ".gpg", ".netrc", ".env",
    ".npmrc", ".pypirc", ".docker", ".kube", ".terraform.d",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "credentials.json",
    # The Discord bot token — the away lane's whole identity. Lives in its
    # own file precisely so this list can name it; config.toml itself stays
    # readable.
    "discord.token",
    # The Oura authorization — client secret and a refresh token, same
    # arrangement: its own file so this list can name it.
    "oura.json",
    # The hub token — the wire's shared secret; its own file for the same
    # reason.
    "hub.token",
    # Agent and CLI state. `.claude` is not just settings — it holds session
    # transcripts and history from every coding session, which is a broader
    # disclosure than any single credential file.
    ".claude", ".claude.json", ".config",
    # Login items and launch agents — another persistence path
    "LaunchAgents", "LaunchDaemons",
})

# Whole subtrees that are off limits. Home-relative, matched by prefix.
# ~/Library holds keychains, browser cookies and history, the Messages
# database, and Mail — none of which anyone means to include when they say
# "let the assistant work with my files".
_FORBIDDEN_SUBTREES: tuple[str, ...] = ("Library",)


class WorkspaceGuard:
    """Confines file tools to a single directory tree.

    One deliberate carve-out: reads (never writes) under the journal's
    snapshots directory. The undo prompt sends the model there to restore a
    file's previous contents, and the journal's design keeps undo inside the
    ordinary tools and guards — which only works if the ordinary Read can
    actually reach the snapshot when the workspace is narrow.
    """

    def __init__(
        self,
        workspace: Path,
        read_only_outside: bool = False,
        snapshot_dir: Path | None = None,
    ) -> None:
        self._workspace = workspace.expanduser().resolve()
        self._read_only_outside = read_only_outside
        self._snapshots = (
            snapshot_dir.expanduser().resolve() if snapshot_dir is not None else None
        )

    @property
    def workspace(self) -> Path:
        return self._workspace

    def ensure_workspace(self) -> None:
        self._workspace.mkdir(parents=True, exist_ok=True)

    async def __call__(
        self,
        payload: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        """PreToolUse hook. Returning ``{}`` means "no opinion", which allows."""
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}

        # Tools that never touch the filesystem pass through untouched.
        if tool_name not in _PATH_FIELDS:
            return {}

        is_write = tool_name in {"Write", "Edit", "NotebookEdit"}

        for field in _PATH_FIELDS[tool_name]:
            raw = tool_input.get(field)
            if raw is None:
                # Glob and Grep default to the working directory when no path
                # is given, and cwd is the workspace — so this is fine.
                continue

            verdict = self._check(str(raw), is_write=is_write)
            if verdict is not None:
                log.warning("denied %s on %s: %s", tool_name, raw, verdict)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": verdict,
                    }
                }

        return {}

    def as_hooks(self) -> dict[str, list[HookMatcher]]:
        """The ``hooks`` mapping to hand to ClaudeAgentOptions."""
        return {"PreToolUse": [HookMatcher(hooks=[self])]}

    def _check(self, raw_path: str, *, is_write: bool) -> str | None:
        """Return a denial reason, or ``None`` if the path is acceptable."""
        try:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = self._workspace / candidate
            # Resolve *before* comparing. This is the whole trick: it collapses
            # `..` segments and follows symlinks, so a link inside the
            # workspace pointing at /etc is caught here rather than sailing
            # through a naive string prefix check.
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return f"That path could not be resolved ({exc})."

        if any(part in FORBIDDEN_NAMES for part in resolved.parts):
            return (
                "That path touches credentials, shell configuration, or agent "
                "state, which is off limits."
            )

        try:
            relative = resolved.relative_to(Path.home())
        except ValueError:
            relative = None
        if relative is not None and relative.parts[:1] and relative.parts[0] in _FORBIDDEN_SUBTREES:
            return (
                "That path is inside the Library folder, which holds keychains, "
                "browser data, and messages. It is off limits."
            )

        if (
            not is_write
            and self._snapshots is not None
            and self._snapshots in resolved.parents
            # A snapshot's filename ends with the original file's name. A
            # stray snapshot of a forbidden file should never exist (denied
            # calls are not snapshotted), but if one ever does, reading it
            # here would resurrect exactly what the names list buries.
            and not any(resolved.name.endswith(f"-{bad}") for bad in FORBIDDEN_NAMES)
        ):
            return None

        inside = resolved == self._workspace or self._workspace in resolved.parents
        if inside:
            return None

        if not is_write and self._read_only_outside:
            return None

        return (
            f"Ciel can only touch files inside {self._workspace}. "
            f"That path is outside it."
        )


__all__ = ["WorkspaceGuard", "FILE_TOOLS", "FORBIDDEN_NAMES"]
