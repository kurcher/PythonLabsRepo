"""Lab 2 measurements: postings memory study, storage formats, merge vs set.

uv run python scripts/bench_lab02.py memory <variant> data/chapters   # one variant per process
uv run python scripts/bench_lab02.py formats data/chapters
uv run python scripts/bench_lab02.py engines index.bin
"""

from __future__ import annotations

import pickle
import sys
import tempfile
import time
import timeit
import tracemalloc
from pathlib import Path

from findex import store
from findex.corpus import iter_documents
from findex.index import build_index, build_postings_variant
from findex.search import boolean_search, intersect


def memory(variant: str, root: Path) -> None:
    tracemalloc.start()
    postings = build_postings_variant(iter_documents(root), variant)  # type: ignore[arg-type]
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.pkl"
        with path.open("wb") as fh:
            pickle.dump(postings, fh, protocol=pickle.HIGHEST_PROTOCOL)
        size = path.stat().st_size
        del postings
        t0 = time.perf_counter()
        with path.open("rb") as fh:
            pickle.load(fh)
        load = time.perf_counter() - t0
    print(
        f"| {variant} | {peak / 2**20:.0f} MiB | {current / 2**20:.0f} MiB "
        f"| {size / 2**20:.1f} MiB | {load:.2f} s |"
    )


def formats(root: Path) -> None:
    for positions in (False, True):
        index = build_index(iter_documents(root), positions=positions)
        n = sum(len(p) for p in index.postings.values())
        print(f"positions={positions}: {len(index.postings):,} terms, {n:,} postings")
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in store.FORMATS:
                path = Path(tmp) / f"ix.{fmt}"
                t0 = time.perf_counter()
                store.save(index, path, fmt=fmt)
                t_save = time.perf_counter() - t0
                t0 = time.perf_counter()
                store.load(path)
                t_load = time.perf_counter() - t0
                size = path.stat().st_size / 2**20
                print(f"| {fmt} | {positions} | {size:.1f} MiB | {t_save:.2f} s | {t_load:.2f} s |")
        del index


def engines(index_path: Path) -> None:
    index = store.load(index_path)
    by_df = sorted(index.postings, key=lambda t: len(index.postings[t]))
    common = by_df[-2:]
    rare = [t for t in by_df if t.isalpha()][:2]
    queries = {
        f"common AND: {common[0]} {common[1]}": f"{common[0]} {common[1]}",
        f"common OR: {common[0]} OR {common[1]}": f"{common[0]} OR {common[1]}",
        f"rare AND: {rare[0]} {rare[1]}": f"{rare[0]} {rare[1]}",
        f"rare OR: {rare[0]} OR {rare[1]}": f"{rare[0]} OR {rare[1]}",
        f"common AND rare: {common[0]} {rare[0]}": f"{common[0]} {rare[0]}",
    }
    for label, q in queries.items():
        row = [label]
        for engine in ("merge", "set"):
            runs = timeit.repeat(
                lambda q=q, e=engine: boolean_search(index, q, e), number=20, repeat=5
            )
            row.append(f"{min(runs) / 20 * 1000:.3f} ms")
        hits = len(boolean_search(index, q))
        print(f"| {row[0]} | {hits} | {row[1]} | {row[2]} |")

    print()
    print("bare operation on pre-extracted sorted id lists (no postings walk):")
    pairs = {"common": common, "rare": rare, "common+rare": [common[0], rare[0]]}
    for label, (t1, t2) in pairs.items():
        a = [p.doc_id for p in index.postings[t1]]
        b = [p.doc_id for p in index.postings[t2]]
        sa, sb = set(a), set(b)
        cases = {
            "merge intersect": lambda a=a, b=b: intersect(a, b),
            "set & (prebuilt sets)": lambda sa=sa, sb=sb: sa & sb,
            "set & (+ build sets + sort)": lambda a=a, b=b: sorted(set(a) & set(b)),
        }
        for name, fn in cases.items():
            best = min(timeit.repeat(fn, number=50, repeat=5)) / 50 * 1e6
            print(f"| {label} ({len(a)}, {len(b)}) | {name} | {best:.1f} us |")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "memory":
        memory(sys.argv[2], Path(sys.argv[3]))
    elif cmd == "formats":
        formats(Path(sys.argv[2]))
    else:
        engines(Path(sys.argv[2]))
