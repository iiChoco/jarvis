"""Probe Ciel's own email address — scripted, no network.

    uv run scripts/probe_mail.py

Drives the [mail] config (armed only with both an address and a token),
the send_as_ciel tool through a fake relay (unbound, missing fields, a
name instead of an address, a refused relay, and the From it stamps), the
spoken question the gate asks, and the prompt section's presence — so the
address can't be promised where it can't be used.
"""

import asyncio
import logging
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from ciel.brain.prompt import build_system_prompt
from ciel.brain.toolguard import describe_call
from ciel.brain.tools import mail as tool_module
from ciel.brain.tools.mail import bind_mail, send_as_ciel
from ciel.config import MailConfig, load_config
from ciel.mail import MailUnavailable

CHECKS: list[str] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        sys.exit(1)


def text_of(result) -> str:
    return result["content"][0]["text"]


class FakeSender:
    def __init__(self, fail: str | None = None) -> None:
        self.sent: list[tuple[str, str, str, str]] = []
        self.fail = fail

    def send(self, to, subject, body, sender=""):
        if self.fail:
            raise MailUnavailable(self.fail)
        self.sent.append((to, subject, body, sender))
        return "id"


async def tool_checks() -> None:
    print("the tool:")
    tool_module._sender = None
    tool_module._config = None
    check("unbound, it says so",
          "not set up" in text_of(await send_as_ciel.handler({"to": "a@b.c", "subject": "s", "body": "b"})))
    sender = FakeSender()
    config = MailConfig(enabled=True, address="ciel@example.com", owner="me@gmail.com", token="t")
    bind_mail(sender, config)
    check("every field is required",
          "all needed" in text_of(await send_as_ciel.handler({"to": "a@b.c", "subject": "s"})))
    check("a name is not an address",
          "not an email address" in text_of(await send_as_ciel.handler({"to": "Sarah", "subject": "s", "body": "b"})))
    out = text_of(await send_as_ciel.handler({"to": "me@gmail.com", "subject": "The list", "body": "Eggs."}))
    check("a send goes out from Ciel's address",
          sender.sent == [("me@gmail.com", "The list", "Eggs.", "ciel@example.com")]
          and out.startswith("Sent to me@gmail.com from ciel@example.com"))
    bind_mail(FakeSender(fail="relay down"), config)
    check("a refused relay is reported, not raised",
          text_of(await send_as_ciel.handler({"to": "a@b.c", "subject": "s", "body": "b"})) == "Not sent: relay down")


def gate_and_prompt_checks() -> None:
    print("the gate and the prompt:")
    check("the spoken question names Ciel's address, the recipient, and the subject",
          describe_call("mcp__ciel__send_as_ciel", {"to": "me@gmail.com", "subject": "The list"})
          == "Send an email from my own address to me@gmail.com with the subject The list — okay?")
    with_mail = build_system_prompt(mail_address="ciel@example.com", mail_owner="me@gmail.com")
    check("armed, the prompt teaches the address and where 'email me' goes",
          "ciel@example.com" in with_mail and "me@gmail.com" in with_mail
          and "send_as_ciel" in with_mail)
    check("a copy address is taught too",
          "me@berkeley.edu" in build_system_prompt(mail_address="a@b.c", mail_copy_to="me@berkeley.edu")
          and "copy of everything" not in with_mail)
    check("unarmed, the prompt never mentions it",
          "send_as_ciel" not in build_system_prompt())


def config_checks(tmp: Path) -> None:
    print("config:")
    fresh = MailConfig()
    check("off and unarmed by default, Cloudflare's relay preset",
          not fresh.enabled and not fresh.armed and fresh.smtp_host == "smtp.mx.cloudflare.net"
          and fresh.smtp_port == 465 and fresh.smtp_user == "api_token")
    check("enabled without an address or token is not armed",
          not MailConfig(enabled=True, token="t").armed
          and not MailConfig(enabled=True, address="a@b.c").armed
          and MailConfig(enabled=True, address="a@b.c", token="t").armed)
    toml = tmp / "config.toml"
    toml.write_text('[mail]\nenabled = true\naddress = "ciel@example.com"\nowner = "me@gmail.com"\ntoken = "t"\n')
    loaded = load_config(toml).mail
    check("the TOML section loads and arms",
          loaded.armed and loaded.address == "ciel@example.com" and loaded.owner == "me@gmail.com")


async def main() -> int:
    await tool_checks()
    gate_and_prompt_checks()
    with tempfile.TemporaryDirectory() as tmp:
        config_checks(Path(tmp))
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
