"""Lab 8 profiling harness.

uv run python scripts/profile_lab08.py search build/index-pos.bin      # cProfile of search only
uv run py-spy record -o docs/flame-search.svg -- python scripts/profile_lab08.py search-loop build/index-pos.bin
uv run py-spy record -o docs/flame-index.svg -- python -m findex index data/chapters --out build/p.bin --limit 1000 --executor serial
"""

from __future__ import annotations

import cProfile
import pstats
import sys
import time
from pathlib import Path

from findex import store
from findex.scoring import BM25

QUERIES = ["white whale ship", "the sea captain", "love marriage fortune", "war battle general"]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    mode, path = sys.argv[1], Path(sys.argv[2])
    from findex.search import search

    t0 = time.perf_counter()
    index = store.load(path)
    print(f"load {time.perf_counter() - t0:.1f} s", file=sys.stderr)
    if mode == "search-loop":  # long enough for py-spy to sample well
        t0 = time.perf_counter()
        n = 0
        while time.perf_counter() - t0 < 20:
            for q in QUERIES:
                index.matching.cache_clear()
                search(index, q, BM25(), k=10)
                n += 1
        print(f"{n} searches", file=sys.stderr)
        return
    prof = cProfile.Profile()
    prof.enable()
    for _ in range(5):
        for q in QUERIES:
            index.matching.cache_clear()
            search(index, q, BM25(), k=10)
    prof.disable()
    stats = pstats.Stats(prof).sort_stats("cumulative")
    stats.print_stats(18)
    stats.sort_stats("tottime").print_stats(12)


if __name__ == "__main__":
    main()
