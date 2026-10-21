"""Lab 6 benchmark: the same crawl at concurrency 1 / 5 / 20, each in a fresh process.

    uv run python scripts/bench_lab06.py --pages 200 --levels 1 5 20

Seeds span five python.org documentation hosts (all allow crawling in robots.txt),
so per-host limits (4 in flight, 0.1 s apart) still leave room for 20 global slots.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time

SEEDS = [
    "https://docs.python.org/3/",
    "https://peps.python.org/",
    "https://devguide.python.org/",
    "https://packaging.python.org/en/latest/",
    "https://typing.python.org/en/latest/",
]


def one(concurrency: int, pages: int, per_host: int, delay: float) -> None:
    from findex.crawler import CrawlStats, crawl
    from findex.memory import peak_rss

    stats = CrawlStats()

    async def go() -> int:
        n = 0
        async for _ in crawl(
            SEEDS,
            max_pages=pages,
            concurrency=concurrency,
            per_host=per_host,
            delay=delay,
            scopes=SEEDS,
            stats=stats,
        ):
            n += 1
        return n

    t0 = time.perf_counter()
    n = asyncio.run(go())
    wall = time.perf_counter() - t0
    print(
        json.dumps(
            {
                "concurrency": concurrency,
                "pages": n,
                "wall": wall,
                "errors": stats.errors,
                "rss": peak_rss(),
            }
        )
    )


def main() -> None:
    if sys.argv[1] == "one":
        one(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5]))
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--levels", type=int, nargs="+", default=[1, 5, 20])
    ap.add_argument("--per-host", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.1)
    args = ap.parse_args()
    print("| Concurrency | Pages | Wall (s) | Pages/s | Errors | Peak RSS |")
    print("|---|---|---|---|---|---|")
    for level in args.levels:
        res = subprocess.run(
            [
                sys.executable,
                __file__,
                "one",
                str(level),
                str(args.pages),
                str(args.per_host),
                str(args.delay),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        r = json.loads(res.stdout.strip().splitlines()[-1])
        print(
            f"| {level} | {r['pages']} | {r['wall']:.1f} | {r['pages'] / r['wall']:.1f} "
            f"| {r['errors']} | {r['rss'] / 2**20:.0f} MiB |",
            flush=True,
        )


if __name__ == "__main__":
    main()
