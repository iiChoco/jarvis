"""Projects — durable named workspaces for work that spans conversations.

Codename: **Atlas** — each project file a chart of some territory of ongoing
work, the index the map that says which chart to open.

Sessions are deliberately short-lived (see ``brain.resume_window_minutes``),
so anything that outlives a conversation needs a durable home. Memory holds
*facts*; this holds *working state*: where an effort stands, the numbers and
paths that matter, what's next. The model opens a project before working on
it and updates it when reality moves, which is what makes "let's get back to
the wake word project" work three days and thirty sessions later.

One Markdown file per project, same reasoning as memory: inspectable,
editable, greppable. Two sections with different physics — ``## State`` is
the complete current picture, *replaced* on every update so it can't
accumulate rot; ``## Log`` is append-only dated history, cheap to grow,
returned only as a tail so it can't flood a context. Timestamps are stored
machine-readable (UTC ISO-8601) and prettified only at render time; a stored
"2 hours ago" is a lie by dinnertime.

Identity comes from the *name*, not the description: the user says "the wake
word project" and the model must land on the same file every time. The index
in the system prompt shows the exact names to use.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ciel.memory.store import atomic_write, slugify

log = logging.getLogger(__name__)

VALID_STATUSES = ("active", "paused", "done")

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_SECTIONS = re.compile(r"^## (State|Log)\s*$", re.MULTILINE)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(ts: float) -> str:
    """Render a timestamp relative, for the index only — never stored."""
    delta = max(0.0, time.time() - ts)
    if delta < 90 * 60:
        return f"{max(1, int(delta // 60))} minutes ago"
    if delta < 36 * 3600:
        return f"{int(delta // 3600)} hours ago"
    return f"{int(delta // 86400)} days ago"


@dataclass(frozen=True, slots=True)
class Project:
    name: str
    description: str
    status: str
    state: str
    log: tuple[str, ...]  # newest last; possibly trimmed to a tail
    created_at: float
    updated_at: float
    path: Path


class ProjectStore:
    """File-backed projects: replace-state, append-log."""

    def __init__(
        self,
        directory: Path,
        max_index_entries: int = 30,
        max_state_chars: int = 4000,
        log_tail: int = 15,
    ) -> None:
        self._dir = directory.expanduser()
        self._max_index = max_index_entries
        self._max_state = max_state_chars
        self._log_tail = log_tail

    @property
    def directory(self) -> Path:
        return self._dir

    # ── reading ──────────────────────────────────────────────────────────────

    def all(self) -> list[Project]:
        """Every project, most recently updated first. Full logs."""
        if not self._dir.exists():
            return []
        found = []
        for path in self._dir.glob("*.md"):
            parsed = self._read(path)
            if parsed is not None:
                found.append(parsed)
        return sorted(found, key=lambda p: p.updated_at, reverse=True)

    def get(self, name: str) -> Project | None:
        """One project by name, its log trimmed to the configured tail."""
        path = self._dir / f"{slugify(name)}.md"
        project = self._read(path) if path.exists() else None
        if project is None:
            return None
        return Project(
            name=project.name,
            description=project.description,
            status=project.status,
            state=project.state,
            log=project.log[-self._log_tail:],
            created_at=project.created_at,
            updated_at=project.updated_at,
            path=project.path,
        )

    # ── writing ──────────────────────────────────────────────────────────────

    def write(
        self,
        name: str,
        state: str,
        description: str | None = None,
        status: str | None = None,
    ) -> Project:
        """Create a project or replace its state.

        Raises ``ValueError`` past the state budget rather than truncating:
        the refusal reaches the model as a tool error and teaches it to
        condense; a silent truncation loses whatever was written last and
        teaches nothing.
        """
        state = state.strip()
        if len(state) > self._max_state:
            raise ValueError(
                f"State is {len(state)} characters; the limit is "
                f"{self._max_state}. Condense it — state should be the "
                "current picture, not the history (the log holds history)."
            )

        slug = slugify(name)
        path = self._dir / f"{slug}.md"
        existing = self._read(path) if path.exists() else None
        if existing is None and not description:
            description = name.strip()
        if status is not None and status not in VALID_STATUSES:
            status = "active"

        now = time.time()
        project = Project(
            name=slug,
            description=(description or (existing.description if existing else name)).strip(),
            status=status or (existing.status if existing else "active"),
            state=state,
            log=existing.log if existing else (),
            created_at=existing.created_at if existing else now,
            updated_at=now,
            path=path,
        )
        self._save(project)
        log.info("%s project: %s", "updated" if existing else "created", slug)
        return project

    def append_log(self, name: str, entry: str) -> bool:
        """Add one timestamped line to a project's log. False if no project."""
        path = self._dir / f"{slugify(name)}.md"
        existing = self._read(path) if path.exists() else None
        if existing is None:
            return False
        stamped = f"{_iso(time.time())}: {entry.strip()}"
        updated = Project(
            name=existing.name,
            description=existing.description,
            status=existing.status,
            state=existing.state,
            log=(*existing.log, stamped),
            created_at=existing.created_at,
            updated_at=time.time(),
            path=path,
        )
        self._save(updated)
        return True

    # ── the index ────────────────────────────────────────────────────────────

    def index_prompt(self) -> str | None:
        """The one-line-per-project map for the system prompt."""
        projects = self.all()
        if not projects:
            return None
        lines = [
            "# Projects",
            "",
            "Ongoing work you keep durable state for. Only these one-line",
            "summaries are in your prompt — open a project with open_project",
            "(exact name below) before doing any work on it.",
            "",
        ]
        for project in projects[: self._max_index]:
            lines.append(
                f"- {project.name} ({project.status}): {project.description}"
                f" — updated {_ago(project.updated_at)}"
            )
        hidden = len(projects) - self._max_index
        if hidden > 0:
            lines.append(f"- ({hidden} older projects not listed; close out done ones)")
        return "\n".join(lines)

    # ── internals ────────────────────────────────────────────────────────────

    def _save(self, project: Project) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        log_lines = "\n".join(f"- {line}" for line in project.log)
        text = (
            "---\n"
            f"name: {project.name}\n"
            f"description: {project.description}\n"
            f"status: {project.status}\n"
            f"created_at: {_iso(project.created_at)}\n"
            f"updated_at: {_iso(project.updated_at)}\n"
            "---\n\n"
            "## State\n\n"
            f"{project.state}\n\n"
            "## Log\n\n"
            f"{log_lines}\n"
        )
        atomic_write(project.path, text)

    def _read(self, path: Path) -> Project | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            log.warning("could not read project %s", path)
            return None
        match = _FRONTMATTER.match(raw)
        if match is None:
            return None

        meta: dict[str, str] = {}
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()

        body = match.group(2)
        state, log_lines = "", []
        parts = _SECTIONS.split(body)
        # parts: [preamble, "State", state text, "Log", log text] in any order
        for header, content in zip(parts[1::2], parts[2::2]):
            if header == "State":
                state = content.strip()
            elif header == "Log":
                log_lines = [
                    line[2:].strip()
                    for line in content.strip().splitlines()
                    if line.startswith("- ")
                ]

        def parse_ts(value: str) -> float:
            try:
                return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                return path.stat().st_mtime

        return Project(
            name=meta.get("name", path.stem),
            description=meta.get("description", ""),
            status=meta.get("status", "active"),
            state=state,
            log=tuple(log_lines),
            created_at=parse_ts(meta.get("created_at", "")),
            updated_at=parse_ts(meta.get("updated_at", "")),
            path=path,
        )


__all__ = ["ProjectStore", "Project", "VALID_STATUSES"]
