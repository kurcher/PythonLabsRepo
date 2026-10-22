"""Ranking: a ``Scorer`` is anything callable as ``score(term, posting, index)``."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from findex.index import Index, Posting

F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]


class Scorer(Protocol):
    """Structural type: no inheritance needed, only a matching ``__call__``."""

    def __call__(self, term: str, posting: Posting, index: Index) -> float: ...


@runtime_checkable
class VectorScorer(Protocol):
    """A scorer that can also weight a whole postings list at once (Lab 8)."""

    def __call__(self, term: str, posting: Posting, index: Index) -> float: ...

    def vector(self, term: str, index: Index) -> tuple[I64, F64]:
        """``(doc_ids, weights)`` for every posting of ``term``, as arrays."""
        ...


@dataclass(frozen=True, slots=True)
class TfIdf:
    """``(1 + log tf) * log(N / df)`` — sublinear tf, classic idf."""

    def __call__(self, term: str, posting: Posting, index: Index) -> float:
        return (1.0 + math.log(posting.tf)) * math.log(index.num_docs / index.df(term))

    def vector(self, term: str, index: Index) -> tuple[I64, F64]:
        packed = index.packed
        ids = packed.doc_ids(term).astype(np.int64)
        tf = packed.tfs(term).astype(np.float64)
        return ids, (1.0 + np.log(tf)) * math.log(index.num_docs / index.df(term))


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

    def vector(self, term: str, index: Index) -> tuple[I64, F64]:
        # The same formula as __call__, one array operation per step instead of one
        # interpreter loop iteration per posting; operation order matches exactly, so
        # the float64 results are bit-for-bit identical.
        packed = index.packed
        n, df = index.num_docs, packed.df(term)
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        ids = packed.doc_ids(term).astype(np.int64)
        tf = packed.tfs(term).astype(np.float64)
        norm = 1.0 - self.b + self.b * index.doc_len_array[ids] / index.avg_doc_length
        return ids, idf * tf * (self.k1 + 1.0) / (tf + self.k1 * norm)


SCORERS: dict[str, Scorer] = {"bm25": BM25(), "tfidf": TfIdf()}
