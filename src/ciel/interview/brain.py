"""The interviewer's mind: a model session with no tools and a budget.

Ciel's own ``Brain`` is the wrong object for this. It carries the memory
index, the projects, the Mac's tools, the confirm gate, the personality —
everything that makes it the owner's assistant, and everything a friend
must never reach. The room needs a plain conversational session per
interview: a system prompt, streamed text, one structured call for a
brief or a debrief, an interrupt, and a ceiling on spend.

``Backend`` is that contract, and the room sees nothing else. Today's
implementation rides the Claude Agent SDK on the owner's subscription,
like Ciel's brain does; the day friends' usage deserves its own bill, an
``api`` backend on an explicit key is one more class here and one config
line — the SDK's ``ANTHROPIC_API_KEY`` override must never be used for
that, because it would silently rebill Ciel's own brain too. A
``ScriptedBackend`` answers with canned lines for the loopback dev server
and the probes: the whole room runs with no model and no network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Protocol

from ciel.config import InterviewConfig

log = logging.getLogger(__name__)


class BackendError(RuntimeError):
    """The model could not be reached or refused to answer."""


class Backend(Protocol):
    """One conversation with the interviewer, plus one-shot structured calls."""

    async def start(self, system_prompt: str) -> None: ...
    def ask(self, text: str) -> AsyncIterator[str]: ...
    async def ask_json(
        self, system_prompt: str, text: str, schema: dict[str, Any], *, effort: str | None = None
    ) -> dict[str, Any]: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...
    @property
    def cost_usd(self) -> float: ...


def _fenced_json(text: str) -> dict[str, Any] | None:
    """A JSON object out of a reply that may have wrapped it in a fence."""
    candidates = [text.strip()]
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if match:
        candidates.insert(0, match.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


# ── the Agent SDK ────────────────────────────────────────────────────────────


class AgentSdkBackend:
    """A ``ClaudeSDKClient`` per interview, with nothing attached."""

    def __init__(self, config: InterviewConfig) -> None:
        self._cfg = config
        self._client: Any = None
        self._cost = 0.0
        self._session_cost = 0.0
        self._lock = asyncio.Lock()

    @property
    def cost_usd(self) -> float:
        return self._cost + self._session_cost

    def _options(
        self,
        system_prompt: str,
        *,
        effort: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            model=self._cfg.model,
            system_prompt=system_prompt,
            # No tools of any kind: the base set is empty, so nothing ships
            # to the model as schema, and the disallow list closes the
            # ones the CLI might otherwise add on its own.
            tools=[],
            allowed_tools=[],
            disallowed_tools=[
                "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
                "WebSearch", "WebFetch", "Agent", "Task", "TodoWrite",
                "NotebookEdit", "KillShell", "BashOutput", "Skill",
            ],
            permission_mode="dontAsk",
            # Not the owner's ~/.claude: no skills, no memory, no settings.
            setting_sources=[],
            include_partial_messages=True,
            max_turns=2,
            effort=effort or self._cfg.effort,
            max_budget_usd=self._cfg.max_budget_usd,
            output_format=(
                {"type": "json_schema", "schema": schema} if schema else None
            ),
        )

    async def start(self, system_prompt: str) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        async with self._lock:
            if self._client is not None:
                return
            client = ClaudeSDKClient(options=self._options(system_prompt))
            started = time.monotonic()
            try:
                await client.connect()
            except Exception as exc:  # noqa: BLE001 - surfaced as one error type
                raise BackendError(f"could not start the interviewer: {exc}") from exc
            self._client = client
            log.info("interviewer connected in %.2fs (%s)", time.monotonic() - started, self._cfg.model)

    async def ask(self, text: str) -> AsyncIterator[str]:
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock

        if self._client is None:
            raise BackendError("interviewer not started")
        async with self._lock:
            try:
                await self._client.query(text)
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"the interviewer is gone: {exc}") from exc
            streamed = False
            fallback: list[str] = []
            async for message in self._client.receive_response():
                if isinstance(message, StreamEvent):
                    if message.parent_tool_use_id is not None:
                        continue
                    piece = _text_delta(message.event)
                    if piece:
                        streamed = True
                        yield piece
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            fallback.append(block.text)
                elif isinstance(message, ResultMessage):
                    if message.total_cost_usd is not None:
                        self._session_cost = float(message.total_cost_usd)
                    if message.is_error and not streamed and not fallback:
                        raise BackendError(
                            "; ".join(message.errors or []) or message.subtype or "turn failed"
                        )
            if not streamed:
                for piece in fallback:
                    yield piece

    async def ask_json(
        self, system_prompt: str, text: str, schema: dict[str, Any], *, effort: str | None = None
    ) -> dict[str, Any]:
        """One structured call on a client of its own — output format is
        fixed per connection in the SDK, so a brief or a debrief cannot
        share the interviewer's session."""
        from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock

        client = ClaudeSDKClient(options=self._options(system_prompt, effort=effort, schema=schema))
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"could not reach the model: {exc}") from exc
        try:
            await client.query(text)
            structured: Any = None
            texts: list[str] = []
            error: str | None = None
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            texts.append(block.text)
                elif isinstance(message, ResultMessage):
                    structured = message.structured_output
                    if message.total_cost_usd is not None:
                        self._cost += float(message.total_cost_usd)
                    if message.is_error:
                        error = "; ".join(message.errors or []) or message.subtype
                    if structured is None and message.result:
                        texts.append(message.result)
        finally:
            with _quiet():
                await client.disconnect()
        if isinstance(structured, dict):
            return structured
        parsed = _fenced_json("\n".join(texts))
        if parsed is not None:
            return parsed
        raise BackendError(error or "the model did not return the expected structure")

    async def interrupt(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception:  # noqa: BLE001 - an interrupt on a dead client is moot
            log.debug("interrupt failed", exc_info=True)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with _quiet():
                await client.disconnect()
        self._cost += self._session_cost
        self._session_cost = 0.0


class _quiet:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def _text_delta(event: dict[str, Any]) -> str:
    """The text in a raw streaming event, or ''."""
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    if delta.get("type") == "text_delta":
        return str(delta.get("text", ""))
    return ""


# ── scripted, for the dev server and the probes ──────────────────────────────


class ScriptedBackend:
    """Canned answers, no model. Knows the room's directives so the page
    can be exercised end to end: an exhibit on the second case turn, a
    problem on the second technical turn, an apology after a cut-off."""

    def __init__(self, config: InterviewConfig, *, delay_s: float = 0.15) -> None:
        self._cfg = config
        self._delay = delay_s
        self._system = ""
        self._turn = 0
        self._interrupted = False

    @property
    def cost_usd(self) -> float:
        return 0.0

    async def start(self, system_prompt: str) -> None:
        self._system = system_prompt

    def _mode(self) -> str:
        if "# The case" in self._system:
            return "case"
        if "# The coding problems" in self._system:
            return "technical"
        return "company"

    def _lines(self, text: str) -> list[str]:
        from ciel.interview.prompt import CUTOFF_MARKER

        if text.startswith(CUTOFF_MARKER):
            return ["Sorry, go on.", "Please finish your thought."]
        if text.startswith("[begin]"):
            return {
                "company": ["Thanks for making the time today. I'm Dana, I lead the platform team here.",
                            "To start, tell me a little about yourself and what drew you to this role."],
                "case": ["Good to meet you. Let's get straight into the case.",
                         "Our client is a regional grocery chain thinking about entering a neighbouring market.",
                         "How would you structure your thinking about whether they should?"],
                "technical": ["Thanks for joining. I'm Sam, one of the senior engineers on the team.",
                              "Before the coding part, tell me about a system you've built that you're proud of."],
            }[self._mode()]
        mode = self._mode()
        n = self._turn
        if mode == "case" and n == 1:
            return ["Good framing.", "Let me share some data on the market.", "[[exhibit: e1]]",
                    "What do you make of these numbers?"]
        if mode == "technical" and n == 1:
            return ["Great, let's move to a coding problem.", "[[problem: p1]]",
                    "Take a moment to read it and talk me through your approach as you go."]
        if "```" in text:
            return ["Thanks, I've read through your code.",
                    "Walk me through the time complexity, and tell me about an edge case you handled."]
        if "[end]" in text:
            return ["That's all the time we have.", "Thank you, this was a good conversation."]
        return [
            ["That's a useful example.", "Can you tell me about a time that approach didn't work?"],
            ["Interesting.", "How would you measure whether it succeeded?"],
            ["Good.", "What would you do differently with more time?"],
            ["I see.", "Let's move on: how do you handle disagreement with a stakeholder?"],
        ][n % 4]

    async def ask(self, text: str) -> AsyncIterator[str]:
        self._interrupted = False
        lines = self._lines(text)
        self._turn += 1
        for line in lines:
            # Stream a sentence in two halves, as a model would.
            cut = max(1, len(line) // 2)
            for piece in (line[:cut], line[cut:] + " "):
                if self._interrupted:
                    return
                await asyncio.sleep(self._delay)
                yield piece

    async def ask_json(
        self, system_prompt: str, text: str, schema: dict[str, Any], *, effort: str | None = None
    ) -> dict[str, Any]:
        from ciel.interview.prompt import scripted_json

        await asyncio.sleep(self._delay)
        return scripted_json(schema, text)

    async def interrupt(self) -> None:
        self._interrupted = True

    async def close(self) -> None:
        return None


def make_backend(config: InterviewConfig, *, dev: bool = False) -> Backend:
    """The room's one switch."""
    if dev:
        return ScriptedBackend(config)
    if config.backend == "api":
        raise BackendError(
            "[interview].backend = api is not implemented yet — set agent-sdk"
        )
    return AgentSdkBackend(config)


__all__ = [
    "AgentSdkBackend",
    "Backend",
    "BackendError",
    "ScriptedBackend",
    "make_backend",
]
