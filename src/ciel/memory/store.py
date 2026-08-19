"""Durable memory — facts that outlive the conversation they were learned in.

Codename: **Invariant** — what survives every session transformation.

This is the deeper half of memory. Session resume (see ``brain/session.py``)
picks up a thread you were already on; this is what lets Ciel know your name
three weeks and forty conversations later, after the session that learned it
has been compacted into nothing.

Storage is one Markdown file per fact, which is deliberate. It is trivially
inspectable, editable, greppable, and diffable — you can open the directory and
see exactly what Ciel believes about you, and delete anything you'd rather it
didn't. A database would be tidier and much worse for that.

The index is *derived* from the files rather than stored alongside them. A
stored index is a second source of truth that drifts the moment anything writes
a file without updating it; at personal scale, reading a few hundred small
files is far cheaper than the class of bug that avoids.
"""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

log = logging.getLogger(__name__)

MemoryKind = Literal[
    "identity", "preference", "project", "fact", "reference", "procedure"
]
VALID_KINDS: frozenset[str] = frozenset(
    ("identity", "preference", "project", "fact", "reference", "procedure")
)
"""``procedure`` is the who-vs-how split: every other kind says who the user
is or what is true; a procedure says how a class of task should be done for
this user ("how the morning brief is structured", "how to word a message to
Sam"). Named for the task class and updated in place as the method improves —
the overwrite-by-description behavior of ``write`` is what keeps one
procedure per task instead of a pile of one-session variants."""

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_WORD = re.compile(r"[a-z0-9]+")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# Words too common to be worth matching on — they'd make every memory look
# equally relevant to every query.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "to", "of", "in", "on", "at", "for", "with", "my", "me", "i",
    "you", "your", "it", "its", "this", "that", "what", "who", "do", "does",
    "did", "have", "has", "had", "about", "from", "as", "so", "if", "then",
})


@dataclass(frozen=True, slots=True)
class Memory:
    name: str
    description: str
    kind: str
    content: str
    created_at: float
    path: Path
    context: str = "conversation"
    """Write provenance: what kind of turn saved this. "conversation" means a
    user was part of it (live turns, and reflection distilling a conversation
    the user just had); "proactive" means an unattended turn observed it with
    nobody around. The distinction is epistemics, not bookkeeping — a fact no
    user ever heard or confirmed deserves a hedge when it is spoken back, and
    recall renders exactly that hedge."""

    def to_markdown(self) -> str:
        # The description sits on one frontmatter line, so any newline in it
        # would spill into the block and be reparsed as another `key: value` —
        # a model-supplied "foo\nkind: identity" would spoof the kind. Collapse
        # whitespace to keep the frontmatter a flat, unforgeable header.
        description = " ".join(self.description.split())
        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {description}\n"
            f"kind: {self.kind}\n"
            f"created_at: {self.created_at:.0f}\n"
            f"context: {self.context}\n"
            "---\n\n"
            f"{self.content.strip()}\n"
        )


def atomic_write(path: Path, text: str) -> None:
    """Write via temp-file-and-replace, so a crash mid-write can't leave a
    half-written file. These files are the truth their stores read back;
    "mostly written" is worse than "previous version". Same pattern as the
    session store, shared here for every file-backed store to use."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def slugify(text: str, *, max_length: int = 60) -> str:
    """Turn a description into a stable, filesystem-safe name."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = _SLUG_STRIP.sub("-", normalized.lower()).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rsplit("-", 1)[0]
    return slug or "memory"


# Two tokens match if one is a prefix of the other and they share at least this
# many characters. Without it, "what is my name" fails against a memory that
# says "named" — a miss that reads as broken recall but is really morphology.
#
# Prefix matching rather than suffix stripping, because stripping has to agree
# on a single canonical form: "name" and "named" reduce to "name" and "nam" and
# still miss each other. Prefixes sidestep that entirely, and cover the
# plural/gerund/past-tense cases that actually come up.
_MIN_PREFIX = 4


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1}


