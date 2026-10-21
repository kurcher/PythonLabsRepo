"""Lab 5 benchmark: serial vs threads vs processes (and the free-threaded build).

    uv run python scripts/bench_lab05.py all data/chapters --limit 1000 --runs 3 --out docs/lab05.json
    # free-threaded interpreter, threads only, appended to the same file:
    PYTHONPATH=src <python3.13t> scripts/bench_lab05.py all data/chapters --limit 1000 \
        --executors threads --tag ft --out docs/lab05.json
    uv run python scripts/bench_lab05.py report docs/lab05.json --plot docs/lab05-speedup.png
    uv run python scripts/bench_lab05.py io --urls 40       # I/O-bound counter-example

Every configuration runs in a fresh child process so its peak RSS is its own.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import sysconfig
import time
from pathlib import Path


def one(executor: str, workers: int, root: Path, limit: int) -> None:
    from findex.memory import peak_rss
    from findex.parallel import build_parallel, corpus_paths

    paths = corpus_paths(root, limit)
    cpu0 = time.process_time()
    report = build_parallel(paths, executor=executor, workers=workers)  # type: ignore[arg-type]
    parent_cpu = time.process_time() - cpu0
    # process_time() covers every thread of *this* process; child processes are extra
    cpu = parent_cpu + (report.worker_cpu if executor == "processes" else 0.0)
    print(
        json.dumps(
            {
                "executor": executor,
                "workers": workers,
                "wall": report.wall,
                "merge": report.merge,
                "cpu": cpu,
                "rss": peak_rss(),
                "docs": report.index.num_docs,
            }
        )
    )


def run_all(args: argparse.Namespace) -> None:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    tag = args.tag or ("gil" if gil else "ft")
    workers = [1, 2, 4, 8, os.cpu_count() or 1]
    configs = [("serial", 1)] if "serial" in args.executors else []
    configs += [(e, w) for e in args.executors if e != "serial" for w in workers]
    out = Path(args.out)
    rows = json.loads(out.read_text()) if out.exists() else []
    rows = [r for r in rows if r.get("tag") != tag or r["executor"] not in args.executors]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    for executor, w in configs:
        runs = []
        for _ in range(args.runs):
            res = subprocess.run(
                [sys.executable, __file__, "one", executor, str(w), args.root, str(args.limit)],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            runs.append(json.loads(res.stdout.strip().splitlines()[-1]))
        row = {
            "tag": tag,
            "python": f"{sys.version.split()[0]}{'' if gil else 't'}",
            "executor": executor,
            "workers": w,
            "wall": statistics.median(r["wall"] for r in runs),
            "cpu": statistics.median(r["cpu"] for r in runs),
            "rss": statistics.median(r["rss"] for r in runs),
            "merge": statistics.median(r["merge"] for r in runs),
            "runs": [round(r["wall"], 3) for r in runs],
            "docs": runs[0]["docs"],
        }
        rows.append(row)
        print(
            f"{tag} {executor:9s} x{w:<2d} wall {row['wall']:.2f}s runs {row['runs']}", flush=True
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=1))


def report(args: argparse.Namespace) -> None:
    rows = json.loads(Path(args.json).read_text())
    base = next(r["wall"] for r in rows if r["executor"] == "serial" and r["tag"] == "gil")
    print(
        "| Python | Executor | Workers | Wall (s, median) | CPU (s) | Peak RSS | Merge (s) | Speedup |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['python']} | {r['executor']} | {r['workers']} | {r['wall']:.2f} | {r['cpu']:.1f} "
            f"| {r['rss'] / 2**20:.0f} MiB | {r['merge']:.2f} | {base / r['wall']:.2f}× |"
        )
    if args.plot:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(7, 4.5))
        top = max(r["workers"] for r in rows)
        ax.plot([1, top], [1, top], "--", color="grey", label="ideal (linear)")
        series = sorted({(r["tag"], r["executor"]) for r in rows if r["executor"] != "serial"})
        for tag, ex in series:
            pts = sorted(
                (r["workers"], base / r["wall"])
                for r in rows
                if (r["tag"], r["executor"]) == (tag, ex)
            )
            label = f"{ex} ({'free-threaded 3.13t' if tag == 'ft' else 'GIL'})"
            ax.plot([p[0] for p in pts], [p[1] for p in pts], "o-", label=label)
        ax.set_xscale("log", base=2)
        ax.set_xlabel("workers")
        ax.set_ylabel("speedup vs serial")
        ax.set_title(f"findex indexing, {rows[0]['docs']} docs")
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(args.plot, dpi=120)
        print(f"plot -> {args.plot}")


def io_bench(args: argparse.Namespace) -> None:
    """Threads on an I/O-bound job: fetch Python docs pages (robots.txt allows /3/)."""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    pages = [
        "tutorial/index.html",
        "library/index.html",
        "reference/index.html",
        "howto/index.html",
        "library/functions.html",
        "library/stdtypes.html",
        "library/asyncio.html",
        "library/itertools.html",
        "library/collections.html",
        "library/re.html",
    ]
    urls = [
        f"https://docs.python.org/3/{p}?n={i}"
        for i in range(args.urls // len(pages) + 1)
        for p in pages
    ]
    urls = urls[: args.urls]
    ua = {"User-Agent": "findex-course-project/0.5 (+mailto:kurch2950@gmail.com)"}

    def get(url: str) -> int:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=30) as r:
                return len(r.read())
        except OSError:  # timeouts / resets count as errors, they don't abort the benchmark
            return -1

    print("| Threads | Wall (s) | Speedup | Errors |")
    print("|---|---|---|---|")
    base = 0.0
    for n in (1, 4, 8):
        t0 = time.perf_counter()
        with ThreadPoolExecutor(n) as pool:
            sizes = list(pool.map(get, urls))
        wall = time.perf_counter() - t0
        base = base or wall
        errors = sum(1 for x in sizes if x < 0)
        print(f"| {n} | {wall:.2f} | {base / wall:.1f}× | {errors} |", flush=True)


def main() -> None:
    if sys.argv[1] == "one":
        one(sys.argv[2], int(sys.argv[3]), Path(sys.argv[4]), int(sys.argv[5]))
        return
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("all")
    a.add_argument("root")
    a.add_argument("--limit", type=int, default=1000)
    a.add_argument("--runs", type=int, default=3)
    a.add_argument("--executors", nargs="+", default=["serial", "threads", "processes"])
    a.add_argument("--tag", default=None)
    a.add_argument("--out", default="docs/lab05.json")
    r = sub.add_parser("report")
    r.add_argument("json")
    r.add_argument("--plot", default=None)
    i = sub.add_parser("io")
    i.add_argument("--urls", type=int, default=40)
    args = ap.parse_args()
    print(
        f"# {sys.version.split()[0]} {sysconfig.get_platform()} cpus={os.cpu_count()}",
        file=sys.stderr,
    )
    {"all": run_all, "report": report, "io": io_bench}[args.cmd](args)


if __name__ == "__main__":
    main()
