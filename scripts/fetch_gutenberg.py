"""Download the Project Gutenberg "Top 100" books into data/gutenberg/.

Politeness: Project Gutenberg asks robots to use a mirror, not www.gutenberg.org,
so book files come from an official mirror (csclub.uwaterloo.ca),
one request at a time, with a delay.
Only the top-list page (one request) is read from the main site.

    uv run python scripts/fetch_gutenberg.py [--limit 120] [--out data/gutenberg]
"""

from __future__ import annotations

import argparse
import re
import time
import urllib.request
from pathlib import Path

TOP_URL = "https://www.gutenberg.org/browse/scores/top"
MIRROR = "https://mirror.csclub.uwaterloo.ca/gutenberg"
UA = "findex-course-project/0.1 (student search-engine lab; +mailto:kurch2950@gmail.com)"

START = re.compile(r"\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\n", re.I)
END = re.compile(r"\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG EBOOK", re.I)


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def mirror_dir(book_id: int) -> str:
    digits = str(book_id)
    prefix = "/".join(digits[:-1]) if len(digits) > 1 else "0"
    return f"{MIRROR}/{prefix}/{book_id}/"


def strip_boilerplate(text: str) -> str:
    if m := START.search(text):
        text = text[m.end() :]
    if m := END.search(text):
        text = text[: m.start()]
    return text.strip() + "\n"


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--out", type=Path, default=Path("data/gutenberg"))
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    html = get(TOP_URL).decode("utf-8")
    books: dict[int, str] = {}
    for m in re.finditer(r'href="/ebooks/(\d+)">([^<]+)</a>', html):
        books.setdefault(int(m.group(1)), m.group(2))
    print(f"{len(books)} books on the top list")

    for n, (book_id, title) in enumerate(list(books.items())[: args.limit], 1):
        title = re.sub(r"\s*\(\d+\)$", "", title)
        target = args.out / f"{book_id:05d}-{slug(title)}.txt"
        if target.exists():
            continue
        try:
            listing = get(mirror_dir(book_id)).decode("utf-8", "replace")
            names = re.findall(r'href="(\d+(?:-0|-8)?\.txt)"', listing)
            if not names:
                print(f"[skip] {book_id}: no plain-text file")
                continue
            name = min(names, key=lambda s: ("-0" not in s, s))  # prefer UTF-8 "-0"
            raw = get(mirror_dir(book_id) + name)
            enc = "utf-8" if name.endswith("-0.txt") else "latin-1"
            text = strip_boilerplate(raw.decode(enc, "replace"))
            target.write_text(f"{title}\n\n{text}", encoding="utf-8")
            print(f"[{n:3d}] {target.name} ({len(text) // 1024} KiB)")
        except Exception as exc:  # network flakiness: log and move on
            print(f"[fail] {book_id}: {exc}")
        time.sleep(args.delay)


if __name__ == "__main__":
    main()
