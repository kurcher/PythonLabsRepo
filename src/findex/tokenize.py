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
from collections.abc import Iterator

_MARKS = "\u0300-\u036f"  # combining diacritical marks
_APOS = "'\u2019\u02bc"  # ASCII apostrophe, right single quote, modifier letter apostrophe
# U+02BC is Unicode category Lm (a *letter*), so \w matches it: exclude it from word
# characters, otherwise a lone "ʼ" would become the token "'" (found by Hypothesis).
_LETTER = r"[^\W_\u02bc]"
_WORD = rf"{_LETTER}(?:{_LETTER}|[{_MARKS}])*"
WORD_RE = re.compile(rf"{_WORD}(?:[{_APOS}]{_WORD})*")
_MARKS_RE = re.compile(f"[{_MARKS}]")
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
