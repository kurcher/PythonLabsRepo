"""Streaming tokenizer.

Rules (they are tested in ``tests/test_tokenize.py``):

1. **Normalization.** The text is NFC-normalized, then ``casefold()``-ed
   (``"Straße"`` → ``"strasse"``, ``"ПРИВІТ"`` → ``"привіт"``).
2. **Word characters** are Unicode letters and digits (``\\w`` without ``_``).
   Everything else — spaces, punctuation, underscores — separates tokens.
3. **Combining marks** that survive NFC (e.g. a Ukrainian stress mark: ``"за\u0301мок"``)
   stay inside the word while matching and are then removed, so ``"за\u0301мок" == "замок"``.
4. **Apostrophes** (``'``, ``\u2019``, ``\u02bc``) *between* two word characters are kept and
   unified to ``'``: ``"don\u2019t"`` → ``"don't"``, ``"п\u02bcять"`` → ``"п'ять"``.
   Leading/trailing apostrophes are quote marks and are dropped.
5. **Hyphens split**: ``"well-known"`` → ``"well"``, ``"known"``. Query-side, the same
   split happens, so a search for "well-known" still finds it.
6. **Digits are tokens**: ``"1984"`` stays, ``"mp3"`` stays one token,
   ``"3.14"`` → ``"3"``, ``"14"``.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterator

COMBINING_MARKS = "\u0300-\u036f"  # combining diacritical marks
APOSTROPHES = "'\u2019\u02bc"  # ASCII apostrophe, right single quote, modifier letter apostrophe
# U+02BC is Unicode category Lm (a *letter*), so \w matches it: exclude it from word
# characters, otherwise a lone "ʼ" would become the token "'" (found by Hypothesis).
LETTER = r"[^\W_\u02bc]"
_WORD = rf"{LETTER}(?:{LETTER}|[{COMBINING_MARKS}])*"
WORD_RE = re.compile(rf"{_WORD}(?:[{APOSTROPHES}]{_WORD})*")
_MARKS_RE = re.compile(f"[{COMBINING_MARKS}]")
_APOS_TABLE = str.maketrans({"\u2019": "'", "\u02bc": "'"})


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def tokenize(text: str) -> Iterator[str]:
    """Yield normalized tokens of ``text`` lazily, one regex match at a time."""
    for match in WORD_RE.finditer(normalize(text)):
        token = match.group().translate(_APOS_TABLE)
        if not token.isascii():
            token = _MARKS_RE.sub("", token)
        yield token


def count_terms(text: str) -> Counter[str]:
    """``Counter(tokenize(text))``, computed with the per-token loop in C (Lab 8).

    ``findall`` + ``Counter`` count raw matches inside the regex engine and the C
    counting helper; the apostrophe/mark normalization then runs once per *distinct*
    raw word instead of once per token. Same result, tested by Hypothesis.
    """
    raw = Counter(WORD_RE.findall(normalize(text)))
    if all(word.isascii() and "'" not in word for word in raw):
        return raw  # the common case: nothing to rewrite
    out: Counter[str] = Counter()
    for word, n in raw.items():
        token = word.translate(_APOS_TABLE)
        if not token.isascii():
            token = _MARKS_RE.sub("", token)
        out[token] += n
    return out
