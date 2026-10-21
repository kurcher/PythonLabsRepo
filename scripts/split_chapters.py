"""Split whole books (data/gutenberg/*.txt) into chapter-sized documents.

    uv run python scripts/split_chapters.py [--src data/gutenberg] [--out data/chapters]

A book is split on its chapter headings ("CHAPTER XII", "Chapter 3.", "STAVE ONE",
"LETTER IV", ...) when at least 5 are found; otherwise into ~20 KB chunks aligned
to paragraph boundaries. Each piece is written as ``<out>/<book>/<NNN>.txt`` whose
first line is ``<book title> — <heading>``, so ``iter_documents`` sees one chapter
per file.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

HEADING = re.compile(
    r"^[ \t]*((?:CHAPTER|Chapter|STAVE|Stave|LETTER|Letter|BOOK|Book|PART|Part)"
    r"[ \t]+(?:[IVXLCDM]+|\d+|[A-Z][a-z]+|[A-Z]+)\b[^\n]{0,60})$",
    re.M,
)
CHUNK = 20_000
MIN_PIECE = 2_000


def split_by_headings(body: str) -> list[tuple[str, str]]:
    marks = list(HEADING.finditer(body))
    pieces: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        text = body[m.end() : end].strip()
        if len(text) >= MIN_PIECE:  # skips table-of-contents entries
            pieces.append((m.group(1).strip(), text))
    return pieces


def split_by_size(body: str) -> list[tuple[str, str]]:
    pieces: list[tuple[str, str]] = []
    buf: list[str] = []
    size = 0
    for para in re.split(r"\n\s*\n", body):
        buf.append(para)
        size += len(para)
        if size >= CHUNK:
            pieces.append((f"part {len(pieces) + 1}", "\n\n".join(buf)))
            buf, size = [], 0
    if buf:
        pieces.append((f"part {len(pieces) + 1}", "\n\n".join(buf)))
    return pieces


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=Path("data/gutenberg"))
    ap.add_argument("--out", type=Path, default=Path("data/chapters"))
    args = ap.parse_args()

    total = 0
    for book in sorted(args.src.glob("*.txt")):
        title, _, body = book.read_text(encoding="utf-8").partition("\n")
        pieces = split_by_headings(body)
        if len(pieces) < 5:
            pieces = split_by_size(body)
        out_dir = args.out / book.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        for n, (heading, text) in enumerate(pieces, 1):
            (out_dir / f"{n:03d}.txt").write_text(
                f"{title.strip()} — {heading}\n\n{text}\n", encoding="utf-8"
            )
        total += len(pieces)
    print(f"{total} documents written to {args.out}")


if __name__ == "__main__":
    main()
