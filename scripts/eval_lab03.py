"""precision@5 for TF-IDF vs BM25 on 10 hand-labeled queries.

    uv run python scripts/eval_lab03.py build/index-pos.bin

Relevance labels are per query: a document is relevant if its title matches one of
the patterns (the book(s) a reader asking that question wants). Labels were decided
before looking at either scorer's output.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from findex import store
from findex.scoring import BM25, TfIdf
from findex.search import search

QUERIES: dict[str, list[str]] = {
    "white whale": [r"Moby Dick"],
    "ghost of christmas past": [r"Christmas Carol"],
    "vampire blood count castle": [r"Dracula", r"Carmilla"],
    "sherlock holmes": [r"Sherlock Holmes", r"Study in Scarlet", r"Hound of the Baskervilles"],
    "elizabeth darcy pemberley": [r"Pride and Prejudice"],
    "natural selection species": [r"Origin of Species"],
    "rabbit hole": [r"Alice's Adventures", r"Alice’s Adventures"],
    "monster creator": [r"Frankenstein"],
    "treasure map pirates": [r"Treasure Island"],
    "war strategy enemy general": [r"Art of War", r"On War"],
}


def precision_at_5(index, query: str, patterns: list[str], scorer) -> tuple[float, list[str]]:
    results = search(index, query, scorer, k=5, snippets=False)
    rel = [any(re.search(p, r.title) for p in patterns) for r in results]
    marks = ["✓" if ok else "✗" for ok in rel]
    return sum(rel) / 5, marks


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    index = store.load(Path(sys.argv[1]))
    totals = {"tfidf": 0.0, "bm25": 0.0}
    print("| Query | TF-IDF P@5 | top-5 | BM25 P@5 | top-5 |")
    print("|---|---|---|---|---|")
    for query, patterns in QUERIES.items():
        p_tf, m_tf = precision_at_5(index, query, patterns, TfIdf())
        p_bm, m_bm = precision_at_5(index, query, patterns, BM25())
        totals["tfidf"] += p_tf
        totals["bm25"] += p_bm
        print(f"| `{query}` | {p_tf:.1f} | {''.join(m_tf)} | {p_bm:.1f} | {''.join(m_bm)} |")
    n = len(QUERIES)
    print(f"| **mean** | **{totals['tfidf'] / n:.2f}** | | **{totals['bm25'] / n:.2f}** | |")


if __name__ == "__main__":
    main()
