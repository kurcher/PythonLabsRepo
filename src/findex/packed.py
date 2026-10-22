"""Packed postings: one contiguous ``uint32`` buffer, two ways to read it.

Layout (the same as the binary index file from Lab 2): for every term, at
``offset``: ``doc_ids[count]``, then ``tfs[count]``, then ``positions[npos]``.

* ``PackedPostings`` is a read-only ``Mapping[str, list[Posting]]`` that builds
  ``Posting`` objects *lazily*, only for terms a query touches — Lab 8 hotspot #2:
  loading used to create ~5 million objects up front (12 s).
* ``doc_ids(term)`` / ``tfs(term)`` are zero-copy NumPy views over the same buffer,
  which is what the vectorized scorers in ``findex.vector`` consume.
"""

from __future__ import annotations

from array import array
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from findex.index import Posting

U32 = npt.NDArray[np.uint32]


class PackedPostings(Mapping[str, "list[Posting]"]):
    def __init__(self, blob: array[int] | U32, terms: dict[str, tuple[int, int, int]]) -> None:
        self._np: U32 = np.frombuffer(blob, dtype=np.uint32) if isinstance(blob, array) else blob
        self._blob = blob  # keep the owner of the memory alive
        self.terms = terms  # term -> (offset, count, npos)
        self._cache: dict[str, list[Posting]] = {}

    # --- Mapping ------------------------------------------------------------------
    def __getitem__(self, term: str) -> list[Posting]:
        cached = self._cache.get(term)
        if cached is not None:
            return cached
        offset, count, npos = self.terms[term]  # KeyError for unknown terms
        from findex.index import Posting

        ids = self._np[offset : offset + count].tolist()
        tfs = self._np[offset + count : offset + 2 * count].tolist()
        if npos:
            pos = self._np[offset + 2 * count : offset + 2 * count + npos].tolist()
            plist: list[Posting] = []
            start = 0
            for d, tf in zip(ids, tfs, strict=True):
                plist.append(Posting(d, tf, tuple(pos[start : start + tf])))
                start += tf
        else:
            plist = [Posting(d, tf) for d, tf in zip(ids, tfs, strict=True)]
        self._cache[term] = plist
        return plist

    def __len__(self) -> int:
        return len(self.terms)

    def __iter__(self) -> Iterator[str]:
        return iter(self.terms)

    def __contains__(self, term: object) -> bool:
        return term in self.terms

    # --- vector views ----------------------------------------------------------------
    def doc_ids(self, term: str) -> U32:
        offset, count, _ = self.terms[term]
        return self._np[offset : offset + count]

    def tfs(self, term: str) -> U32:
        offset, count, _ = self.terms[term]
        return self._np[offset + count : offset + 2 * count]

    def df(self, term: str) -> int:
        entry = self.terms.get(term)
        return entry[1] if entry else 0

    @property
    def nbytes(self) -> int:
        return int(self._np.nbytes)

    def raw_bytes(self) -> bytes:
        return self._np.tobytes()


def pack(postings: Mapping[str, list[Posting]], positions: bool) -> PackedPostings:
    """Pack object postings into the contiguous layout (used for in-memory indexes)."""
    if isinstance(postings, PackedPostings):
        return postings
    blob = array("I")
    terms: dict[str, tuple[int, int, int]] = {}
    for term, plist in postings.items():
        offset = len(blob)
        blob.extend(p.doc_id for p in plist)
        blob.extend(p.tf for p in plist)
        npos = 0
        if positions:
            for p in plist:
                blob.extend(p.positions)
                npos += len(p.positions)
        terms[term] = (offset, len(plist), npos)
    return PackedPostings(blob, terms)
