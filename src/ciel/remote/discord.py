"""The Discord lane: texting Ciel from wherever you are.

Codename: **Parallel Transport** — a turn carried along the path away from
home without changing what it is. The typed lane (Isomorphism) maps a line
on stdin onto a spoken turn; this lane extends that map past the front
door: a DM from the owner's account becomes the same kind of turn — same
brain, same session, same transcript — and the reply rides back as a text
instead of through the speakers. An ``@mention`` of the bot in a server
channel it was invited to is the same turn again, with one difference the
turn is told about: the reply lands where everyone in the channel can
read it.

Identity is pinned, never inferred. The Barn Door judges a voice; there is
no voice here to judge, so the gate is deterministic instead: only the
``owner_id`` account is read at all — its DMs, and its mentions of the
bot. Everyone else — strangers, other bots, the rest of a channel — is
dropped before the words reach anything that could act on them. That
matters more than it looks: this lane feeds text straight into a brain
with tools, and "who may speak here" is the whole security model.

Built hub-shaped on purpose (the endgame notes): the pipeline sees a small
queue-and-send surface — ``pending``/``peek``/``pop_batch`` inbound,
``send``/``typing`` outbound, both channel-aware — not discord.py. A
future transport implements the same members and nothing upstream changes.

Degrades to nothing, loudly-then-quietly: a missing dependency, a bad
token, or a dead network each log one warning and leave the voice loop
untouched. The lane is a convenience like the indicator — it must never be
able to take Ciel down with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import deque
from typing import Any, AsyncIterator

from ciel.config import DiscordConfig

log = logging.getLogger(__name__)

_DISCORD_MESSAGE_LIMIT = 2000
"""Discord's hard cap per message. Replies longer than this are split at
humane boundaries rather than truncated — a text lane that silently eats
the second half of an answer is worse than two notifications."""

_CATCHUP_GRACE_S = 600.0
"""How old a DM missed across a restart may be and still get answered.
The missed-timer grace reasoning at a stricter setting: a question from
ten minutes ago is still being waited on; acting on an instruction from
six hours ago is how stale orders get executed. The sweep exists chiefly
for the seconds a capability-grant reload takes — a text sent into that
gap must come back out of it."""


class RemoteUnavailable(RuntimeError):
    """The link cannot deliver right now — not connected, or the send was
    refused. One failure type upstream, whatever discord.py raised, so
    callers get the MessagesUnavailable shape they already know how to
    route (hold the event, log the reply)."""


def split_message(text: str, limit: int = _DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split an outbound reply into sendable chunks.

    Prefers a newline, then a space, and only cuts mid-word when a single
    unbroken run leaves no choice. Boundaries in the first half are
    ignored so a stray early newline doesn't produce a one-line fragment
    followed by another oversized remainder.
    """
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        cut = remaining.rfind("\n", limit // 2, limit)
        if cut < 0:
            cut = remaining.rfind(" ", limit // 2, limit)
        if cut < 0:
            cut = limit
        head = remaining[:cut].strip()
        if head:
            chunks.append(head)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _same_channel(a: Any, b: Any) -> bool:
    """Whether two channel handles name the same conversation.

    By id when both carry one — object identity is not guaranteed across
    gateway reconnects — with identity as the fallback for anything
    id-less (test fakes included)."""
    if a is b:
        return True
    a_id, b_id = getattr(a, "id", None), getattr(b, "id", None)
    return a_id is not None and a_id == b_id


class DiscordLink:
    """One owner's DMs and mentions, as a turn queue and a reply sink."""

    def __init__(self, config: DiscordConfig) -> None:
        self._config = config
        self._queue: deque[tuple[float, str, Any]] = deque()
        """Inbound turns awaiting the frame loop, oldest first:
        ``(arrival, text, channel)``. The monotonic arrival stamp is the
        typed deque's contract, for the typed deque's reason — it lets a
        pending confirmation tell a genuine answer from a message that
        predates the question. The channel is where the reply belongs: the
        owner's DM, or the server channel the mention came from."""
        self._seen: deque[int] = deque(maxlen=50)
        """Recently handled message ids: a live gateway event can race the
        reconnect backfill sweep, and answering one text twice reads as a
        malfunction."""
        self._last_seen: int | None = self._load_last_seen()
        """Newest DM id actually processed, mirrored to ``state_file`` on
        every accepted message — which is what lets the catch-up sweep
        span a restart instead of only a reconnect."""
        self._client: Any = None
        self._channel: Any = None
        self._task: asyncio.Task[None] | None = None
        self._discord: Any = None
        """The imported module, held from start() so backfill can build a
        history cursor without re-importing at the call site."""

    # ── inbound: the turn queue ──────────────────────────────────────────────

    @property
    def pending(self) -> bool:
        return bool(self._queue)

    def peek(self) -> tuple[float, str, Any] | None:
        return self._queue[0] if self._queue else None

    def pop(self) -> tuple[float, str, Any] | None:
        return self._queue.popleft() if self._queue else None

    def pop_batch(self) -> tuple[str, Any] | None:
        """Drain the head run of same-channel messages into one turn.

        People text in bursts — "hey" / "quick question" / the question —
        and three brain turns for one thought would answer the greeting
        with a paragraph. Only a *contiguous same-channel* run coalesces:
        a DM and a server mention are different conversations with
        different audiences, and their replies must not fuse. Returns
        ``(text, channel)``; anything arriving after simply becomes the
        next turn.
        """
        if not self._queue:
            return None
        _ts, text, channel = self._queue.popleft()
        lines = [text]
        while self._queue and _same_channel(self._queue[0][2], channel):
            lines.append(self._queue.popleft()[1])
        return "\n".join(lines), channel

    # ── outbound ─────────────────────────────────────────────────────────────

    @property
    def armed(self) -> bool:
        """Whether config gave the lane what it needs to exist at all —
        the claim startup logs make. Connection state is :attr:`can_send`."""
        return bool(self._config.resolved_token() and self._config.owner_id)

    @property
    def can_send(self) -> bool:
        """Whether a send would plausibly deliver *right now* — connected,
        with the owner's DM channel resolved. Consulted by the proactive
        policy, so "armed in config but currently offline" correctly
        reads as no away outlet rather than a message into the void."""
        return (
            self._channel is not None
            and self._client is not None
            and not self._client.is_closed()
        )

    async def send(self, text: str, channel: Any = None) -> None:
        """Deliver one reply, split to Discord's cap.

        ``channel`` targets a specific conversation (a server mention's
        channel); None means the owner's DM — the pinned default every
        pipeline-initiated send uses. Raises :class:`RemoteUnavailable`
        when it can't — callers decide what that costs (the proactive
        path holds its event; a turn reply just logs, since the user will
        ask again when they notice).
        """
        if not self.can_send:
            raise RemoteUnavailable("the Discord link is not connected")
        target = channel if channel is not None else self._channel
        try:
            for chunk in split_message(text):
                await target.send(chunk)
        except Exception as exc:  # noqa: BLE001 - normalized for callers
            raise RemoteUnavailable(f"{type(exc).__name__}: {exc}") from exc

    @contextlib.asynccontextmanager
    async def typing(self, channel: Any = None) -> AsyncIterator[None]:
        """Hold Discord's typing indicator while the body composes.

        A null context when the link is down, and indicator failures are
        swallowed either way — it's the remote pill, and cosmetics must
        never cost the turn they decorate.
        """
        cm = None
        if self.can_send:
            target = channel if channel is not None else self._channel
            try:
                cm = target.typing()
                await cm.__aenter__()
            except Exception:  # noqa: BLE001
                log.debug("could not start the typing indicator", exc_info=True)
                cm = None
        try:
            yield
        finally:
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    log.debug("could not stop the typing indicator", exc_info=True)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the gateway task; never blocks startup.

        Everything that can be wrong at this point — no token, no owner
        pinned, dependency not installed — is one warning and a disabled
        lane, mirroring how stdin degrades when it isn't readable.
        """
        token = self._config.resolved_token()
        if not token or not self._config.owner_id:
            log.warning(
                "discord lane enabled but not armed — set the token (file or "
                "config) and discord.owner_id (see the README's Discord section)"
            )
            return
        try:
            import discord
        except ImportError:
            log.warning(
                "discord.py is not installed — the Discord lane is off "
                "(install with: uv sync --extra discord)"
            )
            return
        self._discord = discord

        # Default intents carry DM content, and Discord exempts messages
        # that @mention the bot from the guild content restriction — the
        # two shapes this lane reads. Nothing privileged to toggle in the
        # developer portal.
        intents = discord.Intents.default()
        link = self

        class _Client(discord.Client):
            async def on_ready(self) -> None:
                await link._on_ready(self)

            async def on_message(self, message: Any) -> None:
                link._on_message(message)

        self._client = _Client(intents=intents)
        self._task = asyncio.create_task(self._run(token))

    async def _run(self, token: str) -> None:
        """The gateway connection, for the life of the process.

        discord.py reconnects through gateway drops on its own, and a
        *resumed* session replays the events it missed — a short nap loses
        nothing. What ends this task is terminal: a bad token, or
        shutdown. Either way the lane dies alone.
        """
        assert self._client is not None
        try:
            await self._client.start(token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the lane must never take the loop down
            hint = (
                " — the token was refused; re-copy it from the developer portal"
                if self._discord is not None
                and isinstance(exc, self._discord.LoginFailure)
                else ""
            )
            log.warning(
                "discord link died (%s: %s)%s — texting is off until restart",
                type(exc).__name__, exc, hint,
            )
        finally:
            self._channel = None

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed():
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 - best effort at shutdown
                log.debug("discord client close failed", exc_info=True)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ── gateway callbacks ────────────────────────────────────────────────────

    async def _on_ready(self, client: Any) -> None:
        """Resolve the owner's DM channel; fires on connect and re-identify.

        The channel is created from the pinned id rather than waiting for
        the owner to text first, because the away outlet needs to *send*
        unprompted — a proactive text must not depend on having ever been
        texted."""
        try:
            user = await client.fetch_user(self._config.owner_id)
            self._channel = await user.create_dm()
        except Exception as exc:  # noqa: BLE001 - a wrong id must not kill the task
            log.warning(
                "discord link cannot reach owner_id %s (%s: %s) — check the id",
                self._config.owner_id, type(exc).__name__, exc,
            )
            return
        log.info("discord link ready — signed in as %s, DMing %s", client.user, user)
        await self._backfill()

    async def _backfill(self) -> None:
        """Fetch DMs missed while disconnected — reloads included.

        Short gaps never get here: a resumed gateway session replays its
        missed events on its own. This covers re-identify *and* restart —
        the last-seen mark persists — which matters most for the seconds a
        capability-grant reload takes: "enable shell" / "done, reloading"
        / "now run the build" must not eat the third message. Everything
        after the last message actually seen is pulled from DM history in
        pages until drained, subject to the catch-up grace (acting on stale
        instructions is worse than asking again), and queued stamped as
        *predating
        everything* (monotonic 0.0) so a stale line can never be consumed
        as the answer to a live confirmation — it becomes an ordinary
        turn instead.

        Server mentions are not backfilled — trawling every channel of
        every guild on connect is a scan this lane has no business doing;
        a mention missed while down is asked again where everyone can see
        it wasn't answered.

        A truly first connect (no state file yet) backfills nothing: with
        no last-seen mark, "missed" has no meaning.
        """
        if self._last_seen is None or self._channel is None:
            return
        horizon = time.time() - _CATCHUP_GRACE_S
        # Paged until drained: a single capped fetch would take the oldest
        # page of a deep gap and silently never reach the newest messages —
        # the ones still worth acting on. The page cap bounds one fetch;
        # the page *count* cap bounds a pathological flood (the DM channel
        # includes the bot's own replies, so pages come partly pre-filtered).
        after_id = self._last_seen
        try:
            for _ in range(40):
                page_last: int | None = None
                count = 0
                async for message in self._channel.history(
                    limit=25,
                    after=self._discord.Object(id=after_id),
                    oldest_first=True,
                ):
                    count += 1
                    page_last = message.id
                    if message.created_at.timestamp() < horizon:
                        self._note_seen(message)  # too stale to act on, but seen
                        continue
                    self._on_message(message, backfill=True)
                if count < 25 or page_last is None:
                    return
                after_id = page_last
            log.warning("discord backfill stopped after 1000 messages")
        except Exception:  # noqa: BLE001 - a failed sweep costs the sweep, not the lane
            log.debug("discord backfill failed", exc_info=True)

    def _on_message(self, message: Any, backfill: bool = False) -> None:
        """The identity gate, then the queue. Synchronous and total: an
        exception escaping a discord.py event handler kills nothing, but
        the log noise reads as a broken lane, so everything is caught."""
        try:
            author = message.author
            if author.bot or author.id != self._config.owner_id:
                return
            text = (message.content or "").strip()
            if message.guild is not None:
                # A server channel. Only an explicit @mention of the bot by
                # the owner is addressed to Ciel — everything else in the
                # channel is other people's conversation, unread on
                # purpose. The mention token itself is noise; strip it.
                if not self._config.mentions:
                    return
                me = self._client.user if self._client is not None else None
                if me is None or me not in message.mentions:
                    return
                text = re.sub(rf"<@!?{me.id}>", "", text).strip()
            if message.id in self._seen:
                return  # a live event racing the backfill sweep
            self._note_seen(message)
            if not text:
                log.debug("discord message %s had no text — ignored", message.id)
                return
            if len(text) > self._config.max_inbound_chars:
                text = text[: self._config.max_inbound_chars]
            self._queue.append(
                (0.0 if backfill else time.monotonic(), text, message.channel)
            )
        except Exception:  # noqa: BLE001
            log.exception("discord message handling failed")

    # ── persistence ──────────────────────────────────────────────────────────

    def _note_seen(self, message: Any) -> None:
        """Record a message as handled, and advance the persisted mark.

        Only DMs move the mark — it cursors the DM history the backfill
        sweeps, and a newer guild-mention id would silently skip every DM
        between the two."""
        self._seen.append(message.id)
        if message.guild is None and (
            self._last_seen is None or message.id > self._last_seen
        ):
            self._last_seen = message.id
            try:
                self._config.state_file.write_text(
                    json.dumps({"last_seen": self._last_seen})
                )
            except OSError:
                log.debug("could not persist discord state", exc_info=True)

    def _load_last_seen(self) -> int | None:
        try:
            raw = json.loads(self._config.state_file.read_text())
            return int(raw["last_seen"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None


__all__ = ["DiscordLink", "RemoteUnavailable", "split_message"]
