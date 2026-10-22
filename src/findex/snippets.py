"""Result snippets: a window of text around the densest cluster of query terms."""

from __future__ import annotations

import functools
import html
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from findex.tokenize import APOSTROPHES, COMBINING_MARKS, LETTER, WORD_RE, tokenize

WINDOW = 80


@dataclass(frozen=True, slots=True)
class Snippet:
    text: str
    highlights: tuple[tuple[int, int], ...] = ()  # (start, end) offsets into ``text``

    def _render(self, before: str, after: str, escape: bool) -> str:
        esc: Callable[[str], str] = html.escape if escape else str
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


def _multi_casefolds() -> dict[str, list[str]]:
    """Characters whose casefold is *several* letters, keyed by that expansion
    (``"ss"`` → ``["ß", "ẞ"]``). IGNORECASE only maps one character to one character."""
    table: dict[str, list[str]] = {}
    for cp in range(0x10000):
        ch = chr(cp)
        folded = ch.casefold()
        if len(folded) > 1 and folded.isalnum():
            table.setdefault(folded, []).append(ch)
    return table


_EXPANSIONS = _multi_casefolds()
_EXPANSION_KEYS = sorted(_EXPANSIONS, key=len, reverse=True)


def _term_regex(term: str, apos: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(term):
        if term[i] == "'":
            out.append(apos)
            i += 1
            continue
        for exp in _EXPANSION_KEYS:
            if term.startswith(exp, i):
                choices = [re.escape(exp), *map(re.escape, _EXPANSIONS[exp])]
                out.append(f"(?:{'|'.join(choices)})")
                i += len(exp)
                break
        else:
            out.append(re.escape(term[i]))
            i += 1
    return "".join(out)


@functools.lru_cache(maxsize=512)
def _terms_pattern(terms: frozenset[str]) -> re.Pattern[str] | None:
    """One case-insensitive alternation of the query terms, with tokenizer-style edges.

    Lab 8, hotspot #1: calling ``tokenize`` on every word of every result document was
    94 % interpreter overhead. Here the regex engine (C) finds *candidates* in one
    pass; only those few go through the exact Python normalization check.
    """
    if not terms:
        return None
    apos = f"[{APOSTROPHES}]"
    letter = LETTER  # the tokenizer's own letter class (U+02BC is *not* a letter here)
    alts = sorted((_term_regex(t, apos) for t in terms), key=len, reverse=True)
    return re.compile(
        rf"(?<!{letter})(?<!{letter}{apos})(?:{'|'.join(alts)})(?![{COMBINING_MARKS}])(?!{apos}?{letter})",
        re.IGNORECASE,
    )


def find_term_spans(text: str, wanted: frozenset[str]) -> list[tuple[int, int, str]]:
    """``(start, end, term)`` of every token of ``text`` that normalizes into ``wanted``."""
    pattern = _terms_pattern(wanted)
    if pattern is None:
        return []
    return [
        (m.start(), m.end(), tok)
        for m in pattern.finditer(text)
        if (tok := next(tokenize(m.group()), "")) in wanted  # exact check, few calls
    ]


def find_term_spans_slow(text: str, wanted: frozenset[str]) -> list[tuple[int, int, str]]:
    """The Lab 3 version: tokenize every word. Kept as the reference for tests."""
    return [
        (m.start(), m.end(), tok)
        for m in WORD_RE.finditer(text)
        if (tok := next(tokenize(m.group()), "")) in wanted
    ]


def make_snippet(text: str, query_terms: Iterable[str], window: int = WINDOW) -> Snippet:
    """Pick the ~``2*window``-char span with the most *distinct* query terms; highlight them.

    Matching runs on the NFC text without casefolding so the snippet keeps its case;
    each regex match is normalized exactly as ``tokenize`` would.
    """
    wanted = frozenset(query_terms)
    text = unicodedata.normalize("NFC", text)
    # the first line is the document title, already shown above the snippet
    _title, sep, body = text.partition("\n")
    if sep and body.strip():
        text = body.lstrip()
    spans = find_term_spans(text, wanted)

    if spans:
        best_start, best = spans[0][0], (0, 0)
        j = 0
        for i, (start, _, _) in enumerate(spans):  # sliding window over match offsets
            while spans[j][0] < start - window:
                j += 1
            # most distinct terms first, then most matches overall
            score = (len({t for _, _, t in spans[j : i + 1]}), i + 1 - j)
            if score > best:
                best, best_start = score, spans[j][0]
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
