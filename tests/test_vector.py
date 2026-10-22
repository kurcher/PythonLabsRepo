"""Lab 8: the NumPy scorer must rank exactly like the Lab 3 per-posting loop."""

import math
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pytest_benchmark.fixture import BenchmarkFixture

from findex import store
from findex.corpus import Document, iter_documents
from findex.index import Index, build_index
from findex.packed import PackedPostings
from findex.scoring import BM25, Scorer, TfIdf
from findex.search import search

SCORERS: list[Scorer] = [BM25(), TfIdf(), BM25(k1=1.2, b=0.0)]


def ranking(index: Index, q: str, scorer: Scorer, engine: str, k: int = 50, offset: int = 0):
    res = search(index, q, scorer, k, offset=offset, snippets=False, engine=engine)  # type: ignore[arg-type]
    return [(r.doc_id, r.score) for r in res]


def assert_same(a: list[tuple[int, float]], b: list[tuple[int, float]]) -> None:
    assert [d for d, _ in a] == [d for d, _ in b]
    for (_, x), (_, y) in zip(a, b, strict=True):
        assert math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-12)


@pytest.mark.parametrize("scorer", SCORERS)
@pytest.mark.parametrize(
    "query", ["sea", "the", "whale OR sea OR loop", "language NOT java", "NOT the", "nothing"]
)
def test_identical_on_fixture(fixture_index: Index, scorer: Scorer, query: str):
    assert_same(
        ranking(fixture_index, query, scorer, "numpy"),
        ranking(fixture_index, query, scorer, "python"),
    )


def test_identical_after_load_and_paged(fixture_index: Index, index_file: Path):
    loaded = store.load(index_file)
    assert isinstance(loaded.postings, PackedPostings)  # the lazy, packed representation
    for page in range(3):
        assert_same(
            ranking(loaded, "sea OR the", BM25(), "numpy", k=2, offset=2 * page),
            ranking(fixture_index, "sea OR the", BM25(), "python", k=2, offset=2 * page),
        )


words = st.sampled_from(["a", "b", "c", "d", "e", "whale", "sea"])
corpora = st.lists(st.lists(words, min_size=1, max_size=40).map(" ".join), min_size=1, max_size=25)


@settings(max_examples=60, deadline=None)
@given(corpora, words, words, st.sampled_from(SCORERS))
def test_identical_on_random_corpora(texts: list[str], w1: str, w2: str, scorer: Scorer):
    index = build_index(Document(i, Path(f"{i}.txt"), t) for i, t in enumerate(texts))
    for q in (w1, f"{w1} OR {w2}", f"{w1} {w2}"):
        assert_same(ranking(index, q, scorer, "numpy"), ranking(index, q, scorer, "python"))


def test_lazy_postings_equal_eager(fixture_index: Index, index_file: Path):
    loaded = store.load(index_file)
    assert len(loaded) == len(fixture_index) and "whale" in loaded.postings
    assert loaded.postings == fixture_index.postings  # Mapping equality materializes all
    assert loaded.packed.doc_ids("sea").tolist() == [p.doc_id for p in fixture_index["sea"]]


# --- pytest-benchmark: CI fails if these regress past generous absolute bounds ----------


def mean_seconds(benchmark: BenchmarkFixture) -> float | None:
    """Mean run time, or None under --benchmark-disable (the plugin types this loosely)."""
    if benchmark.stats is None:
        return None
    return float(getattr(benchmark.stats.stats, "mean"))  # noqa: B009  # untyped cached_property


def test_bench_search(benchmark: BenchmarkFixture, fixture_index: Index):
    benchmark(search, fixture_index, "sea OR whale OR the", BM25(), 10)
    mean = mean_seconds(benchmark)
    assert mean is None or mean < 0.05  # 50 ms on 8 tiny docs = something broke


def test_bench_build_index(benchmark: BenchmarkFixture, corpus_dir: Path):
    benchmark(lambda: build_index(iter_documents(corpus_dir)))
    mean = mean_seconds(benchmark)
    assert mean is None or mean < 0.2
