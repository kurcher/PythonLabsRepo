from pathlib import Path

import pytest

from findex import store
from findex.corpus import iter_documents
from findex.index import Index, Posting, build_index, open_index
from findex.query import QuerySyntaxError
from findex.scoring import BM25, TfIdf
from findex.search import SearchResult, search
from findex.snippets import make_snippet

CORPUS = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture(scope="module")
def index() -> Index:
    return build_index(iter_documents(CORPUS), positions=True)


def test_index_protocol(index: Index):
    assert len(index) == len(index.postings)
    assert "whale" in index and "zzz" not in index
    assert index["whale"][0].doc_id == 0
    with pytest.raises(KeyError):
        index["zzz"]
    assert set(iter(index)) == set(index.postings)
    assert repr(index).startswith("<Index ") and "8 docs" in repr(index)
    assert index.df("sea") == 3
    assert index.avg_doc_length == sum(index.doc_lengths.values()) / 8


def test_open_index_releases_on_exception(index: Index, tmp_path: Path):
    path = tmp_path / "ix.bin"
    store.save(index, path)
    with pytest.raises(RuntimeError), open_index(path) as ix:
        held = ix
        assert len(ix) > 0
        raise RuntimeError("boom")
    assert len(held) == 0  # close() ran in the finally


@pytest.mark.parametrize("scorer", [BM25(), TfIdf()])
def test_rare_term_outranks_common_term(index: Index, scorer):
    # "whale" is in 1 doc, "the" in most: the whale doc must come first
    results = search(index, "whale OR the", scorer, k=10, snippets=False)
    assert results[0].doc_id == 0


def test_bm25_saturates(index: Index):
    bm25 = BM25()
    avg = round(index.avg_doc_length)
    index.doc_lengths[999] = avg  # a synthetic document of average length
    try:
        s = [bm25("walrus", Posting(999, tf), index) for tf in (1, 2, 10, 19, 20)]
    finally:
        del index.doc_lengths[999]
    assert s[1] - s[0] > 10 * (s[4] - s[3])  # 2nd occurrence adds far more than the 20th
    assert s[4] / s[0] < bm25.k1 + 1  # score(tf) is bounded by idf * (k1 + 1)


def test_short_doc_outranks_long_doc(index: Index):
    # "platypus" occurs once in 06-short.txt and once in the ~1,800-token 07-long.txt
    results = search(index, "platypus", BM25(), snippets=False)
    assert [r.doc_id for r in results] == [5, 6]
    tfidf = search(index, "platypus", TfIdf(), snippets=False)
    assert tfidf[0].score == tfidf[1].score  # TF-IDF ignores length entirely


def test_phrase_and_not(index: Index):
    hits = [r.doc_id for r in search(index, '"event loop"', snippets=False)]
    assert hits == [2]  # doc 3 has both words but not adjacent
    assert [r.doc_id for r in search(index, "language NOT java", snippets=False)] == [2]


def test_phrase_needs_positions():
    ix = build_index(iter_documents(CORPUS), positions=False)
    with pytest.raises(QuerySyntaxError):
        search(ix, '"event loop"')


def test_cache_hit(index: Index):
    index.matching.cache_clear()
    search(index, "sea ship", snippets=False)
    search(index, "sea ship", snippets=False)
    info = index.matching.cache_info()
    assert info.hits == 1 and info.misses == 1


def test_results_sort_by_score():
    a, b = SearchResult(1.0, 5, "a"), SearchResult(2.0, 3, "b")
    assert sorted([a, b], reverse=True) == [b, a]


def test_snippet_highlights_terms():
    text = "Intro. " + "x " * 200 + "Call me Ishmael. The White Whale swam. " + "y " * 200
    snip = make_snippet(text, ["whale", "white"])
    assert "**White** **Whale**" in snip.render()
    assert snip.text.startswith("…") and snip.text.endswith("…")
    assert "<mark>White</mark>" in snip.render_html()
