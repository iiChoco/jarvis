"""Entry point: ``ciel`` or ``uv run -m ciel``."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import sys
from pathlib import Path

from ciel.config import Config, load_config
from ciel.pipeline import Pipeline

log = logging.getLogger("ciel")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ciel",
        description="Ciel — a local-first voice assistant.",
    )
    parser.add_argument(
        "--wake",
        choices=["wakeword", "hotkey", "always"],
        help="how Ciel decides it's being addressed (default: from config)",
    )
    parser.add_argument("--model", help="override the Claude model")
    parser.add_argument("--voice", help="override the TTS voice")
    parser.add_argument(
        "--tts", choices=["say", "piper"], help="override the speech engine"
    )
    parser.add_argument(
        "--indicator",
        choices=["hud", "terminal", "none"],
        help="on-screen status indicator (default: from config)",
    )
    parser.add_argument(
        "--files",
        action="store_true",
        help="let Ciel read and write files, confined to the workspace directory",
    )
    parser.add_argument(
        "--workspace",
        metavar="DIR",
        help="directory Ciel may use for files (implies --files)",
    )
    parser.add_argument(
        "--new", action="store_true", help="start a fresh conversation, ignoring the last one"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    if args.wake:
        config = dataclasses.replace(config, wake=dataclasses.replace(config.wake, mode=args.wake))
    if args.model:
        config = dataclasses.replace(config, brain=dataclasses.replace(config.brain, model=args.model))
    if args.tts or args.voice:
        tts = config.tts
        if args.tts:
            tts = dataclasses.replace(tts, engine=args.tts)
        if args.voice:
            tts = dataclasses.replace(tts, voice=args.voice)
        config = dataclasses.replace(config, tts=tts)
    if args.indicator:
        config = dataclasses.replace(
            config, ui=dataclasses.replace(config.ui, indicator=args.indicator)
        )
    if args.files or args.workspace:
        files = dataclasses.replace(config.files, enabled=True)
        if args.workspace:
            files = dataclasses.replace(
                files, workspace=Path(args.workspace).expanduser()
            )
        config = dataclasses.replace(config, files=files)
    return config


def _log_level(name: str) -> int:
    """Map a config log-level name to its numeric level, INFO on nonsense."""
    level = logging.getLevelName(name.strip().upper())
    return level if isinstance(level, int) else logging.INFO


def _check_auth() -> None:
    """Warn if an API key is set.

    The Agent SDK inherits Claude Code's subscription OAuth. Setting
    ANTHROPIC_API_KEY silently overrides that and switches to pay-as-you-go
    API billing — which is a surprising way to discover you've been billed
    twice, so it's worth saying out loud rather than failing quietly.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "warning: ANTHROPIC_API_KEY is set, so Ciel will bill as pay-as-you-go\n"
            "         API usage instead of using your Claude subscription.\n"
            "         Unset it to use the subscription.\n",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Line-buffer stdout even when it isn't a terminal. Under launchd (or
    # any pipe), Python block-buffers prints — the conversation feed would
    # sit in an 8 KB buffer and reach ciel.out.log in bursts, so a tail -f
    # lags by minutes and a crash eats whatever was buffered. The terminal
    # was always line-buffered; the log file deserves the same truth.
    # Guarded: stdout may be closed entirely in exotic spawns.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if args.verbose else "%(message)s",
    )
    # These are chatty at INFO and say nothing a user of Ciel wants to read.
    for noisy in ("httpx", "faster_whisper", "openwakeword", "urllib3", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _check_auth()

    config = _apply_overrides(load_config(), args)
    if not args.verbose:
        # -v forces DEBUG; otherwise config.log_level (into which load_config
        # folds CIEL_LOG_LEVEL) sets the level. basicConfig above already
        # installed the handler, so adjust the root level here — without this
        # the log_level field and its env var were parsed and never applied.
        logging.getLogger().setLevel(_log_level(config.log_level))
    if args.new:
        config.session_file.unlink(missing_ok=True)

    pipeline = Pipeline(config)
    try:
        asyncio.run(pipeline.run())
    except KeyboardInterrupt:
        print("\nbye")
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard
        log.error("%s", exc)
        if args.verbose:
            raise
        return 1

    if pipeline.reload_requested:
        # Replace this process with a fresh one running the new code — a real
        # restart in everything but who typed it. execv never returns; the
        # same interpreter, arguments, and environment carry over, and the
        # conversation resumes through the session file.
        #
        # --new is stripped from the carried args: it means "start fresh this
        # launch, once", and it deleted the session file above. Carried
        # forward, every autoreload restart would delete the session again —
        # discarding the very conversation the reload is meant to resume.
        # (argparse accepts unambiguous prefixes, and only --new starts with
        # "--n", so those abbreviations are dropped too.)
        raw = list(argv) if argv is not None else sys.argv[1:]
        carried = [
            a for a in raw
            if not (a.startswith("--n") and "--new".startswith(a))
        ]
        print("restarting...\n")
        os.execv(
            sys.executable,
            [sys.executable, "-m", "ciel", *carried],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
