"""Result snippets: a window of text around the densest cluster of query terms."""

from __future__ import annotations

import html
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from findex.tokenize import WORD_RE, tokenize

WINDOW = 80


@dataclass(frozen=True, slots=True)
class Snippet:
    text: str
    highlights: tuple[tuple[int, int], ...] = ()  # (start, end) offsets into ``text``

    def _render(self, before: str, after: str, escape: bool) -> str:
        esc = html.escape if escape else (lambda s: s)
        out: list[str] = []
        last = 0
        for start, end in self.highlights:
            out += [esc(self.text[last:start]), before, esc(self.text[start:end]), after]
            last = end
        out.append(esc(self.text[last:]))
        return "".join(out)

    def render(self, before: str = "**", after: str = "**") -> str:
        return self._render(before, after, escape=False)

    def render_html(self) -> str:
        return self._render("<mark>", "</mark>", escape=True)


def _collapse(segment: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to one space; return text and old→new offset map."""
    out: list[str] = []
    mapping: list[int] = []
    prev_space = True  # also drops leading whitespace
    for ch in segment:
        mapping.append(len(out))
        if ch.isspace():
            if not prev_space:
                out.append(" ")
            prev_space = True
        else:
            out.append(ch)
            prev_space = False
    mapping.append(len(out))
    return "".join(out).rstrip(), mapping


def make_snippet(text: str, query_terms: Iterable[str], window: int = WINDOW) -> Snippet:
    """Pick the ~``2*window``-char span with the most *distinct* query terms; highlight them.

    Matching runs on the NFC text without casefolding so the snippet keeps its case;
    each regex match is normalized exactly as ``tokenize`` would.
    """
    wanted = set(query_terms)
    text = unicodedata.normalize("NFC", text)
    spans = [
        (m.start(), m.end(), tok)
        for m in WORD_RE.finditer(text)
        if (tok := next(tokenize(m.group()), "")) in wanted
    ]

    if spans:
        best_start, best = spans[0][0], 0
        j = 0
        for i, (start, _, _) in enumerate(spans):  # sliding window over match offsets
            while spans[j][0] < start - window:
                j += 1
            distinct = len({t for _, _, t in spans[j : i + 1]})
            if distinct > best:
                best, best_start = distinct, spans[j][0]
        lo = max(0, best_start - window // 2)
    else:
        lo = 0
    hi = min(len(text), lo + 2 * window)
    while lo > 0 and not text[lo - 1].isspace():  # don't cut words in half
        lo -= 1
    while hi < len(text) and not text[hi].isspace():
        hi += 1

    body, mapping = _collapse(text[lo:hi])
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    shift = len(prefix)
    highlights = tuple(
        (mapping[s - lo] + shift, mapping[e - lo] + shift)
        for s, e, _ in spans
        if lo <= s and e <= hi
    )
    return Snippet(prefix + body + suffix, highlights)
