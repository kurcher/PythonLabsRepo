"""Lazy corpus reading: one document in memory at a time.

Supported layouts under ``root``:

* a directory tree of ``*.txt`` files (one file = one document), found with ``rglob``;
* ``*.jsonl`` files, one JSON object per line with at least a ``"text"`` key
  (optional ``"title"`` / ``"url"``) — this is what ``findex crawl`` writes in Lab 6;
* the same, gzip-compressed (``*.txt.gz``, ``*.jsonl.gz``), read without extracting.

``root`` may also be a single file of one of those kinds.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO, NamedTuple

log = logging.getLogger(__name__)

TEXT_SUFFIXES = (".txt", ".txt.gz")
JSONL_SUFFIXES = (".jsonl", ".jsonl.gz")


class Document(NamedTuple):
    doc_id: int
    path: Path
    text: str
    line: int = 0  # 1-based line number inside a JSON-lines file; 0 for one-file documents

    @property
    def locator(self) -> str:
        """Where to find this document again: ``path`` or ``path#line``."""
        # POSIX separators so an index built on Windows still resolves inside a Linux container
        path = self.path.as_posix()
        return f"{path}#{self.line}" if self.line else path


def _open_text(path: Path) -> IO[str]:
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


def iter_paths(root: Path) -> Iterator[Path]:
    """Yield corpus files under ``root`` in a stable (sorted) order."""
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name.endswith(TEXT_SUFFIXES + JSONL_SUFFIXES):
            yield path


def iter_file(path: Path) -> Iterator[tuple[Path, str, int]]:
    """Yield ``(path, text, line)`` for every document stored in one corpus file.

    A file that is not valid UTF-8 is logged and skipped, never fatal.
    """
    try:
        if path.name.endswith(JSONL_SUFFIXES):
            with _open_text(path) as fh:
                for lineno, line in enumerate(fh, 1):  # files are iterators of lines
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("%s:%d: bad JSON line, skipped", path, lineno)
                        continue
                    text = record.get("text", "")
                    title = record.get("title")
                    yield path, _join_title(title, text), lineno
        else:
            with _open_text(path) as fh:
                yield path, fh.read(), 0
    except (UnicodeDecodeError, OSError) as exc:
        log.warning("skipping %s: %s", path, exc)


def iter_documents(root: Path) -> Iterator[Document]:
    """Stream documents from ``root``; ``doc_id`` is assigned in reading order."""
    doc_id = 0
    for path in iter_paths(root):
        for doc_path, text, line in iter_file(path):
            yield Document(doc_id, doc_path, text, line)
            doc_id += 1


def _join_title(title: str | None, text: str) -> str:
    return f"{title}\n\n{text}" if title else text


def read_document(locator: object) -> str:
    """Re-read one document's text from a ``Document.locator`` string (or a ``DocMeta``)."""
    loc = str(getattr(locator, "path", locator))
    if os.sep == "/":
        loc = loc.replace("\\", "/")  # indexes built on Windows before locators were POSIX
    path_str, _, line = loc.rpartition("#") if "#" in loc else (loc, "", "")
    path = Path(path_str)
    if not line:
        with _open_text(path) as fh:
            return fh.read()
    with _open_text(path) as fh:
        for lineno, raw in enumerate(fh, 1):
            if lineno == int(line):
                record = json.loads(raw)
                return _join_title(record.get("title"), record.get("text", ""))
    raise KeyError(loc)
