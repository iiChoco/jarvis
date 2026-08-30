"""Probe the Discord lane (Parallel Transport) — scripted by default, live on request.

    uv run scripts/probe_discord.py               # scripted checks, no network
    uv run scripts/probe_discord.py --live        # connect, then echo your DMs
    uv run scripts/probe_discord.py --send "hi"   # one outbound DM, then exit

The scripted checks drive the broker's remote confirmation choreography and
the link's queue mechanics with fakes, so a regression shows up here before
it shows up as a silent bot. The live modes answer the setup questions one
layer at a time: the token and gateway (does it connect?), the owner id
(can it open your DM?), and delivery (do messages flow both ways?).

Live modes use the same config as Ciel (`[discord]` in ~/.ciel/config.toml,
or CIEL_DISCORD_TOKEN / CIEL_DISCORD_OWNER_ID for a one-off). If echoes
work here but Ciel stays silent, the lane is fine and the pipeline is the
place to look; if nothing works here, it's the token, the id, or the
missing mutual server (Discord only delivers DMs between accounts that
share one).
"""

import argparse
import asyncio
import logging
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from ciel.config import Config, DiscordConfig, load_config
from ciel.confirm import VoiceConfirmBroker
from ciel.remote.discord import DiscordLink, split_message

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeAuthor:
    def __init__(self, id: int, bot: bool = False) -> None:
        self.id = id
        self.bot = bot


class FakeChannel:
    _next_id = [500]

    def __init__(self, name: str, guild=None) -> None:
        FakeChannel._next_id[0] += 1
        self.id = FakeChannel._next_id[0]
        self.name = name
        self.guild = guild


_DM_CHANNEL = FakeChannel("dm")
"""One shared instance, as in the real lane: every DM in a conversation
arrives on the same channel id."""


class FakeClient:
    def __init__(self, user_id: int) -> None:
        self.user = FakeAuthor(user_id, bot=True)

    def is_closed(self) -> bool:
        return False


class FakeMessage:
    _next_id = [100]

    def __init__(
        self,
        author_id: int,
        content: str,
        bot: bool = False,
        guild=None,
        channel: FakeChannel | None = None,
        mentions: list | None = None,
    ) -> None:
        FakeMessage._next_id[0] += 1
        self.id = FakeMessage._next_id[0]
        self.author = FakeAuthor(author_id, bot)
        self.content = content
        self.guild = guild
        self.channel = channel or _DM_CHANNEL
        self.mentions = mentions or []


# ── scripted checks ──────────────────────────────────────────────────────────


async def broker_checks() -> None:
    """The remote Proof Obligation: texted question, texted (or typed)
    answer, every walk-away path resolving to deny."""
    print("remote confirmation:")
    config = replace(Config(), discord=DiscordConfig(confirm_timeout_s=0.4))
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    broker = VoiceConfirmBroker(config)

    with broker.remote(send):
        task = asyncio.create_task(broker.ask("Run: git push — okay?"))
        await asyncio.sleep(0.05)
        check("the question goes out as a text", sent == ["Run: git push — okay?"])
        check("remote_active while pending", broker.remote_active)
        check("an answer is accepted", broker.answer("yes"))
        check("yes approves", await task is True)
    check("remote_active clears with the turn", not broker.remote_active)

    sent.clear()
    with broker.remote(send):
        task = asyncio.create_task(broker.ask("Send it — okay?"))
        await asyncio.sleep(0.05)
        broker.answer("no thanks")
        check("no denies", await task is False)
    check("the refusal is acknowledged", sent[-1] == "Okay, skipping it.")

    sent.clear()
    with broker.remote(send):
        task = asyncio.create_task(broker.ask("Do the thing — okay?"))
        await asyncio.sleep(0.05)
        broker.answer("maybe later")
        await asyncio.sleep(0.05)
        check("an unclear answer earns one re-prompt", sent[-1] == "Yes or no?")
        broker.answer("yes go ahead")
        check("the re-prompted yes approves", await task is True)

    sent.clear()
    with broker.remote(send):
        result = await broker.ask("Anyone there — okay?")
    check("silence past the deadline denies", result is False)
    check("the timeout is announced", sent[-1] == "No answer — skipping it.")

    sent.clear()
    with broker.remote(send):
        task = asyncio.create_task(broker.ask("Doomed — okay?"))
        await asyncio.sleep(0.05)
        broker.cancel("shutdown")
        check("cancel denies", await task is False)
    check("cancel says nothing further", sent == ["Doomed — okay?"])

    async def dead_link(text: str) -> None:
        raise RuntimeError("gateway down")

    logging.disable(logging.WARNING)  # the expected dead-link warning
    try:
        with broker.remote(dead_link):
            check("an unaskable question denies", await broker.ask("Lost — okay?") is False)
    finally:
        logging.disable(logging.NOTSET)

    with broker.suppress(), broker.remote(send):
        check("suppression outranks remote", await broker.ask("Quiet — okay?") is False)

    check("voice path unchanged: unbound denies", await broker.ask("Plain — okay?") is False)


