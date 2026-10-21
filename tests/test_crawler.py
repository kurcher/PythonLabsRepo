"""Crawler tests: every HTTP exchange goes through ``httpx.MockTransport`` — no network."""

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from findex.corpus import iter_documents
from findex.crawler import CrawlStats, RobotsCache, crawl, fetch
from findex.crawler.fetch import retry_after_seconds
from findex.crawler.html import canonicalize

SITE = {
    "/": "<html><head><title>Home</title></head><body><main>Welcome home. "
    '<a href="/a">A</a> <a href="/b#frag">B</a> <a href="https://other.org/x">ext</a>'
    ' <a href="/private/secret">secret</a> <a href="/logo.png">img</a></main></body></html>',
    "/a": '<html><title>Page A</title><body><p>Alpha whale text</p><a href="/">home</a>'
    '<a href="/c?b=2&a=1">C</a><script>var x = "noise"</script></body></html>',
    "/b": "<html><title>Page B</title><body><nav>menu</nav><article>Beta sea text</article>"
    '<a href="/a">A again</a></body></html>',
    "/c?a=1&b=2": "<html><title>Page C</title><body>Gamma ship text</body></html>",
    "/private/secret": "<html><title>Secret</title><body>do not crawl</body></html>",
}
ROBOTS = "User-agent: *\nDisallow: /private/\n"


Handler = Callable[[httpx.Request], httpx.Response]


def site_handler(hits: Counter[str]) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        hits[path] += 1
        if request.url.host != "example.com":
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if path in SITE:
            return httpx.Response(200, html=SITE[path])
        return httpx.Response(404, text="nope")

    return handle


def client_for(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def no_sleep(_: float) -> None:
    return None


def test_canonicalize():
    assert canonicalize("HTTPS://Example.COM:443/a/index.html#x") == "https://example.com/a/"
    assert canonicalize("http://example.com:8080//b?z=1&a=2") == "http://example.com:8080/b?a=2&z=1"
    assert canonicalize("mailto:me@example.com") is None
    assert canonicalize("https://example.com") == "https://example.com/"


def test_retry_after_parsing():
    assert retry_after_seconds("3") == 3.0
    assert retry_after_seconds("9999") == 60.0  # capped
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:05 GMT", now=1445412480.0) == 5.0
    assert retry_after_seconds("garbage") is None and retry_after_seconds(None) is None


def test_fetch_retries_5xx_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls < 3 else httpx.Response(200, text="ok")

    async def run():
        async with client_for(handler) as c:
            return await fetch(c, "https://example.com/", sleep=no_sleep)

    result = asyncio.run(run())
    assert result.ok and result.attempts == 3 and calls == 3


def test_fetch_honors_retry_after_on_429():
    waits: list[float] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, text="ok")

    async def record(seconds: float) -> None:
        waits.append(seconds)

    async def run():
        async with client_for(handler) as c:
            return await fetch(c, "https://example.com/", sleep=record)

    assert asyncio.run(run()).ok
    assert waits == [7.0]


def test_fetch_does_not_retry_404_and_gives_up_on_timeouts():
    calls = Counter[str]()

    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        if request.url.path == "/slow":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(404)

    async def run():
        async with client_for(handler) as c:
            return (
                await fetch(c, "https://example.com/missing", sleep=no_sleep),
                await fetch(c, "https://example.com/slow", sleep=no_sleep, retries=3),
            )

    missing, slow = asyncio.run(run())
    assert missing.status == 404 and not missing.ok and calls["/missing"] == 1
    assert slow.status == 0 and "ReadTimeout" in (slow.error or "") and calls["/slow"] == 4


def test_robots_cache_fetches_once_per_host():
    hits = Counter[str]()

    async def run():
        async with client_for(site_handler(hits)) as c:
            robots = RobotsCache(c)
            results = await asyncio.gather(
                robots.allowed("https://example.com/a"),
                robots.allowed("https://example.com/private/x"),
                robots.allowed("https://example.com/b"),
            )
            missing = await robots.allowed("https://other.org/anything")  # 404 → allow
            return results, missing

    (a, private, b), other = asyncio.run(run())
    assert (a, private, b, other) == (True, False, True, True)
    assert hits["/robots.txt"] == 2  # example.com once (despite 3 concurrent asks) + other.org


def test_crawl_end_to_end():
    hits = Counter[str]()
    stats = CrawlStats()

    async def run():
        async with client_for(site_handler(hits)) as c:
            return [
                p
                async for p in crawl(
                    ["https://example.com/"],
                    max_pages=50,
                    concurrency=3,
                    delay=0,
                    client=c,
                    stats=stats,
                )
            ]

    pages = asyncio.run(run())
    urls = sorted(p.url for p in pages)
    assert urls == [
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c?a=1&b=2",
    ]
    assert all(n == 1 for path, n in hits.items() if path != "/robots.txt")  # nothing twice
    assert "/private/secret" not in hits and "/logo.png" not in hits  # robots + extension filter
    page_a = next(p for p in pages if p.url.endswith("/a"))
    assert page_a.title == "Page A" and "noise" not in page_a.text and "Alpha whale" in page_a.text
    page_b = next(p for p in pages if p.url.endswith("/b"))
    assert "menu" not in page_b.text  # <nav> stripped
    # /private/secret is queued (same domain) but robots.txt forbids it: skipped, never fetched
    assert (stats.pages, stats.fetched, stats.skipped_robots) == (4, 4, 1)


def test_crawl_stops_at_max_pages():
    hits = Counter[str]()

    async def run():
        async with client_for(site_handler(hits)) as c:
            return [
                p async for p in crawl(["https://example.com/"], max_pages=2, delay=0, client=c)
            ]

    assert len(asyncio.run(run())) == 2


def test_consumer_can_stop_early():
    hits = Counter[str]()

    async def run():
        async with client_for(site_handler(hits)) as c:
            async for _page in crawl(["https://example.com/"], max_pages=50, delay=0, client=c):
                break  # the generator's finally must cancel the workers cleanly
        return True

    assert asyncio.run(run())


def test_jsonl_output_feeds_the_indexer(tmp_path: Path):
    path = tmp_path / "crawl.jsonl"
    records = [{"url": "https://e/1", "title": "One", "text": "whale sea", "fetched_at": "x"}]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    docs = list(iter_documents(path))
    assert docs[0].text == "One\n\nwhale sea" and docs[0].locator.endswith("#1")


@pytest.mark.parametrize("bad", ["ftp://example.com/x", "javascript:alert(1)"])
def test_non_http_seeds_are_ignored(bad: str):
    async def run():
        return [p async for p in crawl([bad], max_pages=5, delay=0)]

    assert asyncio.run(run()) == []
