"""Conversation transcripts — a local record of everything said.

One CSV file per conversation rather than one ever-growing log: a conversation
is the natural unit to read back, grep, or delete, and per-file layout means
removing one leaves the rest untouched. Files are named for when the
conversation started.

The file is created lazily on the first row, so a session where nothing was
said leaves nothing behind. CSV because it opens everywhere — a spreadsheet,
pandas, `column -s, -t` — and the csv module's quoting handles commas and
newlines inside utterances correctly.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


class Transcript:
    """Appends rows of (timestamp, speaker, text) to this conversation's CSV."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._file: IO[str] | None = None
        self._writer: "csv._writer | None" = None
        self._failed = False
        self.path: Path | None = None

    def record(self, speaker: str, text: str) -> None:
        """Add one utterance.

        Never raises: a transcript that can't be written costs the record,
        never the conversation it was recording.
        """
        if self._failed:
            return
        try:
            if self._writer is None:
                self._open()
            assert self._writer is not None and self._file is not None
            self._writer.writerow(
                [datetime.now().isoformat(timespec="seconds"), speaker, text]
            )
            self._file.flush()
        except OSError:
            log.warning(
                "could not write the transcript in %s — giving up for this conversation",
                self._dir, exc_info=True,
            )
            self._failed = True

    def _open(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._dir / f"conversation-{stamp}.csv"
        # A second conversation starting within the same second gets a suffix
        # rather than clobbering the first.
        n = 1
        while path.exists():
            n += 1
            path = self._dir / f"conversation-{stamp}-{n}.csv"

        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["timestamp", "speaker", "text"])
        self.path = path
        log.debug("transcript: %s", path)

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
            self._writer = None


__all__ = ["Transcript"]