def link_checks(state_file) -> None:
    """The identity gate, the mention gate, and the queue contract."""
    print("the link:")
    config = DiscordConfig(
        token="t", owner_id=42, max_inbound_chars=10, state_file=state_file
    )
    link = DiscordLink(config)
    link._client = FakeClient(user_id=7)  # the bot's own account, for mentions
    from ciel.remote.lane import Lane

    check("DiscordLink conforms to the Lane protocol", isinstance(link, Lane))
    check("token plus owner id arms the lane", link.armed)
    check("unconnected means can_send is False", not link.can_send)

    link._on_message(FakeMessage(42, "hello"))
    link._on_message(FakeMessage(99, "a stranger"))
    link._on_message(FakeMessage(42, "a bot", bot=True))
    link._on_message(FakeMessage(42, "   "))
    link._on_message(FakeMessage(42, "0123456789ABCDEF"))
    check("only the owner's messages queue", len(link._queue) == 2)
    ts, first, _ch = link.peek()
    check("live messages carry their arrival time", ts > 0.0 and first == "hello")
    check("the inbound cap truncates", link._queue[-1][1] == "0123456789")

    # ── the mention lane ─────────────────────────────────────────────────
    guild = object()
    hall = FakeChannel("general", guild=guild)
    me = link._client.user
    link._on_message(FakeMessage(42, "<@7> status?", guild=guild, channel=hall,
                                 mentions=[me]))
    link._on_message(FakeMessage(42, "no mention here", guild=guild, channel=hall))
    link._on_message(FakeMessage(99, "<@7> hi ciel", guild=guild, channel=hall,
                                 mentions=[me]))
    mentioned = [item for item in link._queue if item[2] is hall]
    check("an owner @mention in a server queues", len(mentioned) == 1)
    check("the mention token is stripped", mentioned[0][1] == "status?")
    check(
        "unmentioned channel talk and strangers' mentions do not",
        sum(1 for item in link._queue if item[2] is hall) == 1,
    )

    twice = FakeMessage(42, "once")
    link._on_message(twice)
    link._on_message(twice)
    check(
        "a message seen twice queues once",
        sum(1 for _, t, _c in link._queue if t == "once") == 1,
    )

    stale_dm = FakeMessage(42, "stale")
    link._on_message(stale_dm, backfill=True)
    check("backfilled messages predate everything", link._queue[-1][0] == 0.0)

    # ── batching: same channel coalesces, channels never fuse ────────────
    batch = link.pop_batch()
    check(
        "the DM head run coalesces into one turn",
        batch is not None and batch[0] == "hello\n0123456789",
    )
    batch = link.pop_batch()
    check(
        "a channel boundary ends the batch",
        batch is not None and batch[0] == "status?" and batch[1] is hall,
    )
    batch = link.pop_batch()
    check(
        "the later DM run stands alone",
        batch is not None and batch[0] == "once\nstale" and batch[1] is not hall,
    )
    check("drained means not pending", not link.pending and link.pop_batch() is None)

    # ── persistence: the last-seen mark survives a restart ───────────────
    check("the DM mark persisted", state_file.exists())
    reborn = DiscordLink(config)
    check(
        "a fresh link loads the persisted mark",
        reborn._last_seen == link._last_seen and reborn._last_seen is not None,
    )
    link._on_message(
        FakeMessage(42, "<@7> later", guild=guild, channel=hall, mentions=[me])
    )
    check(
        "guild mentions never advance the DM cursor",
        link._last_seen == stale_dm.id,  # still the newest *DM*
    )

    long = ("word " * 900).strip()
    parts = split_message(long)
    check("long replies split under the cap", all(len(p) <= 2000 for p in parts))
    check("splitting loses nothing", " ".join(parts).split() == long.split())
    check("a short reply stays whole", split_message("short") == ["short"])
    check("an empty reply sends nothing", split_message("   ") == [])


# ── live modes ───────────────────────────────────────────────────────────────


async def wait_ready(link: DiscordLink, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if link.can_send:
            return True
        await asyncio.sleep(0.2)
    return False


async def live(send_once: str | None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    if not config.discord.token or not config.discord.owner_id:
        print("set discord.token and discord.owner_id first (see the README)")
        return 1

    link = DiscordLink(config.discord)
    await link.start()
    print("connecting…")
    if not await wait_ready(link):
        print("never became ready — check the log above (token? owner id?)")
        await link.close()
        return 1
    print("ready — the DM channel is open")

    try:
        if send_once is not None:
            await link.send(send_once)
            print("sent")
            return 0
        print("echoing your DMs — text the bot, Ctrl-C to stop")
        while True:
            while link.pending:
                _ts, text = link.pop()
                print(f"  heard: {text}")
                await link.send(f"heard: {text}")
            await asyncio.sleep(0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        await link.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="connect and echo DMs")
    parser.add_argument("--send", metavar="TEXT", help="send one DM and exit")
    args = parser.parse_args()

    if args.live or args.send is not None:
        return await live(args.send)

    await broker_checks()
    with tempfile.TemporaryDirectory() as tmp:
        link_checks(Path(tmp) / "discord.json")
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
