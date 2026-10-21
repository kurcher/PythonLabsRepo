"""The inverted index: term -> postings sorted by doc_id.

python -m findex.index data/ --out index.bin [--positions] [--format binary|pickle|json]
"""

from __future__ import annotations

import argparse
import functools
import time
from array import array
from collections import Counter, defaultdict
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from itertools import islice
from pathlib import Path
from typing import Literal

from findex.corpus import Document, iter_documents
from findex.decorators import timed
from findex.memory import peak_rss, utf8_stdout
from findex.tokenize import tokenize


@dataclass(frozen=True, slots=True)
class Posting:
    doc_id: int
    tf: int
    positions: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DocMeta:
    doc_id: int
    path: str
    title: str


@dataclass(frozen=True)
class PlainPosting:
    """Same fields as ``Posting`` but with a per-instance ``__dict__`` (memory study only)."""

    doc_id: int
    tf: int
    positions: tuple[int, ...] = ()


PostingsRepr = Literal["slots", "plain", "array"]


class Index(Mapping[str, list[Posting]]):
    """Read-only mapping ``term -> postings`` plus the per-document statistics ranking needs.

    Inheriting ``Mapping`` turns the five methods below into ``get``, ``keys``, ``items``,
    ``values`` and ``==`` for free.
    """

    def __init__(
        self,
        postings: dict[str, list[Posting]] | None = None,
        doc_lengths: dict[int, int] | None = None,
        doc_meta: dict[int, DocMeta] | None = None,
        *,
        has_positions: bool = False,
    ) -> None:
        self.postings: dict[str, list[Posting]] = postings if postings is not None else {}
        self.doc_lengths: dict[int, int] = doc_lengths if doc_lengths is not None else {}
        self.doc_meta: dict[int, DocMeta] = doc_meta if doc_meta is not None else {}
        self.has_positions = has_positions
        self._init_cache()

    # --- the mapping protocol -------------------------------------------------
    def __getitem__(self, term: str) -> list[Posting]:
        return self.postings[term]  # KeyError for an unknown term, as a Mapping must

    def __len__(self) -> int:
        return len(self.postings)

    def __iter__(self) -> Iterator[str]:
        return iter(self.postings)

    def __contains__(self, term: object) -> bool:
        return term in self.postings  # one dict lookup instead of Mapping's try/__getitem__

    def __repr__(self) -> str:
        pos = ", positions" if self.has_positions else ""
        return f"<Index {len(self):,} terms, {self.num_docs:,} docs{pos}>"

    # --- statistics -----------------------------------------------------------
    @property
    def num_docs(self) -> int:
        return len(self.doc_meta)

    @property
    def doc_ids(self) -> Iterable[int]:
        return self.doc_meta.keys()

    @cached_property
    def avg_doc_length(self) -> float:
        return sum(self.doc_lengths.values()) / max(1, len(self.doc_lengths))

    def doc_length(self, doc_id: int) -> int:
        return self.doc_lengths[doc_id]

    def df(self, term: str) -> int:
        return len(self.postings.get(term, ()))

    def title(self, doc_id: int) -> str:
        return self.doc_meta[doc_id].title

    def text(self, doc_id: int) -> str:
        """Full text of a document, re-read from the corpus (the index stores no text)."""
        from findex.corpus import read_document

        return read_document(self.doc_meta[doc_id])

    # --- cached query matching (Lab 3, M4) -------------------------------------
    def _init_cache(self) -> None:
        # A per-instance lru_cache: the Index itself is unhashable (Mapping sets
        # __hash__ = None), so it cannot be an argument of a module-level cached function.
        self.matching = functools.lru_cache(maxsize=256)(self._matching)

    def _matching(self, query: str) -> frozenset[int]:
        from findex.query import parse

        return parse(query).evaluate(self)

    def __getstate__(self) -> dict[str, object]:
        state = self.__dict__.copy()
        state.pop("matching", None)  # an lru_cache wrapper doesn't pickle
        state.pop("avg_doc_length", None)
        return state

    def __setstate__(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        self._init_cache()

    def close(self) -> None:
        """Drop the big structures; the object is unusable afterwards."""
        self.matching.cache_clear()
        self.postings = {}
        self.doc_lengths = {}
        self.doc_meta = {}
        self.__dict__.pop("avg_doc_length", None)


@contextmanager
def open_index(path: Path) -> Generator[Index]:
    """``with open_index(p) as ix:`` — everything before ``yield`` is ``__enter__``,
    the ``finally`` is ``__exit__`` and runs even if the body raises."""
    from findex import store

    index = store.load(path)
    try:
        yield index
    finally:
        index.close()


def title_of(doc: Document) -> str:
    for line in doc.text.splitlines():
        if line.strip():
            return line.strip()[:120]
    return doc.path.name


def doc_terms(text: str, positions: bool) -> tuple[dict[str, list[int]] | Counter[str], int]:
    """Per-document term statistics: ``Counter`` of tf, or term -> list of positions."""
    if not positions:
        counts = Counter(tokenize(text))
        return counts, counts.total()
    where: defaultdict[str, list[int]] = defaultdict(list)
    n = -1
    for n, tok in enumerate(tokenize(text)):
        where[tok].append(n)
    return where, n + 1


@timed
def build_index(docs: Iterable[Document], *, positions: bool = False) -> Index:
    """One streaming pass: flush each document's Counter into the postings, then drop it.

    Documents arrive in increasing ``doc_id`` order, so appending keeps every
    postings list sorted without a separate sort.
    """
    postings: defaultdict[str, list[Posting]] = defaultdict(list)
    index = Index(has_positions=positions)
    for doc in docs:
        terms, length = doc_terms(doc.text, positions)
        for term, value in terms.items():
            if isinstance(value, int):
                postings[term].append(Posting(doc.doc_id, value))
            else:
                postings[term].append(Posting(doc.doc_id, len(value), tuple(value)))
        index.doc_lengths[doc.doc_id] = length
        index.doc_meta[doc.doc_id] = DocMeta(doc.doc_id, doc.locator, title_of(doc))
    index.postings = dict(postings)
    return index


def build_postings_variant(docs: Iterable[Document], variant: PostingsRepr) -> dict[str, object]:
    """Postings only, in one of three representations — for the memory study (M4)."""
    if variant == "array":
        arrays: defaultdict[str, tuple[array[int], array[int]]] = defaultdict(
            lambda: (array("I"), array("I"))
        )
        for doc in docs:
            for term, tf in Counter(tokenize(doc.text)).items():
                ids, tfs = arrays[term]
                ids.append(doc.doc_id)
                tfs.append(tf)
        return dict(arrays)
    cls = Posting if variant == "slots" else PlainPosting
    lists: defaultdict[str, list[object]] = defaultdict(list)
    for doc in docs:
        for term, tf in Counter(tokenize(doc.text)).items():
            lists[term].append(cls(doc.doc_id, tf))
    return dict(lists)


def main(argv: list[str] | None = None) -> None:
    from findex import store

    ap = argparse.ArgumentParser(prog="python -m findex.index")
    ap.add_argument("root", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--positions", action="store_true", help="record token positions")
    ap.add_argument("--format", choices=store.FORMATS, default="binary")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    docs = iter_documents(args.root)
    if args.limit is not None:
        docs = islice(docs, args.limit)

    t0 = time.perf_counter()
    index = build_index(docs, positions=args.positions)
    t_build = time.perf_counter() - t0

    t0 = time.perf_counter()
    store.save(index, args.out, fmt=args.format)
    t_save = time.perf_counter() - t0

    utf8_stdout()
    print(f"documents: {index.num_docs:,}  terms: {len(index.postings):,}")
    print(f"build: {t_build:.2f} s  save: {t_save:.2f} s")
    print(f"-> {args.out} ({args.out.stat().st_size / 2**20:.1f} MiB)")
    print(f"peak memory: {peak_rss() / 2**20:.0f} MiB (peak RSS)")


if __name__ == "__main__":
    main()
