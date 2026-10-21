"""One polite HTTP fetch: timeout, bounded retries with backoff + jitter, Retry-After."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger("findex.crawl")

USER_AGENT = (
    "findex-crawler/0.6 (student search-engine project; +https://github.com/kurcher/PythonLabsRepo)"
)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 60.0


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int  # 0 when no HTTP response arrived (timeout, connection error)
    content: bytes
    content_type: str
    elapsed: float
    attempts: int
    final_url: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error is None

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    """Parse ``Retry-After``: either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return min(max(0.0, when - (now if now is not None else time.time())), MAX_RETRY_AFTER)


def backoff(attempt: int, base: float, rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter: uniform(0, base * 2**attempt)."""
    return (rng or random).uniform(0, base * 2**attempt)


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = 10.0,
    retries: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FetchResult:
    """GET ``url``; retry 429/5xx/timeouts up to ``retries`` times, never other 4xx.

    ``sleep`` is injectable so tests can run the retry schedule instantly.
    """
    t0 = time.perf_counter()
    attempt = 0
    while True:
        attempt += 1
        wait: float | None = None
        try:
            async with asyncio.timeout(timeout):
                resp = await client.get(url, follow_redirects=True)
            status = resp.status_code
            if status in RETRY_STATUSES and attempt <= retries:
                wait = retry_after_seconds(resp.headers.get("retry-after"))
                if wait is None:
                    wait = backoff(attempt - 1, base_delay)
                log.warning("%s -> %d, retry %d in %.2f s", url, status, attempt, wait)
            else:
                return FetchResult(
                    url,
                    status,
                    resp.content,
                    resp.headers.get("content-type", ""),
                    time.perf_counter() - t0,
                    attempt,
                    str(resp.url),
                    None if resp.is_success else f"HTTP {status}",
                )
        except (TimeoutError, httpx.TransportError) as exc:
            # asyncio.timeout raises TimeoutError; CancelledError is a BaseException and
            # deliberately passes through, so cancelling the crawl still works.
            error = f"{type(exc).__name__}: {exc}".strip(": ")
            if attempt > retries:
                log.warning("%s failed after %d attempts: %s", url, attempt, error)
                return FetchResult(url, 0, b"", "", time.perf_counter() - t0, attempt, url, error)
            wait = backoff(attempt - 1, base_delay)
            log.warning("%s -> %s, retry %d in %.2f s", url, error, attempt, wait)
        await sleep(wait)
