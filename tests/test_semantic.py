"""Semantic/hybrid search with a fake embedder: no model download, deterministic vectors."""

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from fastapi.testclient import TestClient

from findex.index import Index
from findex.search import search_mode
from findex.semantic import SemanticIndex, SemanticUnavailable, chunk_text, rrf
from findex.web.app import create_app, get_optional_index, state
from findex.web.settings import Settings

VOCAB = ["whale", "sea", "ship", "python", "loop", "кіт"]


class FakeEmbedder:
    """Bag-of-words over VOCAB: enough geometry to test ranking plumbing."""

    def query_embed(self, text: str) -> Iterator[npt.NDArray[np.float32]]:
        vec = np.array([text.lower().count(w) for w in VOCAB], dtype=np.float32) + 1e-3
        yield vec


def fake_semantic(index: Index) -> SemanticIndex:
    rows: list[npt.NDArray[np.float32]] = []
    doc_of: list[int] = []
    pos_of: list[int] = []
    for doc_id in sorted(index.doc_ids):
        for pos, chunk in enumerate(chunk_text(index.text(doc_id), words=8)):
            vec = np.array([chunk.lower().count(w) for w in VOCAB], dtype=np.float32) + 1e-3
            rows.append(vec / np.linalg.norm(vec))
            doc_of.append(doc_id)
            pos_of.append(pos)
    sem = SemanticIndex(
        np.vstack(rows), np.array(doc_of, np.int32), np.array(pos_of, np.int32), "fake"
    )
    sem._embedder = FakeEmbedder()  # pyright: ignore[reportPrivateUsage]
    return sem


def test_chunk_text_prefixes_title():
    chunks = chunk_text("My Book\n\n" + " ".join(f"w{i}" for i in range(450)), words=200)
    assert len(chunks) == 3 and all(c.startswith("My Book. ") for c in chunks)
    assert chunk_text("") == [] and chunk_text("Only a title") == ["Only a title"]


def test_rrf_uses_ranks_not_scores():
    fused = rrf([[1, 2, 3], [3, 1]])
    assert [d for d, _ in fused] == [1, 3, 2]  # 1: 1/61+1/62 > 3: 1/63+1/61 > 2: 1/62
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)


def test_semantic_search_best_chunk_per_doc(fixture_index: Index):
    sem = fake_semantic(fixture_index)
    hits = sem.search("whale whale", k=3)
    assert hits[0][0] == 0  # the whale document
    assert len({d for d, _, _ in hits}) == len(hits)  # one entry per document


def test_search_modes(fixture_index: Index):
    sem = fake_semantic(fixture_index)
    total_kw, kw = search_mode(fixture_index, "sea", "keyword")
    total_sem, se = search_mode(fixture_index, "sea", "semantic", semantic=sem, k=3)
    total_hy, hy = search_mode(fixture_index, "sea OR whale", "hybrid", semantic=sem, k=3)
    assert total_kw == 3 and kw[0].doc_id == 1
    assert total_sem == 8 and all("sea" in fixture_index.text(r.doc_id) for r in se)
    assert hy and hy[0].score > 0 and total_hy >= 3
    assert all(r.snippet is not None for r in se + hy)
    with pytest.raises(SemanticUnavailable):
        search_mode(fixture_index, "sea", "semantic", semantic=None)


def test_hybrid_survives_bad_keyword_syntax(fixture_index: Index):
    sem = fake_semantic(fixture_index)
    _, res = search_mode(fixture_index, "sea OR", "hybrid", semantic=sem, k=2)
    assert res  # keyword side fails to parse; semantic side still answers


def test_api_modes(fixture_index: Index, index_file: Path):
    app = create_app(Settings(index_path=index_file))
    app.dependency_overrides[get_optional_index] = lambda: fixture_index
    with TestClient(app) as c:
        state.semantic = None
        r = c.get("/search", params={"q": "sea", "mode": "semantic"})
        assert r.status_code == 400 and "embed" in r.json()["detail"]
        assert c.get("/search", params={"q": "sea", "mode": "vector"}).status_code == 422
        state.semantic = fake_semantic(fixture_index)
        r = c.get("/search", params={"q": "sea", "mode": "hybrid"})
        assert r.status_code == 200 and r.json()["scorer"] == "hybrid"
        assert 'name="mode"' in c.get("/").text  # the UI toggle appears
        state.semantic = None
