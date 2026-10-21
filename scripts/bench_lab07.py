"""Lab 7: `def` vs `async def` for /search under 20 concurrent requests.

    uv run python scripts/bench_lab07.py --index build/index-pos.bin

For each mode a fresh uvicorn (1 worker) is started with SEARCH_MODE=sync|async.
We fire 20 concurrent /search requests and, at the same moment, one /health probe:
the probe's latency shows whether the event loop stayed free while searches ran.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time

import httpx

QUERY = "the OR and OR of"  # matches nearly every document: ~CPU-heavy scoring


async def measure(base: str, n: int) -> tuple[float, list[float], float]:
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        await client.get("/search", params={"q": "warmup"})

        async def one(i: int) -> float:
            t0 = time.perf_counter()
            # page varies so the lru_cache on matching does not hide the work
            r = await client.get("/search", params={"q": QUERY, "k": 10, "page": i + 1})
            r.raise_for_status()
            return time.perf_counter() - t0

        async def probe() -> float:
            await asyncio.sleep(0.05)  # land while the searches are in flight
            t0 = time.perf_counter()
            (await client.get("/health")).raise_for_status()
            return time.perf_counter() - t0

        t0 = time.perf_counter()
        *lat, health = await asyncio.gather(*(one(i) for i in range(n)), probe())
        return time.perf_counter() - t0, lat, health


def run_mode(mode: str, index: str, port: int, n: int) -> str:
    env = {**os.environ, "INDEX_PATH": index, "SEARCH_MODE": mode, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "findex.web.app:create_app",
            "--factory",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(300):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        wall, lat, health = asyncio.run(measure(base, n))
        lat.sort()
        return (
            f"| `{'async def' if mode == 'async' else 'def'}` | {n} | {wall:.2f} "
            f"| {statistics.median(lat) * 1000:.0f} | {lat[-1] * 1000:.0f} "
            f"| {health * 1000:.0f} |"
        )
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="build/index-pos.bin")
    ap.add_argument("-n", type=int, default=20)
    args = ap.parse_args()
    print(
        "| /search is | Concurrent | Wall (s) | p50 latency (ms) | max latency (ms) | /health during load (ms) |"
    )
    print("|---|---|---|---|---|---|")
    for i, mode in enumerate(("sync", "async")):
        print(run_mode(mode, args.index, 8790 + i, args.n), flush=True)


if __name__ == "__main__":
    main()
