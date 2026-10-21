"""The crawler: a Queue frontier, TaskGroup workers, global + per-host limits.

``crawl(...)`` is an *async generator*: pages come out as they are fetched, so the
caller can stream them to disk; nothing is buffered beyond a small output queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from findex.crawler.fetch import USER_AGENT, fetch
from findex.crawler.html import canonicalize, extract_links, extract_text
from findex.crawler.robots import RobotsCache

log = logging.getLogger("findex.crawl")


@dataclass(frozen=True, slots=True)
class Page:
    url: str
    title: str
    text: str
    fetched_at: str

    def to_json(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class CrawlStats:
    """Live counters for the progress display (one event loop → no locks needed)."""

    started: float = field(default_factory=time.perf_counter)
    pages: int = 0
    fetched: int = 0
    errors: int = 0
    skipped_robots: int = 0
    in_flight: int = 0
    queued: int = 0

    @property
    def pages_per_s(self) -> float:
        return self.pages / max(1e-9, time.perf_counter() - self.started)


class _Host:
    """Per-host politeness: at most ``per_host`` concurrent requests, ``delay`` apart."""

    def __init__(self, per_host: int, delay: float) -> None:
        self.sem = asyncio.Semaphore(per_host)
        self.delay = delay
        self.lock = asyncio.Lock()
        self.next_slot = 0.0

    async def wait_turn(self) -> None:
        async with self.lock:  # serialize the bookkeeping, not the requests
            now = time.monotonic()
            start = max(now, self.next_slot)
            self.next_slot = start + self.delay
        if start > now:
            await asyncio.sleep(start - now)


def parse_page(url: str, content: bytes) -> tuple[list[str], str, str]:
    """Links, title and text of one HTML page (pure CPU; runs off the event loop)."""
    tree = HTMLParser(content)
    links = extract_links(url, tree)
    title, text = extract_text(tree)
    return links, title, text


def host_of(url: str) -> str:
    return urlsplit(url).netloc


def domain_allowed(url: str, allowed_domains: Iterable[str]) -> bool:
    host = urlsplit(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


async def crawl(
    seeds: Iterable[str],
    *,
    max_pages: int = 100,
    concurrency: int = 10,
    per_host: int = 2,
    delay: float = 0.25,
    allowed_domains: Iterable[str] | None = None,
    scopes: Iterable[str] = (),
    client: httpx.AsyncClient | None = None,
    stats: CrawlStats | None = None,
    timeout: float = 10.0,
) -> AsyncIterator[Page]:
    """Breadth-first crawl from ``seeds``; yields at most ``max_pages`` HTML pages."""
    seed_urls = [u for u in (canonicalize(s) for s in seeds) if u]
    domains = tuple(allowed_domains or {urlsplit(u).hostname or "" for u in seed_urls})
    scope_list = tuple(scopes)
    stats = stats or CrawlStats()
    own_client = client is None
    if client is None:
        # Building the SSL context loads the OS certificate store (~0.3 s on Windows,
        # flagged by debug=True): do it in a thread instead of blocking the loop.
        client = await asyncio.to_thread(
            httpx.AsyncClient,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=concurrency),
            timeout=timeout,
        )
    robots = RobotsCache(client)
    global_sem = asyncio.Semaphore(concurrency)
    hosts: defaultdict[str, _Host] = defaultdict(lambda: _Host(per_host, delay))
    frontier: asyncio.Queue[str] = asyncio.Queue()
    out: asyncio.Queue[Page | None] = asyncio.Queue(maxsize=concurrency * 2)
    seen: set[str] = set()
    reserved = 0  # fetches started that may still become pages (budget accounting)
    done = asyncio.Event()

    def enqueue(url: str) -> None:
        if url in seen or not domain_allowed(url, domains):
            return
        if scope_list and not url.startswith(scope_list):
            return
        seen.add(url)
        frontier.put_nowait(url)
        stats.queued = frontier.qsize()

    async def process(url: str) -> None:
        nonlocal reserved
        if not await robots.allowed(url):
            stats.skipped_robots += 1
            log.info("%s skipped (robots.txt)", url)
            return
        host = hosts[host_of(url)]
        robots_delay = await robots.crawl_delay(url)
        if robots_delay:
            host.delay = max(host.delay, robots_delay)
        async with global_sem, host.sem:
            await host.wait_turn()
            stats.in_flight += 1
            try:
                result = await fetch(client, url, timeout=timeout)
            finally:
                stats.in_flight -= 1
        stats.fetched += 1
        log.info(
            "%s %s %.0fms attempts=%d",
            result.status or "ERR",
            url,
            result.elapsed * 1000,
            result.attempts,
        )
        if not result.ok:
            stats.errors += 1
            reserved -= 1
            return
        final = canonicalize(result.final_url) or url
        if final != url:
            seen.add(final)
        if not result.is_html:
            reserved -= 1
            return
        # Parsing is CPU work (0.1-0.4 s on big pages, measured with debug=True):
        # run it in a worker thread so the event loop keeps serving the other fetches.
        links, title, text = await asyncio.to_thread(parse_page, final, result.content)
        for link in links:
            enqueue(link)
        if not text:
            reserved -= 1
            return
        await out.put(Page(final, title, text, datetime.now(UTC).isoformat(timespec="seconds")))
        stats.pages += 1
        if stats.pages >= max_pages:
            done.set()

    async def worker() -> None:
        nonlocal reserved
        while True:
            url = await frontier.get()
            try:
                stats.queued = frontier.qsize()
                if reserved >= max_pages:
                    continue  # budget is committed; drain the rest of the queue
                reserved += 1
                await process(url)
            except Exception:  # one bad page must not kill the crawl
                stats.errors += 1
                reserved -= 1
                log.exception("%s crashed the worker", url)
            finally:
                frontier.task_done()

    async def supervisor() -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                workers = [tg.create_task(worker()) for _ in range(concurrency)]
                drained = tg.create_task(frontier.join())
                budget = tg.create_task(done.wait())
                await asyncio.wait({drained, budget}, return_when=asyncio.FIRST_COMPLETED)
                for task in (*workers, drained, budget):
                    task.cancel()  # workers block forever on get(); stop them explicitly
        finally:
            await out.put(None)  # tell the consumer we are finished

    for url in seed_urls:
        enqueue(url)
    runner = asyncio.create_task(supervisor())
    try:
        while (page := await out.get()) is not None:
            yield page
        await runner  # re-raise anything the supervisor raised
    finally:
        if not runner.done():  # the consumer stopped early (break / exception)
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        if own_client:
            await client.aclose()
