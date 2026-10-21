"""Parallel indexing: build partial indexes over chunks of paths, then merge.

The unit of work is a *list of paths*, not loaded documents: a worker opens and
tokenizes its own files, so only paths go in and compact arrays come out —
that keeps the pickling tax of ``ProcessPoolExecutor`` small.
"""

from __future__ import annotations

import logging
import time
from array import array
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from findex.corpus import Document, iter_file, iter_paths
from findex.decorators import timed
from findex.index import DocMeta, Index, Posting, doc_terms, title_of

log = logging.getLogger(__name__)

ExecutorName = Literal["serial", "threads", "processes"]
EXECUTORS: tuple[ExecutorName, ...] = ("serial", "threads", "processes")


@dataclass(slots=True)
class PartialIndex:
    """Postings for one chunk, with *chunk-local* doc ids (0, 1, 2, ...).

    Arrays instead of ``Posting`` objects: ~8 bytes per posting to pickle back to
    the parent instead of ~60, and no per-object pickling opcodes.
    """

    postings: dict[str, tuple[array[int], array[int], array[int]]] = field(
        default_factory=dict[str, tuple[array[int], array[int], array[int]]]
    )
    doc_lengths: list[int] = field(default_factory=list[int])
    doc_meta: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # (loc, title)
    cpu_seconds: float = 0.0  # CPU time spent in this worker, for the benchmark

    @property
    def num_docs(self) -> int:
        return len(self.doc_lengths)


def build_partial(paths: Sequence[Path], positions: bool = False) -> PartialIndex:
    """Index one chunk of corpus files. Module-level so a process pool can pickle it."""
    cpu0 = time.thread_time()  # per-thread CPU: correct under threads *and* processes
    acc: defaultdict[str, tuple[array[int], array[int], array[int]]] = defaultdict(
        lambda: (array("I"), array("I"), array("I"))
    )
    part = PartialIndex()
    local_id = 0
    for path in paths:
        for doc_path, text, line in iter_file(path):
            doc = Document(local_id, doc_path, text, line)
            terms, length = doc_terms(text, positions)
            for term, value in terms.items():
                ids, tfs, pos = acc[term]
                ids.append(local_id)
                if isinstance(value, int):
                    tfs.append(value)
                else:
                    tfs.append(len(value))
                    pos.extend(value)
            part.doc_lengths.append(length)
            part.doc_meta.append((doc.locator, title_of(doc)))
            local_id += 1
    part.postings = dict(acc)
    part.cpu_seconds = time.thread_time() - cpu0
    return part


@timed
def merge(partials: Iterable[PartialIndex], *, positions: bool = False) -> Index:
    """Concatenate partials in chunk order, shifting local ids by a running offset.

    Chunk ``i`` owns the id range ``[offset_i, offset_i + num_docs_i)``; because the
    ranges are increasing, appending each chunk's postings keeps every list sorted —
    the "sorted merge" degenerates to a concatenation.
    """
    postings: defaultdict[str, list[Posting]] = defaultdict(list)
    index = Index(has_positions=positions)
    offset = 0
    for part in partials:
        for term, (ids, tfs, pos) in part.postings.items():
            plist = postings[term]
            if positions:
                start = 0
                for local, tf in zip(ids, tfs, strict=True):
                    plist.append(Posting(local + offset, tf, tuple(pos[start : start + tf])))
                    start += tf
            else:
                plist.extend(
                    Posting(local + offset, tf) for local, tf in zip(ids, tfs, strict=True)
                )
        for local, (length, (locator, title)) in enumerate(
            zip(part.doc_lengths, part.doc_meta, strict=True)
        ):
            index.doc_lengths[local + offset] = length
            index.doc_meta[local + offset] = DocMeta(local + offset, locator, title)
        offset += part.num_docs
    index.postings = dict(postings)
    return index


def chunked[T](items: Sequence[T], n_chunks: int) -> list[Sequence[T]]:
    """Split into ``n_chunks`` contiguous, nearly equal slices (order preserved)."""
    n_chunks = max(1, min(n_chunks, len(items)))
    size, extra = divmod(len(items), n_chunks)
    out: list[Sequence[T]] = []
    start = 0
    for i in range(n_chunks):
        end = start + size + (1 if i < extra else 0)
        out.append(items[start:end])
        start = end
    return out


def make_executor(name: ExecutorName, workers: int) -> Executor | None:
    if name == "threads":
        return ThreadPoolExecutor(max_workers=workers)
    if name == "processes":
        return ProcessPoolExecutor(max_workers=workers)
    return None


@dataclass(slots=True)
class BuildReport:
    index: Index
    wall: float
    merge: float
    worker_cpu: float


def build_parallel(
    paths: Sequence[Path],
    *,
    executor: ExecutorName = "processes",
    workers: int = 4,
    positions: bool = False,
    chunks_per_worker: int = 4,
    on_chunk: Callable[[int], None] | None = None,
) -> BuildReport:
    """Build an index over ``paths`` with the chosen executor.

    ``executor.map`` yields results in submission order, which is what makes the
    output identical to the serial build. If a worker raises, ``map`` re-raises
    that exception here (for processes, with the worker's traceback attached as
    ``__cause__``) and the ``with`` block shuts the pool down — no hang.
    """
    t0 = time.perf_counter()
    chunks = chunked(paths, workers * chunks_per_worker if executor != "serial" else 1)
    pool = make_executor(executor, workers)
    partials: list[PartialIndex] = []
    if pool is None:
        results: Iterable[PartialIndex] = (build_partial(chunk, positions) for chunk in chunks)
        for part in results:
            partials.append(part)
            if on_chunk:
                on_chunk(part.num_docs)
    else:
        with pool:
            for part in pool.map(build_partial, chunks, [positions] * len(chunks)):
                partials.append(part)
                if on_chunk:
                    on_chunk(part.num_docs)
    t_merge = time.perf_counter()
    index = merge(partials, positions=positions)
    t_end = time.perf_counter()
    log.info(
        "%s x%d: %d chunks, build %.2f s, merge %.2f s",
        executor,
        workers,
        len(chunks),
        t_merge - t0,
        t_end - t_merge,
    )
    return BuildReport(index, t_end - t0, t_end - t_merge, sum(p.cpu_seconds for p in partials))


def corpus_paths(root: Path, limit: int | None = None) -> list[Path]:
    paths = list(iter_paths(root))
    return paths[:limit] if limit is not None else paths
