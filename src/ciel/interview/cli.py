"""``ciel interview …`` — the owner's command line for the room.

Accounts are created here before there is an admin panel to create them
in, and the page is developed here against a loopback server with no hub
behind it. Everything the panel can do, this can do; the panel is the
convenience, this is the guarantee.

    ciel interview add-user alice            # prints the generated password
    ciel interview add-user me --admin       # the owner's own account
    ciel interview reset alice
    ciel interview disable alice / enable alice / delete alice
    ciel interview list
    ciel interview serve --dev [--port 8797] # loopback, scripted brain
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from pathlib import Path

from ciel.config import Config, load_config
from ciel.interview.accounts import ACCOUNTS_FILE, Accounts

log = logging.getLogger(__name__)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ciel interview", description="the interview room's owner tools"
    )
    parser.add_argument("--dir", metavar="DIR", help="override [interview].dir")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add-user", help="create an account; prints its password once")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true", help="make it an owner account")

    for name, help_ in (
        ("reset", "new password for an account; prints it once"),
        ("disable", "refuse the account's logins"),
        ("enable", "allow them again"),
        ("delete", "remove the account (its sessions stay on disk)"),
    ):
        one = sub.add_parser(name, help=help_)
        one.add_argument("username")

    sub.add_parser("list", help="every account, one per line")

    seed = sub.add_parser(
        "seed-cases", help="ask the model to write consulting cases into the room's case library"
    )
    seed.add_argument("-n", type=int, default=3, help="how many (default 3)")
    seed.add_argument("--type", choices=["any", "product", "project", "company"], default="any")
    seed.add_argument("--dev", action="store_true", help="use the scripted brain (one canned case)")

    sub.add_parser("cases", help="list the case library: bundled and added")

    serve = sub.add_parser(
        "serve", help="run the room alone on loopback, for working on the page"
    )
    serve.add_argument("--dev", action="store_true",
                       help="dev/dev account, scripted brain, no real model")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8797)
    serve.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _config(args: argparse.Namespace) -> Config:
    config = load_config()
    if args.cmd == "serve" and args.dev and not args.dir:
        # Development never touches the real room: its dev/dev account and
        # its test sessions live in a sibling directory.
        args.dir = str(config.state_dir / "interview-dev")
    if args.dir:
        config = dataclasses.replace(
            config,
            interview=dataclasses.replace(
                config.interview, dir=Path(args.dir).expanduser()
            ),
        )
    return config


def _accounts(config: Config) -> Accounts:
    return Accounts(config.interview.dir / ACCOUNTS_FILE)


async def _serve(config: Config, args: argparse.Namespace) -> None:
    from aiohttp import web

    from ciel.interview.app import PREFIX, InterviewApp

    room = InterviewApp(config, dev=args.dev, port=args.port)
    await room.start()
    app = web.Application()
    room.register(app.router)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    print(f"interview room at http://{args.host}:{args.port}{PREFIX}")
    if args.dev:
        print("  sign in as dev / dev")
    try:
        await asyncio.Event().wait()
    finally:
        await room.close()
        await runner.cleanup()


async def _seed_cases(config: Config, args: argparse.Namespace) -> None:
    from ciel.interview import prompt as prompts
    from ciel.interview.brain import make_backend
    from ciel.interview.cases import CaseLibrary

    library = CaseLibrary(config.interview.dir / "cases")
    have = {c["slug"] for c in library.all()}
    titles = "; ".join(c["title"] for c in library.all())
    for i in range(args.n):
        backend = make_backend(config.interview, dev=args.dev)
        setup = {"length_min": 30, "case_type": args.type,
                 "request": f"Not one of these existing cases: {titles}" if titles else ""}
        system, user, schema = prompts.brief_request("case", setup)
        try:
            case = await backend.ask_json(system, user, schema, effort="high")
        finally:
            await backend.close()
        if case.get("slug") in have:
            case["slug"] = f"{case['slug']}-{i + 1}"
        try:
            path = library.save(config.interview.dir / "cases", case)
        except ValueError as exc:
            print(f"skipped one case: {exc}", file=sys.stderr)
            continue
        have.add(case["slug"])
        print(f"wrote {path}")


def main(argv: list[str]) -> int:
    args = _parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(message)s",
    )
    config = _config(args)
    accounts = _accounts(config)

    try:
        if args.cmd == "add-user":
            password = accounts.create(args.username, "admin" if args.admin else "user")
            print(f"{args.username}: {password}")
            print("(shown once — the file holds only a hash)")
        elif args.cmd == "reset":
            print(f"{args.username}: {accounts.reset(args.username)}")
        elif args.cmd == "disable":
            accounts.set_disabled(args.username, True)
            print(f"{args.username} disabled")
        elif args.cmd == "enable":
            accounts.set_disabled(args.username, False)
            print(f"{args.username} enabled")
        elif args.cmd == "delete":
            accounts.delete(args.username)
            print(f"{args.username} deleted")
        elif args.cmd == "list":
            rows = accounts.list()
            if not rows:
                print(f"no accounts yet ({accounts.path})")
            for a in rows:
                flags = " ".join(
                    f for f in (a.role if a.is_admin else "", "disabled" if a.disabled else "")
                    if f
                )
                print(f"{a.username:<24} created {a.created[:10]}  "
                      f"last seen {(a.last_seen or '-')[:10]}  {flags}")
        elif args.cmd == "cases":
            from ciel.interview.cases import CaseLibrary

            for case in CaseLibrary(config.interview.dir / "cases").all():
                print(f"{case['slug']:<44} {case['type']:<8} {case['title']}")
        elif args.cmd == "seed-cases":
            asyncio.run(_seed_cases(config, args))
        elif args.cmd == "serve":
            try:
                asyncio.run(_serve(config, args))
            except KeyboardInterrupt:
                print("\nbye")
                return 130
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


__all__ = ["main"]
