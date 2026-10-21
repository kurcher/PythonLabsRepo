import random
from pathlib import Path

from findex import store
from findex.corpus import iter_documents
from findex.index import DocMeta, Posting, build_index
from findex.search import boolean_search, difference, intersect, union

CORPUS = Path(__file__).parent / "fixtures" / "corpus"


def test_postings_sorted_and_counted():
    index = build_index(iter_documents(CORPUS))
    assert index.num_docs == 8
    for plist in index.postings.values():
        ids = [p.doc_id for p in plist]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
    whale = index.postings["whale"]
    assert [(p.doc_id, p.tf) for p in whale] == [(0, 3)]
    assert sum(index.doc_lengths.values()) == sum(
        p.tf for pl in index.postings.values() for p in pl
    )


def test_positions_match_tf():
    index = build_index(iter_documents(CORPUS), positions=True)
    for plist in index.postings.values():
        for p in plist:
            assert len(p.positions) == p.tf
            assert list(p.positions) == sorted(p.positions)


def test_dataclasses_are_hashable_and_frozen():
    a, b = Posting(1, 2), Posting(1, 2)
    assert {a, b} == {a}
    assert hash(DocMeta(0, "x", "t")) == hash(DocMeta(0, "x", "t"))
    assert not hasattr(a, "__dict__")


def test_merges_agree_with_sets():
    rng = random.Random(42)
    for _ in range(200):
        a = sorted(rng.sample(range(100), rng.randint(0, 30)))
        b = sorted(rng.sample(range(100), rng.randint(0, 30)))
        assert intersect(a, b) == sorted(set(a) & set(b))
        assert union(a, b) == sorted(set(a) | set(b))
        assert difference(a, b) == sorted(set(a) - set(b))


def test_boolean_queries_both_engines():
    index = build_index(iter_documents(CORPUS))
    for engine in ("merge", "set"):
        assert boolean_search(index, "event loop", engine) == [2, 3]
        assert boolean_search(index, "event loop NOT java", engine) == [2]
        assert boolean_search(index, "whale OR walrus", engine) == [0, 7]
        assert boolean_search(index, "NOT the", engine) == [4, 5, 6]


def test_store_round_trip(tmp_path: Path):
    for positions in (False, True):
        index = build_index(iter_documents(CORPUS), positions=positions)
        for fmt in store.FORMATS:
            path = tmp_path / f"ix.{fmt}"
            store.save(index, path, fmt=fmt)
            assert store.detect_format(path) == fmt
            loaded = store.load(path)
            assert loaded.postings == index.postings
            assert loaded.doc_lengths == index.doc_lengths
            assert loaded.doc_meta == index.doc_meta
