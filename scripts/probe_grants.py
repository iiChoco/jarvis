"""Probe the capability grants — the catalog, the surgery, the tools.

    uv run scripts/probe_grants.py

Everything scripted, nothing touched outside a temp directory. The three
properties that must hold: only cataloged keys are reachable at all (the
deny-by-construction boundary), the config edit is surgical (comments
survive, the result parses, failure restores the original bytes), and the
tools tell the truth about what changed.
"""

import asyncio
import sys
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path

from ciel.config import Config, GrantsConfig
from ciel.brain.tools import grants
from ciel.brain.tools.grants import (
    CAPABILITIES,
    apply_setting,
    describe_grant,
    grant_capability,
    list_capabilities,
    revoke_capability,
)

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


FIXTURE = """\
# Hand-written preamble that must survive every edit.

[shell]
# A comment inside the section survives too.
enabled = false
# commented = "this line must never be mistaken for the key"
# enabled = true  <- nor this one

[proactive]
enabled = true
"""


def text_of(result) -> str:
    return result["content"][0]["text"]


def surgery_checks(tmp: Path) -> None:
    print("config surgery:")
    path = tmp / "config.toml"
    path.write_text(FIXTURE)
    path.chmod(0o600)

    check("replace a live key", apply_setting(path, "shell", "enabled", True))
    after = path.read_text()
    check("the value took", tomllib.loads(after)["shell"]["enabled"] is True)
    check("comments survive", "Hand-written preamble" in after
          and "A comment inside the section" in after)
    check("commented lines were not the target",
          '# commented = "this line' in after and "<- nor this one" in after)
    check("the edit is stamped", "# via grant" in after)
    check("file mode survives", (path.stat().st_mode & 0o777) == 0o600)

    check("insert a missing key",
          apply_setting(path, "proactive", "brief_time", "07:45"))
    check("the inserted key parses",
          tomllib.loads(path.read_text())["proactive"]["brief_time"] == "07:45")

    check("append a missing section", apply_setting(path, "screen", "enabled", True))
    check("the new section parses",
          tomllib.loads(path.read_text())["screen"]["enabled"] is True)

    before = path.read_text()
    check("a value that cannot read back fails",
          not apply_setting(path, "shell", "enabled", float("nan")))
    check("failure restores the original bytes", path.read_text() == before)


async def tool_checks(tmp: Path) -> None:
    print("the tools:")
    path = tmp / "config.toml"
    path.write_text(FIXTURE)
    state_dir = tmp / "state"
    state_dir.mkdir()
    # The tools judge "already on/off" against the RUNNING process config —
    # the file is what the next process reads — so the fixture makes the
    # bound config's proactive live, matching the file.
    config = replace(
        Config(),
        grants=GrantsConfig(enabled=True),
        state_dir=state_dir,
        proactive=replace(Config().proactive, enabled=True),
    )
    grants.bind_config(config, path)

    listing = text_of(await list_capabilities.handler({}))
    check("the listing names every capability",
          all(name in listing for name in CAPABILITIES))
    check("the listing shows current state", "shell: off" in listing)

    check("an uncataloged key is refused",
          "not a grantable" in text_of(
              await grant_capability.handler({"name": "owner_id"})))
    check("the refusal lists what is grantable",
          "shell" in text_of(await grant_capability.handler({"name": "token"})))

    result = text_of(await grant_capability.handler({"name": "shell"}))
    check("a grant edits the config", "shell enabled" in result)
    check("the granted value parses",
          tomllib.loads(path.read_text())["shell"]["enabled"] is True)
    check("the grant trips the reload sentinel", (state_dir / "reload").exists())

    check("a grant of what's on is a no-op",
          "already enabled" in text_of(
              await grant_capability.handler({"name": "proactive"})))

    check("the brief needs a valid time",
          "HH:MM" in text_of(
              await grant_capability.handler({"name": "brief", "value": "25:99"})))
    check("a valid brief time lands",
          "brief set to 07:45" in text_of(
              await grant_capability.handler({"name": "brief", "value": "07:45"})))

    check("a revoke edits immediately",
          "proactive disabled" in text_of(
              await revoke_capability.handler({"name": "proactive"})))
    check("the revoked value parses",
          tomllib.loads(path.read_text())["proactive"]["enabled"] is False)
    check("a revoke of what's off is a no-op",
          "already off" in text_of(
              await revoke_capability.handler({"name": "messages"})))

    check("the confirm question names the power",
          describe_grant({"name": "shell"}) == "Enable shell access")
    check("the brief question names the time",
          describe_grant({"name": "brief", "value": "07:45"})
          == "Set the morning brief to 07:45")

    for forbidden in ("owner_id", "token", "voice", "workspace", "auto_allow"):
        check(f"{forbidden!r} is not in the catalog", forbidden not in CAPABILITIES)


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        surgery_checks(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        await tool_checks(Path(tmp))
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
