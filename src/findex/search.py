"""Boolean search over the inverted index.

    python -m findex.search index.bin "whale OR shark NOT ship" [--engine merge|set]

Query syntax (Lab 2 — evaluated strictly left to right; Lab 3 adds a real parser):
words are ANDed implicitly; ``OR`` and ``NOT`` combine the running result with
the next word: ``a b OR c NOT d`` == ``((a AND b) OR c) AND NOT d``.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from findex.index import InvertedIndex
from findex.memory import peak_rss, utf8_stdout
from findex.tokenize import tokenize

Engine = Literal["merge", "set"]


# --- two-pointer merges over sorted doc_id lists: O(len(a) + len(b)) -------------


def intersect(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a) and j < len(b):
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            i += 1
        else:
            j += 1
    return out


def union(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a) and j < len(b):
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            out.append(y)
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def difference(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a):
        if j == len(b) or a[i] < b[j]:
            out.append(a[i])
            i += 1
        elif a[i] == b[j]:
            i += 1
            j += 1
        else:
            j += 1
    return out


def _set_op(op: Callable[[set[int], set[int]], set[int]]):
    def run(a: Sequence[int], b: Sequence[int]) -> list[int]:
        return sorted(op(set(a), set(b)))

    return run


ENGINES: dict[Engine, tuple[Callable[..., list[int]], ...]] = {
    "merge": (intersect, union, difference),
    "set": (_set_op(set.__and__), _set_op(set.__or__), _set_op(set.__sub__)),
}


def doc_ids(index: InvertedIndex, word: str) -> list[int]:
    """Doc ids containing ``word``; a word that tokenizes to several terms ANDs them."""
    result: list[int] | None = None
    for term in tokenize(word):
        ids = [p.doc_id for p in index.postings.get(term, ())]
        result = ids if result is None else intersect(result, ids)
    return result or []


def boolean_search(index: InvertedIndex, query: str, engine: Engine = "merge") -> list[int]:
    and_, or_, not_ = ENGINES[engine]
    result: list[int] | None = None
    op = and_
    for word in query.split():
        if word == "OR":
            op = or_
            continue
        if word == "NOT":
            op = not_
            continue
        ids = doc_ids(index, word)
        if result is None:
            result = difference(sorted(index.doc_meta), ids) if op is not_ else ids
        else:
            result = op(result, ids)
        op = and_
    return result or []


def main(argv: list[str] | None = None) -> None:
    from findex import store

    ap = argparse.ArgumentParser(prog="python -m findex.search")
    ap.add_argument("index", type=Path)
    ap.add_argument("query")
    ap.add_argument("--engine", choices=("merge", "set"), default="merge")
    ap.add_argument("--show", type=int, default=20)
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    index = store.load(args.index)
    t_load = time.perf_counter() - t0
    t0 = time.perf_counter()
    hits = boolean_search(index, args.query, engine=args.engine)
    t_search = time.perf_counter() - t0
    utf8_stdout()

    print(f"{len(hits)} documents match {args.query!r}")
    for doc_id in hits[: args.show]:
        print(f"  [{doc_id:5d}] {index.doc_meta[doc_id].title}")
    if len(hits) > args.show:
        print(f"  ... and {len(hits) - args.show} more")
    print(f"load: {t_load:.2f} s  search: {t_search * 1000:.2f} ms  ({args.engine})")
    print(f"peak memory: {peak_rss() / 2**20:.0f} MiB (peak RSS)")


if __name__ == "__main__":
    main()
