"""Hypothesis properties: statements that must hold for *every* input, not just examples."""

import unicodedata
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from findex import store
from findex.corpus import Document
from findex.index import build_index
from findex.query import parse
from findex.search import difference, intersect, union
from findex.tokenize import tokenize

sorted_ids = st.lists(st.integers(0, 500), unique=True, max_size=60).map(sorted)


@given(sorted_ids, sorted_ids)
def test_merges_equal_set_algebra(a: list[int], b: list[int]):
    assert intersect(a, b) == sorted(set(a) & set(b))
    assert union(a, b) == sorted(set(a) | set(b))
    assert difference(a, b) == sorted(set(a) - set(b))


@given(sorted_ids, sorted_ids)
def test_merge_output_is_sorted_and_unique(a: list[int], b: list[int]):
    for out in (intersect(a, b), union(a, b), difference(a, b)):
        assert out == sorted(set(out))


@given(st.text())
def test_tokens_are_normalized_fixed_points(text: str):
    for tok in tokenize(text):
        assert tok
        assert tok == unicodedata.normalize("NFC", tok)
        assert list(tokenize(tok)) == [tok]  # re-tokenizing a token yields exactly itself


@given(st.text())
def test_tokenize_is_idempotent_on_its_output(text: str):
    tokens = list(tokenize(text))
    assert list(tokenize(" ".join(tokens))) == tokens


words = st.sampled_from(["whale", "sea", "ship", "кіт", "п'ять", "loop", "event", "x1"])
corpora = st.lists(st.lists(words, max_size=30).map(" ".join), min_size=1, max_size=8)


@pytest.mark.slow
@settings(max_examples=40, deadline=None)
@given(corpora, st.booleans(), st.sampled_from(store.FORMATS))
def test_store_round_trip(texts: list[str], positions: bool, fmt: store.Format):
    import tempfile

    docs = [Document(i, Path(f"d{i}.txt"), t) for i, t in enumerate(texts)]
    index = build_index(docs, positions=positions)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ix"
        store.save(index, path, fmt=fmt)
        loaded = store.load(path)
    assert loaded == index  # Mapping equality: same terms, same postings
    assert loaded.doc_lengths == index.doc_lengths
    assert loaded.doc_meta == index.doc_meta


@settings(max_examples=60, deadline=None)
@given(corpora, words, words)
def test_query_algebra_matches_set_algebra(texts: list[str], w1: str, w2: str):
    docs = [Document(i, Path(f"d{i}.txt"), t) for i, t in enumerate(texts)]
    index = build_index(docs)
    a, b = parse(w1).evaluate(index), parse(w2).evaluate(index)
    assert parse(f"{w1} {w2}").evaluate(index) == a & b
    assert parse(f"{w1} OR {w2}").evaluate(index) == a | b
    assert parse(f"{w1} NOT {w2}").evaluate(index) == a - b
    assert parse(f"NOT {w1}").evaluate(index) == frozenset(index.doc_ids) - a


snippet_alphabet = st.sampled_from(
    [*list("whaleWHALEsea ship'’ʼ-_.,!\n"), "кіт", "Кіт", "п'ять", "e" + chr(769), "ß", "SS"]
)
snippet_terms = st.frozensets(
    st.sampled_from(["whale", "sea", "кіт", "п'ять", "ss", "ship", "whale's"]), max_size=4
)


@settings(max_examples=300, deadline=None)
@given(st.lists(snippet_alphabet, max_size=60).map("".join), snippet_terms)
def test_fast_snippet_spans_equal_reference(text: str, terms: frozenset[str]):
    """Lab 8: the regex fast path must find exactly what tokenizing every word finds."""
    from findex.snippets import find_term_spans, find_term_spans_slow

    text = unicodedata.normalize("NFC", text)
    assert find_term_spans(text, terms) == find_term_spans_slow(text, terms)


def test_fast_snippet_regressions():
    """Counterexamples Hypothesis found while building the fast path, pinned forever."""
    from findex.snippets import find_term_spans, find_term_spans_slow

    for text, terms in [("ß", {"ss"}), ("ʼß", {"ss"}), ("whale's whale", {"whale"})]:
        assert find_term_spans(text, frozenset(terms)) == find_term_spans_slow(
            text, frozenset(terms)
        )


@given(st.text())
def test_count_terms_equals_counter_of_tokenize(text: str):
    from collections import Counter

    from findex.tokenize import count_terms

    assert count_terms(text) == Counter(tokenize(text))
