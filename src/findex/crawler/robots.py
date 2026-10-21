"""robots.txt, fetched once per host and cached."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from findex.crawler.fetch import USER_AGENT, fetch

log = logging.getLogger("findex.crawl")


class RobotsCache:
    """``await robots.allowed(url)`` — one robots.txt request per host, ever.

    404/410 means "no rules" (allow everything); any other failure means we could
    not learn the rules, so we stay out (disallow everything) — the conservative choice.
    """

    def __init__(self, client: httpx.AsyncClient, user_agent: str = USER_AGENT) -> None:
        self.client = client
        self.user_agent = user_agent
        self._parsers: dict[str, RobotFileParser] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _parser_for(self, origin: str) -> RobotFileParser:
        if origin in self._parsers:
            return self._parsers[origin]
        # Several workers may hit a new host at once: one fetches, the rest wait.
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._parsers:
                parser = RobotFileParser()
                result = await fetch(self.client, f"{origin}/robots.txt", retries=1)
                if result.status in (404, 410):
                    parser.parse([])
                elif result.ok:
                    parser.parse(result.content.decode("utf-8", "replace").splitlines())
                else:
                    parser.parse(["User-agent: *", "Disallow: /"])
                    log.warning(
                        "robots.txt for %s unavailable (%s): staying out", origin, result.status
                    )
                self._parsers[origin] = parser
        return self._parsers[origin]

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        parser = await self._parser_for(f"{parts.scheme}://{parts.netloc}")
        return parser.can_fetch(self.user_agent, url)

    async def crawl_delay(self, url: str) -> float | None:
        parts = urlsplit(url)
        parser = await self._parser_for(f"{parts.scheme}://{parts.netloc}")
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None
