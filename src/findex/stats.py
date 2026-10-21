"""Corpus statistics in one streaming pass.

    python -m findex.stats data/ [--limit N] [--top 50] [--eager]

``--eager`` runs the deliberately wasteful list-based version for comparison.
"""

from __future__ import annotations

import argparse
import time
import tracemalloc
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from findex.corpus import Document, iter_documents
from findex.tokenize import tokenize


@dataclass
class CorpusStats:
    documents: int
    tokens: int
    counts: Counter[str]

    @property
    def vocabulary(self) -> int:
        return len(self.counts)


def lazy_stats(docs: Iterable[Document]) -> CorpusStats:
    """Single pass: one document and the Counter are the only things alive."""
    counts: Counter[str] = Counter()
    n_docs = 0
    for doc in docs:
        n_docs += 1
        counts.update(tokenize(doc.text))
    return CorpusStats(n_docs, counts.total(), counts)


def eager_stats(docs: Iterable[Document]) -> CorpusStats:
    """The anti-pattern on purpose: materialize every stage as a list."""
    all_docs = list(docs)  # the whole corpus text in RAM
    all_tokens = [list(tokenize(d.text)) for d in all_docs]  # every token as a str object
    flat = [tok for doc_tokens in all_tokens for tok in doc_tokens]  # + one more list of pointers
    counts = Counter(flat)
    return CorpusStats(len(all_docs), len(flat), counts)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m findex.stats")
    ap.add_argument("root", type=Path)
    ap.add_argument("--limit", type=int, default=None, help="only the first N documents")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--eager", action="store_true", help="use the list-based version")
    args = ap.parse_args(argv)

    docs = iter_documents(args.root)
    if args.limit is not None:
        docs = islice(docs, args.limit)
    compute = eager_stats if args.eager else lazy_stats

    tracemalloc.start()
    t0 = time.perf_counter()
    stats = compute(docs)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"version:     {'eager (lists)' if args.eager else 'lazy (generators)'}")
    print(f"documents:   {stats.documents:,}")
    print(f"tokens:      {stats.tokens:,}")
    print(f"vocabulary:  {stats.vocabulary:,}")
    print(f"\ntop {args.top} terms:")
    for rank, (term, count) in enumerate(stats.counts.most_common(args.top), 1):
        print(f"  {rank:3d}. {term:<15} {count:>10,}")
    print(f"\nelapsed:     {elapsed:.2f} s")
    print(f"peak memory: {peak / 2**20:.1f} MiB (tracemalloc)")


if __name__ == "__main__":
    main()
