"""Search over the inverted index.

    python -m findex.search index.bin 'white whale' '"call me ishmael"' [--scorer bm25|tfidf] [-v]
    python -m findex.search index.bin "whale OR shark NOT ship" --boolean [--engine merge|set]

``search`` (Lab 3) is ranked and uses the query language in ``findex.query``.
``boolean_search`` (Lab 2) is unranked and evaluated strictly left to right:
words are ANDed implicitly; ``OR`` and ``NOT`` combine the running result with
the next word: ``a b OR c NOT d`` == ``((a AND b) OR c) AND NOT d``.
"""

from __future__ import annotations

import argparse
import heapq
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from findex.decorators import timed
from findex.index import Index
from findex.memory import peak_rss, utf8_stdout
from findex.query import QuerySyntaxError, parse
from findex.scoring import BM25, SCORERS, Scorer, VectorScorer
from findex.semantic import SemanticIndex, rrf
from findex.snippets import Snippet, make_snippet
from findex.tokenize import tokenize

log = logging.getLogger(__name__)

Engine = Literal["merge", "set"]


# --- two-pointer merges over sorted doc_id lists: O(len(a) + len(b)) -------------


def intersect(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a) and j < len(b):
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            i += 1
        else:
            j += 1
    return out


def union(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a) and j < len(b):
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            out.append(y)
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def difference(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a):
        if j == len(b) or a[i] < b[j]:
            out.append(a[i])
            i += 1
        elif a[i] == b[j]:
            i += 1
            j += 1
        else:
            j += 1
    return out


def set_intersect(a: Sequence[int], b: Sequence[int]) -> list[int]:
    return sorted(set(a) & set(b))


def set_union(a: Sequence[int], b: Sequence[int]) -> list[int]:
    return sorted(set(a) | set(b))


def set_difference(a: Sequence[int], b: Sequence[int]) -> list[int]:
    return sorted(set(a) - set(b))


type SetOp = Callable[[Sequence[int], Sequence[int]], list[int]]
ENGINES: dict[Engine, tuple[SetOp, SetOp, SetOp]] = {
    "merge": (intersect, union, difference),
    "set": (set_intersect, set_union, set_difference),
}


def doc_ids(index: Index, word: str) -> list[int]:
    """Doc ids containing ``word``; a word that tokenizes to several terms ANDs them."""
    result: list[int] | None = None
    for term in tokenize(word):
        ids = [p.doc_id for p in index.postings.get(term, ())]
        result = ids if result is None else intersect(result, ids)
    return result or []


def boolean_search(index: Index, query: str, engine: Engine = "merge") -> list[int]:
    and_, or_, not_ = ENGINES[engine]
    result: list[int] | None = None
    op = and_
    for word in query.split():
        if word == "OR":
            op = or_
            continue
        if word == "NOT":
            op = not_
            continue
        ids = doc_ids(index, word)
        if result is None:
            result = difference(sorted(index.doc_meta), ids) if op is not_ else ids
        else:
            result = op(result, ids)
        op = and_
    return result or []


@dataclass(frozen=True, slots=True, order=True)
class SearchResult:
    """Ordered by score first, so ``sorted(results, reverse=True)`` is the ranking."""

    score: float
    doc_id: int
    title: str = field(compare=False)
    snippet: Snippet | None = field(default=None, compare=False)


Engine2 = Literal["numpy", "python"]


def _top_python(
    index: Index, terms: list[str], matched: frozenset[int], scorer: Scorer, n: int
) -> list[tuple[int, float]]:
    """Lab 3: a dict accumulator and one scorer call per posting."""
    scores: defaultdict[int, float] = defaultdict(float)
    for term in terms:
        for posting in index.get(term, ()):
            if posting.doc_id in matched:
                scores[posting.doc_id] += scorer(term, posting, index)
    for doc_id in matched:  # matched only through OR/NOT branches with no scored term
        scores.setdefault(doc_id, 0.0)
    # ties broken by doc_id so both engines produce the same order
    return heapq.nlargest(n, scores.items(), key=lambda kv: (kv[1], -kv[0]))


def _top_numpy(
    index: Index, terms: list[str], matched: frozenset[int], scorer: VectorScorer, n: int
) -> list[tuple[int, float]]:
    """Lab 8: a dense score array, one vector expression per term, argpartition top-n."""
    import numpy as np

    scores = np.zeros(len(index.doc_len_array), dtype=np.float64)
    for term in terms:
        if term in index:
            ids, weights = scorer.vector(term, index)
            scores[ids] += weights  # ids are unique within one postings list
    cand = np.fromiter(matched, dtype=np.int64, count=len(matched))
    cand_scores = scores[cand]
    if len(cand) > n:  # O(len) selection instead of an O(len log len) sort
        part = np.argpartition(-cand_scores, n - 1)[:n]
        cand, cand_scores = cand[part], cand_scores[part]
    order = np.lexsort((cand, -cand_scores))  # by score desc, then doc_id asc
    return [(int(d), float(s)) for d, s in zip(cand[order], cand_scores[order], strict=True)]


