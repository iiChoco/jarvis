"""Where the room keeps what happened: one directory per session.

    <dir>/users/<username>/sessions/<session_id>/
        meta.json          mode, timestamps, state, cost — the lobby's row
        brief.json         the company, the case, or the technical brief
        transcript.jsonl   {t, speaker, text}, appended as it is said
        code.jsonl         {t, lang, code, event}, technical mode only
        recording.webm     the browser's chunks, appended in order
        debrief.json       the structured scorecard
        debrief.md         the same, readable

Flat files, one thing each, the house style: a friend's interview can be
inspected with ``cat``, handed to them as a folder, or deleted with
``rm -r``. Metadata is written atomically; the record files are
append-only, so a crash mid-interview loses at most the line in flight.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Iterator

from ciel.memory.store import atomic_write

log = logging.getLogger(__name__)

SESSION_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
STATES = ("prepared", "live", "ended", "debriefed")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class SessionStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    # ── paths ────────────────────────────────────────────────────────────────

    def user_dir(self, username: str) -> Path:
        return self._root / "users" / username / "sessions"

    def path(self, username: str, session_id: str) -> Path:
        if not SESSION_ID.match(session_id):
            raise KeyError(session_id)
        return self.user_dir(username) / session_id

    def exists(self, username: str, session_id: str) -> bool:
        try:
            return (self.path(username, session_id) / "meta.json").is_file()
        except KeyError:
            return False

    # ── create / meta ────────────────────────────────────────────────────────

    def create(self, username: str, mode: str, setup: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
        while True:
            session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
            path = self.user_dir(username) / session_id
            if not path.exists():
                break
        path.mkdir(parents=True)
        meta = {
            "id": session_id,
            "username": username,
            "mode": mode,
            "title": brief_title(mode, brief),
            "setup": setup,
            "created": _now_iso(),
            "created_ts": time.time(),
            "started": None,
            "ended": None,
            "duration_s": 0,
            "question_count": 0,
            "state": "prepared",
            "cost_usd": 0.0,
            "tts": None,
            "case_slug": brief.get("slug") if mode == "case" else None,
        }
        self.save_meta(username, session_id, meta)
        self.save_brief(username, session_id, brief)
        return meta

    def load_meta(self, username: str, session_id: str) -> dict[str, Any]:
        path = self.path(username, session_id) / "meta.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(session_id) from None
        if not isinstance(data, dict):
            raise KeyError(session_id)
        return data

    def save_meta(self, username: str, session_id: str, meta: dict[str, Any]) -> None:
        atomic_write(self.path(username, session_id) / "meta.json", json.dumps(meta, indent=2) + "\n")

    def update_meta(self, username: str, session_id: str, **fields: Any) -> dict[str, Any]:
        meta = self.load_meta(username, session_id)
        meta.update(fields)
        self.save_meta(username, session_id, meta)
        return meta

    def list(self, username: str) -> list[dict[str, Any]]:
        base = self.user_dir(username)
        if not base.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for child in base.iterdir():
            if not SESSION_ID.match(child.name):
                continue
            try:
                meta = json.loads((child / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict):
                meta["debriefed"] = (child / "debrief.json").is_file()
                meta["has_recording"] = (child / "recording.webm").is_file()
                rows.append(meta)
        # Two sessions in one second tie on the ISO stamp; the float breaks it.
        rows.sort(key=lambda m: (m.get("created", ""), float(m.get("created_ts") or 0)), reverse=True)
        return rows

    def count_since(self, username: str, since_iso: str) -> int:
        return sum(1 for m in self.list(username) if m.get("created", "") >= since_iso)

    def delete(self, username: str, session_id: str) -> None:
        path = self.path(username, session_id)
        if not path.is_dir():
            raise KeyError(session_id)
        shutil.rmtree(path)

    # ── brief ────────────────────────────────────────────────────────────────

    def save_brief(self, username: str, session_id: str, brief: dict[str, Any]) -> None:
        atomic_write(self.path(username, session_id) / "brief.json", json.dumps(brief, indent=2) + "\n")

    def load_brief(self, username: str, session_id: str) -> dict[str, Any]:
        path = self.path(username, session_id) / "brief.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(session_id) from None
        return data if isinstance(data, dict) else {}

    # ── append-only records ──────────────────────────────────────────────────

    def _append(self, username: str, session_id: str, name: str, row: dict[str, Any]) -> None:
        path = self.path(username, session_id) / name
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _rows(self, username: str, session_id: str, name: str) -> list[dict[str, Any]]:
        path = self.path(username, session_id) / name
        rows: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except FileNotFoundError:
            pass
        return rows

    def append_transcript(self, username: str, session_id: str, t: float, speaker: str, text: str) -> None:
        self._append(username, session_id, "transcript.jsonl",
                     {"t": round(t, 2), "speaker": speaker, "text": text})

    def transcript(self, username: str, session_id: str) -> list[dict[str, Any]]:
        return self._rows(username, session_id, "transcript.jsonl")

    def append_code(self, username: str, session_id: str, t: float, lang: str, code: str, event: str) -> None:
        self._append(username, session_id, "code.jsonl",
                     {"t": round(t, 2), "lang": lang, "code": code, "event": event})

    def code(self, username: str, session_id: str) -> list[dict[str, Any]]:
        return self._rows(username, session_id, "code.jsonl")

    # ── recording ────────────────────────────────────────────────────────────

    def recording_path(self, username: str, session_id: str) -> Path:
        return self.path(username, session_id) / "recording.webm"

    def append_recording(self, username: str, session_id: str, data: bytes) -> int:
        path = self.recording_path(username, session_id)
        with path.open("ab") as fh:
            fh.write(data)
        return path.stat().st_size

    # ── debrief ──────────────────────────────────────────────────────────────

    def write_debrief(self, username: str, session_id: str, debrief: dict[str, Any], markdown: str) -> None:
        base = self.path(username, session_id)
        atomic_write(base / "debrief.json", json.dumps(debrief, indent=2, ensure_ascii=False) + "\n")
        atomic_write(base / "debrief.md", markdown)

    def debrief(self, username: str, session_id: str) -> tuple[dict[str, Any] | None, str | None]:
        base = self.path(username, session_id)
        try:
            data = json.loads((base / "debrief.json").read_text(encoding="utf-8"))
            md = (base / "debrief.md").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None, None
        return (data if isinstance(data, dict) else None), md

    # ── usage (admin) ────────────────────────────────────────────────────────

    def usernames(self) -> Iterator[str]:
        base = self._root / "users"
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if child.is_dir():
                    yield child.name


def brief_title(mode: str, brief: dict[str, Any]) -> str:
    if mode == "case":
        return str(brief.get("title") or brief.get("client") or "Case")
    company = brief.get("company") if isinstance(brief.get("company"), dict) else brief
    name = str(company.get("name") or "Interview")
    role = brief.get("role") if isinstance(brief.get("role"), dict) else {}
    title = str(role.get("title") or "")
    return f"{name} · {title}" if title else name


__all__ = ["SESSION_ID", "STATES", "SessionStore", "brief_title"]
