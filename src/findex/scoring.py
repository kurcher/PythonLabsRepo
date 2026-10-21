"""Ranking: a ``Scorer`` is anything callable as ``score(term, posting, index)``."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from findex.index import Index, Posting


class Scorer(Protocol):
    """Structural type: no inheritance needed, only a matching ``__call__``."""

    def __call__(self, term: str, posting: Posting, index: Index) -> float: ...


@dataclass(frozen=True, slots=True)
class TfIdf:
    """``(1 + log tf) * log(N / df)`` — sublinear tf, classic idf."""

    def __call__(self, term: str, posting: Posting, index: Index) -> float:
        return (1.0 + math.log(posting.tf)) * math.log(index.num_docs / index.df(term))


@dataclass(frozen=True, slots=True)
class BM25:
    """Okapi BM25: saturating tf (``k1``) and document-length normalization (``b``)."""

    k1: float = 1.5
    b: float = 0.75

    def __call__(self, term: str, posting: Posting, index: Index) -> float:
        n, df = index.num_docs, index.df(term)
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        norm = 1.0 - self.b + self.b * index.doc_length(posting.doc_id) / index.avg_doc_length
        tf = posting.tf
        return idf * tf * (self.k1 + 1.0) / (tf + self.k1 * norm)


SCORERS: dict[str, Scorer] = {"bm25": BM25(), "tfidf": TfIdf()}
