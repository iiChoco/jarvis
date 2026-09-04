"""The case library: consulting cases as files, one each.

A case is a JSON document in the shape ``prompt.CASE_SCHEMA`` describes —
the prompt, the background the candidate has to ask for, the
clarifications, the exhibits with their reveal conditions, the framework
hints, a good answer, and the historical outcome the debrief compares
against. A few ship with the code under ``interview/cases/``; the owner
adds more under ``<interview dir>/cases/`` (``ciel interview seed-cases``
asks the model to write them). A candidate is never handed a case they
have already had, when there is a choice.
"""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

BUNDLED = Path(__file__).with_name("cases")
TYPES = ("product", "project", "company")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")


def validate(case: Any) -> list[str]:
    """Why a case is not one — empty when it is."""
    problems: list[str] = []
    if not isinstance(case, dict):
        return ["not an object"]
    for key in ("slug", "title", "type", "client", "prompt", "historical_outcome"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            problems.append(f"{key} missing")
    if isinstance(case.get("slug"), str) and not _SLUG.match(case["slug"]):
        problems.append("slug must be lowercase letters, digits, dashes")
    if case.get("type") not in TYPES:
        problems.append(f"type must be one of {', '.join(TYPES)}")
    exhibits = case.get("exhibits")
    if not isinstance(exhibits, list):
        problems.append("exhibits must be a list")
    else:
        seen: set[str] = set()
        for i, e in enumerate(exhibits):
            if not isinstance(e, dict) or not isinstance(e.get("id"), str):
                problems.append(f"exhibit {i} needs an id")
                continue
            if e["id"] in seen:
                problems.append(f"exhibit id {e['id']} repeated")
            seen.add(e["id"])
            if e.get("kind", "table") == "table":
                cols = e.get("columns")
                rows = e.get("rows")
                if not isinstance(cols, list) or not cols:
                    problems.append(f"exhibit {e['id']} needs columns")
                elif not isinstance(rows, list) or any(
                    not isinstance(r, list) or len(r) != len(cols) for r in rows
                ):
                    problems.append(f"exhibit {e['id']} rows must match its columns")
    return problems


class CaseLibrary:
    def __init__(self, user_dir: Path | None = None) -> None:
        self._dirs = [BUNDLED] + ([user_dir] if user_dir is not None else [])

    def _load_dir(self, base: Path) -> Iterable[dict[str, Any]]:
        if not base.is_dir():
            return
        for path in sorted(base.glob("*.json")):
            try:
                case = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("case %s unreadable (%s)", path.name, exc)
                continue
            why = validate(case)
            if why:
                log.warning("case %s skipped: %s", path.name, "; ".join(why))
                continue
            yield case

    def all(self) -> list[dict[str, Any]]:
        by_slug: dict[str, dict[str, Any]] = {}
        for base in self._dirs:
            for case in self._load_dir(base):
                by_slug[case["slug"]] = case  # a user's file overrides a bundled one
        return list(by_slug.values())

    def pick(self, case_type: str = "any", exclude: set[str] | None = None) -> dict[str, Any] | None:
        pool = [c for c in self.all() if case_type in ("any", "", None) or c["type"] == case_type]
        if not pool:
            return None
        fresh = [c for c in pool if not exclude or c["slug"] not in exclude]
        return random.choice(fresh or pool)

    def save(self, base: Path, case: dict[str, Any]) -> Path:
        why = validate(case)
        if why:
            raise ValueError("; ".join(why))
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{case['slug']}.json"
        path.write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path


__all__ = ["BUNDLED", "CaseLibrary", "TYPES", "validate"]
