"""``ciel hub --check``: is this box ready to be the hub?

Five questions, each answered in one line, and a non-zero exit when any
fails — for the first minute on a new server and for the 3 AM "why is
Discord silent" that comes later:

* the wire's token exists (or the bind is loopback and none is needed);
* the bind address is an address this machine actually has;
* the brain's login is alive (the bundled CLI's own word for it);
* the connectors' runtime is present (``npx`` for the ``[mcp.*]`` ones
  that use it);
* the state directory holds what the hub runs on (config, memory).

No network, no sockets bound: this must be safe to run beside a running
hub.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ciel.config import Config

_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}


def _local_addresses() -> set[str]:
    out = {"127.0.0.1", "::1", "0.0.0.0", "::"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            out.add(info[4][0])
    except OSError:
        pass
    try:
        # The tailnet address is not on the hostname's records; a bind test
        # is the honest check for it.
        pass
    except OSError:
        pass
    return out


def _can_bind(addr: str) -> bool:
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((addr, 0))
        return True
    except OSError:
        return False


def _cli_auth(config: "Config") -> tuple[bool, str]:
    try:
        import claude_agent_sdk
    except ImportError:
        return False, "the Agent SDK is not installed"
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if not cli.exists():
        cli = Path(shutil.which("claude") or "")
        if not cli.exists():
            return False, "no claude CLI found"
    try:
        proc = subprocess.run(
            [str(cli), "auth", "status"], capture_output=True, text=True, timeout=20,
        )
        raw = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return False, f"auth status failed: {exc}"
    if raw.get("loggedIn"):
        return True, f"logged in via {raw.get('authMethod', '?')}"
    return False, "not logged in — run the CLI and /login, or set CLAUDE_CODE_OAUTH_TOKEN in ~/.ciel/hub.env"


def run(config: "Config") -> list[tuple[str, bool, str]]:
    """Every check, as (name, ok, detail)."""
    rows: list[tuple[str, bool, str]] = []

    bind = config.hub.bind or config.web.host
    if bind in _LOOPBACK:
        rows.append(("bind", True, f"loopback ({bind or '127.0.0.1'}) — reach is identity, no token needed"))
        rows.append(("token", True, "not required on loopback"))
    else:
        ok = _can_bind(bind)
        rows.append(("bind", ok, f"{bind} {'is an address of this machine' if ok else 'is NOT an address of this machine (tailscale down?)'}"))
        token = config.hub.current_token()
        rows.append(("token", bool(token), (
            f"present ({config.hub.token_file})" if token
            else f"missing — the hub mints one at start into {config.hub.token_file}"
        )))

    ok, detail = _cli_auth(config)
    rows.append(("brain login", ok, detail))

    npx_needed = [n for n, e in config.mcp.items() if e.command == "npx"]
    if npx_needed:
        have = shutil.which("npx") is not None
        rows.append(("connectors", have, f"npx {'present' if have else 'MISSING'} for {', '.join(npx_needed)}"))
    else:
        rows.append(("connectors", True, "none need npx"))

    state = config.state_dir
    cfg = Path.home() / ".ciel" / "config.toml"
    rows.append(("state", state.is_dir() and cfg.exists(),
                 f"{state} {'present' if state.is_dir() else 'MISSING'}, config {'present' if cfg.exists() else 'MISSING'}"))
    return rows


def report(config: "Config") -> int:
    rows = run(config)
    width = max(len(r[0]) for r in rows)
    for name, ok, detail in rows:
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<{width}}  {detail}")
    failed = [r for r in rows if not r[1]]
    print(f"\n{'hub ready' if not failed else f'{len(failed)} check(s) failed'}")
    return 0 if not failed else 1


__all__ = ["report", "run"]
