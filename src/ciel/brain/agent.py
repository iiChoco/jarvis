"""The brain: Claude, wrapped so it speaks in sentences.

Two responsibilities, both about latency.

The first is authentication, by omission: the Agent SDK inherits Claude Code's
subscription OAuth, so there is no API key here and none should be introduced.
Setting ``ANTHROPIC_API_KEY`` anywhere in the environment silently overrides
that and switches to pay-as-you-go billing.

The second is chunking. A response arrives as a stream of small text deltas. If
we waited for the whole thing before speaking, the user would sit in silence for
the full generation. Instead deltas are buffered and released at sentence
boundaries, so Ciel starts talking as soon as the first complete thought exists
and keeps talking while the rest is still being written.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import aclosing
from types import TracebackType
from typing import AsyncIterator, Awaitable, Callable, Self

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from ciel.brain.permissions import FILE_TOOLS, WorkspaceGuard
from ciel.brain.prompt import build_system_prompt
from ciel.brain.session import SessionStore
from ciel.brain.recorder import ActionRecorder
from ciel.brain.shellguard import ShellGuard
from ciel.brain.toolguard import ConfirmToolGuard
from ciel.config import BrainConfig, Config
from ciel.journal import ActionJournal

log = logging.getLogger(__name__)

# A sentence ends at .?! followed by whitespace — but not when the period is
# part of a decimal ("3.5" — the lookahead already rejects it, since the next
# character is a digit) or an ellipsis. Getting this wrong is audible: a
# premature split makes Ciel pause mid-number.
_BOUNDARY = re.compile(r"(?<!\.)([.!?])(?=[\s\"')\]]|$)")

# Abbreviations that end in a period without ending a sentence. The prompt
# discourages these, but the model still produces them occasionally. Ordinary
# words that double as abbreviations ("no", "us") don't belong here — "The
# answer is no." is a complete sentence far more often than "No. 5" appears.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "eg", "ie",
    "approx", "fig", "inc", "ltd", "co", "uk",
})

_TRAILING_WORD = re.compile(r"([A-Za-z]+)\.$")


def _is_real_boundary(candidate: str) -> bool:
    """Reject boundaries that fall inside an abbreviation."""
    match = _TRAILING_WORD.search(candidate.strip())
    if match is None:
        return True
    return match.group(1).lower() not in _ABBREVIATIONS


class Brain:
    """A conversational Claude session that yields speakable sentences."""

    def __init__(
        self,
        config: Config,
        memory_index_provider: "Callable[[], str | None] | None" = None,
        confirmer: "Callable[[str], Awaitable[bool]] | None" = None,
        journal: ActionJournal | None = None,
        projects_index_provider: "Callable[[], str | None] | None" = None,
    ) -> None:
        self._config = config
        self._brain_config: BrainConfig = config.brain
        # Providers, not strings: the indexes are read at every connect, so a
        # rotation picks up whatever reflection just wrote. A string would
        # freeze the indexes at construction time.
        self._memory_index_provider = memory_index_provider
        self._projects_index_provider = projects_index_provider
        self._client: ClaudeSDKClient | None = None
        self._sessions = SessionStore(
            config.session_file, config.brain.resume_window_minutes
        )
        self._session_id: str | None = None
        # Cost is split because the SDK's total_cost_usd is cumulative *per
        # session*: rotation rolls the finished session's cost into lifetime
        # and restarts the session counter, so per-turn deltas stay correct
        # and the total never jumps backwards.
        self._session_cost = 0.0
        self._lifetime_cost = 0.0
        self._last_turn_cost = 0.0
        self._needs_drain = False
        # One lock serializes everything that touches the client's message
        # stream — user turns, reflection turns, rotation. Two concurrent
        # receive_response() readers interleave and both corrupt.
        self._turn_lock = asyncio.Lock()
        self._mcp_servers: dict | None = None
        self._extra_tools: list[str] | None = None

        self._guard: WorkspaceGuard | None = None
        if config.files.enabled:
            self._guard = WorkspaceGuard(
                config.files.workspace, config.files.read_only_outside
            )
            self._guard.ensure_workspace()
            log.info("file access enabled, confined to %s", self._guard.workspace)

        # The shell needs both halves to exist: the config saying yes AND a
        # confirmer to route the spoken yes/no through. Absent either, Bash
        # stays in disallowed_tools exactly as before.
        self._shell_guard: ShellGuard | None = None
        if config.shell.enabled and confirmer is not None:
            self._shell_guard = ShellGuard(config.shell, confirmer)
            log.info("shell enabled behind the voice gate")

        # Connector tools on a `confirm` list get the same gate. Both halves
        # again: names to gate AND a confirmer to gate them through. Without
        # a confirmer nothing is silently widened — the names simply never
        # reach allowed_tools' quiet tier, and the calls deny like any other
        # unresolved confirmation would.
        self._tool_guard: ConfirmToolGuard | None = None
        self._gated_tools = frozenset(
            f"mcp__{name}__{tool}"
            for name, entry in config.mcp.items()
            for tool in entry.confirm
        )
        if self._gated_tools and confirmer is not None:
            self._tool_guard = ConfirmToolGuard(self._gated_tools, confirmer)
            log.info("%d connector tools behind the voice gate", len(self._gated_tools))

        # The recorder watches whatever can mutate: the file tools and Bash
        # (built into it), every confirm-gated connector tool, and the
        # iMessage send. It observes only — journaling lives outside the
        # guards so nothing here can grow into an enforcement path.
        self._recorder: ActionRecorder | None = None
        if journal is not None:
            self._recorder = ActionRecorder(
                journal, self._gated_tools | {"mcp__ciel__send_message"}
            )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def total_cost_usd(self) -> float:
        """Spend across the whole process — every session, rotations included.

        Note ``ResultMessage.total_cost_usd`` is *cumulative for the session*,
        not the cost of one turn — summing it across turns overstates spend
        dramatically. We mirror the session's running total and derive
        per-turn cost by difference; rotation banks the finished session into
        the lifetime sum.
        """
        return self._lifetime_cost + self._session_cost

    @property
    def last_turn_cost_usd(self) -> float:
        """Cost of the most recent turn alone."""
        return self._last_turn_cost

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _build_options(
        self,
        mcp_servers: dict | None = None,
        extra_tools: list[str] | None = None,
    ) -> ClaudeAgentOptions:
        previous = self._sessions.load()

        file_tools = list(FILE_TOOLS) if self._guard else []

        # A confirm-listed connector tool arrives via extra_tools already
        # allowed. That is only acceptable while the guard exists to hold it;
        # with no confirmer there is no gate, so the tool must not be offered
        # at all rather than run silently.
        if self._tool_guard is None and extra_tools:
            extra_tools = [t for t in extra_tools if t not in self._gated_tools]

        return ClaudeAgentOptions(
            model=self._brain_config.model,
            system_prompt=build_system_prompt(
                self._memory_index_provider() if self._memory_index_provider else None,
                workspace=str(self._guard.workspace) if self._guard else None,
                shell=self._shell_guard is not None,
                confirmed_actions=self._tool_guard is not None,
                undo=self._recorder is not None,
                projects_index=self._projects_index_provider()
                if self._projects_index_provider
                else None,
            ),
            # Explicit allowlist. The Agent SDK ships the full Claude Code
            # toolset — Bash, Write, Edit — and a voice assistant that can
            # silently run shell commands is not what we're building. Ciel's
            # own tools are appended so permission_mode="dontAsk" doesn't
            # silently block the very tools we just registered.
            allowed_tools=[
                *self._brain_config.allowed_tools,
                *file_tools,
                *(["Bash"] if self._shell_guard else []),
                *(extra_tools or []),
            ],
            # Bash is granted only behind the ShellGuard voice gate; without
            # it a shell walks straight around the workspace guard, which
            # would make the confinement theatre. Denied explicitly rather
            # than merely omitted when the gate is off, so a stray config
            # edit can't quietly re-enable it. KillShell and BashOutput stay
            # denied either way: they serve backgrounded commands, which the
            # gate refuses — a confirmation is for the command you heard,
            # not for whatever it keeps doing after the conversation moves on.
            disallowed_tools=[
                *([] if self._shell_guard else ["Bash"]),
                "KillShell",
                "BashOutput",
            ],
            # PreToolUse hooks, deliberately not can_use_tool: an
            # allowed_tools entry auto-approves its tool *before* that callback
            # runs, so a guard wired there is never consulted for the tools it
            # exists to constrain. Hooks run on every call regardless.
            hooks=self._hooks(),
            permission_mode="dontAsk",
            # Ignore ~/.claude and any project .claude directory. Without this
            # Ciel inherits the user's Claude Code config, skills, and memory,
            # which belong to a coding agent, not to this.
            setting_sources=[],
            include_partial_messages=True,
            # Opus 4.7+ omits thinking text by default (signature only), so
            # there is nothing to verbalize unless the summarized text is
            # explicitly requested. None keeps the model's default when the
            # text would only be dropped anyway.
            thinking={"type": "adaptive", "display": "summarized"}
            if self._brain_config.speak_thinking
            else None,
            mcp_servers=mcp_servers or {},
            max_turns=self._brain_config.max_turns,
            effort=self._brain_config.effort,
            # Relative paths resolve inside the workspace rather than wherever
            # Ciel happened to be launched from.
            cwd=str(self._guard.workspace) if self._guard else None,
            resume=previous.session_id if previous else None,
        )

    def _hooks(self) -> dict | None:
        """Merge the hook matchers of whichever guards and observers exist.

        Guards first, recorder last: when a guard denies, the recorder's
        pre-hook should never have snapshotted a call that won't run.
        """
        hooks: dict[str, list] = {}
        pre = hooks.setdefault("PreToolUse", [])
        if self._guard is not None:
            pre.extend(self._guard.as_hooks()["PreToolUse"])
        if self._shell_guard is not None:
            pre.extend(self._shell_guard.as_hooks()["PreToolUse"])
        if self._tool_guard is not None:
            pre.extend(self._tool_guard.as_hooks()["PreToolUse"])
        if self._recorder is not None:
            recorder_hooks = self._recorder.as_hooks()
            pre.extend(recorder_hooks["PreToolUse"])
            hooks["PostToolUse"] = recorder_hooks["PostToolUse"]
        return {k: v for k, v in hooks.items() if v} or None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(
        self,
        mcp_servers: dict | None = None,
        extra_tools: list[str] | None = None,
    ) -> None:
        async with self._turn_lock:
            await self._connect_locked(mcp_servers, extra_tools)

    async def _connect_locked(
        self,
        mcp_servers: dict | None = None,
        extra_tools: list[str] | None = None,
    ) -> None:
        """The actual connect; callers must hold the turn lock."""
        if self._client is not None:
            return
        # Remembered so rotation can rebuild the client without the pipeline
        # having to re-thread them.
        self._mcp_servers = mcp_servers if mcp_servers is not None else self._mcp_servers
        self._extra_tools = extra_tools if extra_tools is not None else self._extra_tools
        # Installed only after connect() succeeds: a half-connected client
        # left on self would make every later ask() skip reconnection and
        # query a dead subprocess — a failed rotation must leave the brain
        # in the "reconnect on next turn" state, not the "wedged" one.
        client = ClaudeSDKClient(
            options=self._build_options(self._mcp_servers, self._extra_tools)
        )
        await client.connect()
        self._client = client
        log.info("brain connected (model=%s)", self._brain_config.model)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
            self._needs_drain = False  # nothing stale survives a new client

    async def rotate(self) -> None:
        """Retire the current session and start a clean one.

        Post-rotate invariant: a new SDK session; no resumable reference to
        the old one; fresh memory and project indexes in the system prompt.
        """
        async with self._turn_lock:
            if self._client is None:
                return
            await self._client.disconnect()
            self._client = None
            self._needs_drain = False
            # Reflection (or any turn) may have persisted the current session
            # again minutes ago — well inside the resume window. Rotation
            # intentionally breaks conversational continuity, so the
            # persisted resume state must be cleared before reconnecting;
            # otherwise the reconnect resumes the very session this method
            # exists to discard. Not redundant. Do not remove.
            self._sessions.clear()
            self._lifetime_cost += self._session_cost
            self._session_cost = 0.0
            self._session_id = None
            await self._connect_locked()
            log.info("session rotated (lifetime cost $%.4f)", self._lifetime_cost)

    async def interrupt(self) -> None:
        """Abort the turn in flight, so a barge-in doesn't leave the model
        generating a response nobody will hear."""
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception:  # pragma: no cover - races with turn completion
                log.debug("interrupt failed (turn likely already done)", exc_info=True)

    # ── the conversational turn ──────────────────────────────────────────────

    async def ask(self, text: str) -> AsyncIterator[tuple[str, str]]:
        """Send a user turn; yield ``(kind, sentence)`` as they become available.

        ``kind`` is ``"thinking"`` for spoken reasoning and ``"reply"`` for the
        answer itself, so the pipeline can present them differently — same
        voice, but the listener deserves to know which one they're hearing.

        Yields whole sentences rather than raw deltas because that's the unit
        the speech synthesizer needs — half a sentence has the wrong prosody
        and lands as a stutter.

        Holds the turn lock for its whole lifetime, which puts an obligation
        on every consumer: if you stop iterating early, you MUST close the
        generator (``async with contextlib.aclosing(brain.ask(...))``).
        An abandoned generator keeps the lock until garbage collection gets
        around to finalizing it, and everything that needs a turn —
        including the next user question — deadlocks behind it.
        """
        buffer = ""
        spoken_any = False
        saw_result = False
        last_kind: str | None = None

        await self._turn_lock.acquire()
        try:
            if self._client is None:
                await self._connect_locked()
            assert self._client is not None

            await self._drain_stale()
            await self._client.query(text)

            async for message in self._client.receive_response():
                if isinstance(message, StreamEvent):
                    kind, delta = _delta(message)
                    if not delta:
                        continue
                    if kind == "thinking" and not self._brain_config.speak_thinking:
                        continue
                    # A block boundary (reasoning → answer) is a hard break:
                    # the unterminated tail of one must not fuse into the
                    # first sentence of the other.
                    if last_kind is not None and kind != last_kind and buffer.strip():
                        spoken_any = True
                        yield _label(last_kind), buffer.strip()
                        buffer = ""
                    last_kind = kind
                    buffer += delta
                    sentences, buffer = self._drain(buffer)
                    for sentence in sentences:
                        spoken_any = True
                        yield _label(last_kind), sentence

                elif isinstance(message, AssistantMessage):
                    # Fallback for a non-streaming turn. Partial events normally
                    # cover everything; this catches the case where they don't,
                    # so Ciel never goes mute on a valid response.
                    if not spoken_any and not buffer:
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                buffer += block.text

                elif isinstance(message, ResultMessage):
                    saw_result = True
                    self._record_result(message)

            # Whatever is left has no terminal punctuation but still needs
            # saying. Inside the lock scope: this yield is part of the turn.
            tail = buffer.strip()
            if tail:
                yield _label(last_kind), tail
        finally:
            # Abandoned mid-stream (barge-in): the rest of this turn's
            # messages — including its terminal ResultMessage — are still
            # queued, and receive_response() stops at the first ResultMessage
            # it sees, so they would otherwise be consumed as the start of the
            # *next* turn's response. Runs promptly only because consumers
            # close the generator (see the docstring) — that close is also
            # what releases the lock.
            if not saw_result:
                self._needs_drain = True
            self._turn_lock.release()

    async def reflect(self, prompt: str) -> None:
        """One silent turn: send the prompt, discard everything it says.

        Serialized with user turns by the same lock, because it *is* a turn —
        the model may call memory and project tools. Consuming the stream to
        exhaustion is the point: even a reflection hastened by interrupt()
        ends at its own ResultMessage, so the next user turn starts clean
        with no drain debt.
        """
        async with aclosing(self.ask(prompt)) as stream:
            async for _kind, _sentence in stream:
                pass

    def _record_result(self, message: ResultMessage) -> None:
        if message.session_id:
            self._session_id = message.session_id
            self._sessions.save(message.session_id)
        if message.total_cost_usd:
            # Cumulative for the current session, so assign rather than add;
            # rotation resets the session baseline, never this math.
            self._last_turn_cost = max(
                0.0, message.total_cost_usd - self._session_cost
            )
            self._session_cost = message.total_cost_usd
        if message.is_error:
            log.error("turn failed: %s", message.result or message.errors)

    async def _drain_stale(self) -> None:
        """Consume messages left over from a turn abandoned mid-stream."""
        if not self._needs_drain or self._client is None:
            return
        self._needs_drain = False

        client = self._client

        async def consume() -> None:
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    self._record_result(message)

        try:
            await asyncio.wait_for(consume(), timeout=10.0)
        except Exception:  # noqa: BLE001 - a failed drain must not block the turn
            log.debug("could not drain the abandoned turn", exc_info=True)

    def _drain(self, buffer: str) -> tuple[list[str], str]:
        """Split off every complete sentence.

        Returns the finished sentences and whatever text remains unterminated,
        which the caller keeps buffering into.
        """
        sentences: list[str] = []
        start = 0

        for match in _BOUNDARY.finditer(buffer):
            end = match.end()
            candidate = buffer[start:end]
            if not _is_real_boundary(candidate):
                continue
            text = candidate.strip()
            if text:
                sentences.append(text)
            start = end

        if sentences:
            return sentences, buffer[start:]

        # No sentence boundary yet. Past the flush threshold, release anyway —
        # otherwise a long run-on leaves the user in silence indefinitely.
        limit = self._brain_config.sentence_flush_chars
        if len(buffer) >= limit:
            cut = self._flush_point(buffer, limit)
            if cut > 0:
                head = buffer[:cut].strip()
                if head:
                    return [head], buffer[cut:]

        return [], buffer

    @staticmethod
    def _flush_point(buffer: str, limit: int) -> int:
        """Choose where to cut an over-long buffer.

        Prefer a clause boundary — a comma, semicolon, colon, or dash. Cutting
        at an arbitrary word break is audible: "...compared to drinking" / "none."
        puts a pause in a place no speaker would, because the synthesizer gives
        each chunk its own falling intonation. A clause boundary is somewhere a
        person would naturally draw breath.
        """
        window = buffer[:limit]
        # Ignore boundaries in the first third: cutting there leaves a stub too
        # short to be worth speaking on its own ("Yes," as its own utterance).
        earliest = limit // 3

        for marker in ("; ", ", ", ": ", " — ", " - "):
            cut = window.rfind(marker)
            if cut > earliest:
                return cut + len(marker)

        cut = window.rfind(" ")
        # The same guard applies to the word-break fallback, which otherwise
        # happily returns the space after the first word.
        return cut if cut > earliest else limit


def _label(kind: str | None) -> str:
    """Map a wire delta kind onto the spoken-sentence label."""
    return "thinking" if kind == "thinking" else "reply"


def _delta(event: StreamEvent) -> tuple[str, str]:
    """Extract ``(kind, text)`` from a raw streaming event.

    ``kind`` is ``"text"`` or ``"thinking"``; ``("", "")`` when the event
    carries neither. The Agent SDK passes Anthropic's wire events through
    untouched, so this reaches into the raw shape: ``content_block_delta``
    carrying a ``text_delta`` or ``thinking_delta``. Whether thinking is
    spoken or dropped is the caller's decision (``speak_thinking``), so both
    kinds are surfaced here.
    """
    raw = event.event
    if raw.get("type") != "content_block_delta":
        return "", ""
    delta = raw.get("delta") or {}
    if delta.get("type") == "text_delta":
        return "text", delta.get("text", "")
    if delta.get("type") == "thinking_delta":
        return "thinking", delta.get("thinking", "")
    return "", ""


__all__ = ["Brain"]