@timed
def search(
    index: Index,
    query: str,
    scorer: Scorer | None = None,
    k: int = 10,
    *,
    offset: int = 0,
    snippets: bool = True,
    engine: Engine2 = "numpy",
) -> list[SearchResult]:
    """Ranked search: the query tree picks the matching set, ``scorer`` orders it.

    ``engine="numpy"`` (default) scores whole postings lists as arrays when the scorer
    supports it; ``engine="python"`` is the Lab 3 per-posting loop. Rankings are
    identical (tested).
    """
    scorer = scorer or BM25()
    node = parse(query)
    matched = index.matching(query)  # lru_cache'd: a repeated query skips parse+evaluate
    log.debug("matching cache: %s", index.matching.cache_info())
    terms = list(dict.fromkeys(node.terms()))  # dedupe, keep order
    n = offset + k  # page n needs the top offset+k
    if not matched:
        top: list[tuple[int, float]] = []
    elif engine == "numpy" and isinstance(scorer, VectorScorer):
        top = _top_numpy(index, terms, matched, scorer, n)
    else:
        top = _top_python(index, terms, matched, scorer, n)
    return [
        SearchResult(
            score,
            doc_id,
            index.title(doc_id),
            make_snippet(index.text(doc_id), terms) if snippets else None,
        )
        for doc_id, score in top[offset:]
    ]


Mode = Literal["keyword", "semantic", "hybrid"]
POOL = 100  # semantic/hybrid rank the best POOL documents; pages come from that pool
_OPERATORS = re.compile(r'\b(?:AND|OR|NOT)\b|["()]')


def _semantic_snippet(index: Index, doc_id: int, chunk_pos: int, terms: list[str]) -> Snippet:
    from findex.semantic import chunk_text

    chunks = chunk_text(index.text(doc_id))
    piece = chunks[chunk_pos] if chunk_pos < len(chunks) else index.text(doc_id)
    first_line = index.text(doc_id).partition("\n")[0].strip()
    body = piece.removeprefix(f"{first_line}. ")  # every chunk carries "<title>. " in front
    # leading newline: make_snippet treats the first line as a title and skips it
    return make_snippet("\n" + (body or piece), terms)


def search_mode(
    index: Index,
    query: str,
    mode: Mode = "keyword",
    scorer: Scorer | None = None,
    k: int = 10,
    *,
    offset: int = 0,
    semantic: SemanticIndex | None = None,
) -> tuple[int, list[SearchResult]]:
    """``(total, page of results)`` for keyword (BM25/TF-IDF), semantic or hybrid search.

    Hybrid = Reciprocal Rank Fusion of the keyword top-``POOL`` and the semantic
    top-``POOL``: it uses ranks only, so the two incomparable score scales never mix.
    """
    if mode == "keyword":
        return len(index.matching(query)), search(index, query, scorer, k, offset=offset)
    if semantic is None:
        from findex.semantic import SemanticUnavailable

        raise SemanticUnavailable("no embeddings loaded for this index (run `findex embed`)")
    text = " ".join(_OPERATORS.sub(" ", query).split()) or query
    # highlight only informative words: "by"/"an" in a semantic snippet is noise
    terms = [t for t in dict.fromkeys(tokenize(text)) if index.df(t) < 0.3 * index.num_docs]
    sem = semantic.search(text, POOL)
    best_chunk = {doc_id: pos for doc_id, _, pos in sem}
    if mode == "semantic":
        page = sem[offset : offset + k]
        return len(sem), [
            SearchResult(score, d, index.title(d), _semantic_snippet(index, d, pos, terms))
            for d, score, pos in page
        ]
    try:
        keyword = search(index, query, scorer, POOL, snippets=False)
    except QuerySyntaxError:
        keyword = []  # an unparsable query can still be answered semantically
    fused = rrf([[r.doc_id for r in keyword], [d for d, _, _ in sem]])
    in_keyword = {r.doc_id for r in keyword}
    results: list[SearchResult] = []
    for doc_id, score in fused[offset : offset + k]:
        if doc_id in in_keyword:  # the words are in there: a keyword snippet highlights them
            snip = make_snippet(index.text(doc_id), terms)
        else:
            snip = _semantic_snippet(index, doc_id, best_chunk.get(doc_id, 0), terms)
        results.append(SearchResult(score, doc_id, index.title(doc_id), snip))
    return len(fused), results


def main(argv: list[str] | None = None) -> None:
    from findex import store

    ap = argparse.ArgumentParser(prog="python -m findex.search")
    ap.add_argument("index", type=Path)
    ap.add_argument("query", nargs="+", help="one or more queries (repeat one to see the cache)")
    ap.add_argument("--scorer", choices=sorted(SCORERS), default="bm25")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--boolean", action="store_true", help="Lab 2 unranked boolean search")
    ap.add_argument("--engine", choices=("merge", "set"), default="merge")
    ap.add_argument("-v", "--verbose", action="store_true", help="log timings")
    args = ap.parse_args(argv)
    utf8_stdout()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    t0 = time.perf_counter()
    index = store.load(args.index)
    print(f"{index!r} loaded in {time.perf_counter() - t0:.2f} s")

    for query in args.query:
        t0 = time.perf_counter()
        if args.boolean:
            hits = boolean_search(index, query, engine=args.engine)
            took = time.perf_counter() - t0
            print(f"{len(hits)} documents match {query!r} ({took * 1000:.2f} ms, {args.engine})")
            for doc_id in hits[: args.k]:
                print(f"  [{doc_id:5d}] {index.title(doc_id)}")
            continue
        results = search(index, query, SCORERS[args.scorer], args.k)
        took = time.perf_counter() - t0
        print(f"top {len(results)} for {query!r} ({args.scorer}, {took * 1000:.1f} ms)")
        for rank, r in enumerate(results, 1):
            print(f"{rank:3d}. {r.score:7.3f}  [{r.doc_id}] {r.title}")
            if r.snippet:
                print(f"       {r.snippet.render(ANSI_ON, ANSI_OFF)}")
    print(f"peak memory: {peak_rss() / 2**20:.0f} MiB (peak RSS)")


ANSI_ON, ANSI_OFF = chr(27) + "[1;33m", chr(27) + "[0m"


if __name__ == "__main__":
    main()
