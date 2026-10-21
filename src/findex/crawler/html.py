"""URL canonicalization, link and text extraction (selectolax: a fast C HTML parser)."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

SKIP_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|ico|css|js|pdf|zip|gz|tar|bz2|xz|epub|mp4|woff2?)$", re.I
)


def canonicalize(url: str) -> str | None:
    """Lowercase scheme/host, drop the fragment and default port, sort the query,
    ``/a/b/index.html`` → ``/a/b/``. Non-HTTP(S) URLs return ``None``."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    path = re.sub(r"/{2,}", "/", path)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))


def extract_links(base_url: str, tree: HTMLParser) -> list[str]:
    links: list[str] = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href")
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        url = canonicalize(urljoin(base_url, href))
        if url and not SKIP_EXT.search(urlsplit(url).path):
            links.append(url)
    return links


def extract_text(tree: HTMLParser) -> tuple[str, str]:
    """``(title, readable text)``: main content if the page marks it, else ``<body>``."""
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    for junk in tree.css("script, style, noscript, nav, header, footer, aside, form"):
        junk.decompose()
    main = (
        tree.css_first("main")
        or tree.css_first("[role=main]")
        or tree.css_first("article")
        or tree.body
    )
    text = main.text(separator=" ", strip=True) if main else ""
    return title, re.sub(r"\s+", " ", text).strip()