def _overlap(query: set[str], document: set[str]) -> int:
    """Count query tokens with a prefix match somewhere in the document."""
    hits = 0
    for term in query:
        for candidate in document:
            if term == candidate:
                hits += 1
                break
            shorter, longer = (
                (term, candidate) if len(term) <= len(candidate) else (candidate, term)
            )
            if len(shorter) >= _MIN_PREFIX and longer.startswith(shorter):
                hits += 1
                break
    return hits


class MemoryStore:
    """File-backed memories, indexed by keyword."""

    def __init__(self, directory: Path, max_index_entries: int = 200) -> None:
        self._dir = directory
        self._max_index = max_index_entries

    @property
    def directory(self) -> Path:
        return self._dir

    # ── reading ──────────────────────────────────────────────────────────────

    def all(self) -> list[Memory]:
        """Every memory, newest first."""
        if not self._dir.exists():
            return []
        found: list[Memory] = []
        for path in self._dir.glob("*.md"):
            if path.name == "MEMORY.md":
                continue  # the human-readable index, not a memory
            parsed = self._read(path)
            if parsed is not None:
                found.append(parsed)
        return sorted(found, key=lambda m: m.created_at, reverse=True)

    def get(self, name: str) -> Memory | None:
        return self._resolve(name)

    def _resolve(self, name: str) -> Memory | None:
        """Find a memory by name, tolerating hand-authored files.

        The fast path is the slug filename ``write`` uses. But a file the user
        created by hand may be named anything, with its real name in the
        frontmatter, so a miss falls back to scanning for a memory whose name
        (or filename stem) matches — otherwise ``get``/``forget`` can't touch
        exactly the memories the module invites the user to hand-edit.
        """
        direct = self._dir / f"{slugify(name)}.md"
        if direct.exists():
            return self._read(direct)
        target = slugify(name)
        for memory in self.all():
            if slugify(memory.name) == target or memory.path.stem == name:
                return memory
        return None

    def _read(self, path: Path) -> Memory | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            log.warning("could not read memory %s", path)
            return None

        match = _FRONTMATTER.match(raw)
        if match is None:
            # A hand-written file without frontmatter is still a memory; treat
            # the filename as its name and the first line as its description
            # rather than discarding what the user wrote.
            body = raw.strip()
            first = body.splitlines()[0] if body else ""
            return Memory(
                name=path.stem,
                description=first[:120],
                kind="fact",
                content=body,
                created_at=path.stat().st_mtime,
                path=path,
            )

        meta: dict[str, str] = {}
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            if _:
                meta[key.strip()] = value.strip()

        try:
            created = float(meta.get("created_at", 0)) or path.stat().st_mtime
        except ValueError:
            created = path.stat().st_mtime

        return Memory(
            name=meta.get("name", path.stem),
            description=meta.get("description", ""),
            kind=meta.get("kind", "fact"),
            content=match.group(2).strip(),
            created_at=created,
            path=path,
            # Files predating the field (and hand-written ones) read as
            # conversation-provenance: the user made or saw them.
            context=meta.get("context", "conversation"),
        )

    # ── writing ──────────────────────────────────────────────────────────────

    def write(
        self,
        description: str,
        content: str,
        kind: str = "fact",
        context: str = "conversation",
    ) -> Memory:
        """Create or replace a memory.

        Named from its description, so saving a refined version of something
        Ciel already knows overwrites it instead of accumulating near-duplicates
        that later contradict each other. ``context`` is the write provenance
        (see the field on :class:`Memory`); a rewrite keeps the original
        created_at but takes the new context — the latest write is the claim
        being made now.
        """
        if kind not in VALID_KINDS:
            kind = "fact"

        name = slugify(description)
        path = self._dir / f"{name}.md"
        existing = self._read(path) if path.exists() else None

        memory = Memory(
            name=name,
            description=description.strip(),
            kind=kind,
            content=content.strip(),
            created_at=existing.created_at if existing else time.time(),
            path=path,
            context=context,
        )

        self._dir.mkdir(parents=True, exist_ok=True)
        atomic_write(path, memory.to_markdown())
        self._write_human_index()
        log.info("%s memory: %s", "updated" if existing else "saved", name)
        return memory

    def forget(self, name: str) -> bool:
        memory = self._resolve(name)
        if memory is None:
            return False
        memory.path.unlink(missing_ok=True)
        self._write_human_index()
        log.info("forgot memory: %s", memory.name)
        return True

    # ── search ───────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 5) -> list[Memory]:
        """Rank memories by keyword overlap.

        Deliberately not embeddings. At personal scale a few hundred one-line
        descriptions match fine on keywords, and this avoids a vector store, a
        model download, and an indexing pipeline. If recall accuracy ever
        disappoints, this method is the only thing that has to change.

        The known limitation is purely semantic queries: "who am I" shares no
        content word with "User is named Choco" and will not match. That is
        survivable because it is not how the tool is used — the summaries are
        already in the system prompt, so Ciel answers that question directly
        and calls recall only to load a specific memory's full text, with a
        query it formulates itself ("user name", not "who am I").
        """
        wanted = _tokens(query)
        if not wanted:
            return []

        scored: list[tuple[float, Memory]] = []
        for memory in self.all():
            # Description and name are weighted above body text: they are the
            # curated summary, so a hit there is a stronger signal than a
            # passing mention buried in the content.
            score = (
                3.0 * _overlap(wanted, _tokens(memory.description))
                + 2.0 * _overlap(wanted, _tokens(memory.name))
                + 1.0 * _overlap(wanted, _tokens(memory.content))
            )
            if score > 0:
                scored.append((score, memory))

        scored.sort(key=lambda pair: (-pair[0], -pair[1].created_at))
        return [memory for _score, memory in scored[:limit]]

    # ── the index that goes into the system prompt ───────────────────────────

    def index_prompt(self) -> str | None:
        """One line per memory, for injection into the system prompt.

        This is the trick that makes memory scale. Ciel always knows *what* it
        knows — a few hundred tokens — and pays to load full contents only when
        a specific memory turns out to matter. Injecting everything would put
        the entire store into every single turn's prompt.
        """
        memories = self.all()
        if not memories:
            return None

        shown = memories[: self._max_index]
        lines = [f"- {m.name}: {m.description}" for m in shown]

        note = ""
        if len(memories) > len(shown):
            note = (
                f"\n({len(memories) - len(shown)} older memories not listed; "
                "recall can still find them.)"
            )

        return (
            "# What you remember about this user\n\n"
            "These are things you have saved from previous conversations. Only "
            "the summaries are listed here — use the recall tool to read the "
            "full content of one when it turns out to matter.\n\n"
            + "\n".join(lines)
            + note
        )

    def _write_human_index(self) -> None:
        """Maintain MEMORY.md so the directory is browsable by a person.

        Purely a convenience artifact — nothing reads it back, so it can never
        drift out of sync with the real data in a way that matters.
        """
        memories = self.all()
        if not memories:
            (self._dir / "MEMORY.md").unlink(missing_ok=True)
            return

        by_kind: dict[str, list[Memory]] = {}
        for memory in memories:
            by_kind.setdefault(memory.kind, []).append(memory)

        lines = ["# Ciel's memory", "", f"{len(memories)} memories.", ""]
        for kind in sorted(by_kind):
            lines.append(f"## {kind}")
            lines.append("")
            for memory in by_kind[kind]:
                lines.append(f"- [{memory.description}]({memory.path.name})")
            lines.append("")

        atomic_write(self._dir / "MEMORY.md", "\n".join(lines))


__all__ = ["MemoryStore", "Memory", "MemoryKind", "VALID_KINDS", "slugify", "atomic_write"]
