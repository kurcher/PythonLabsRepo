"""API and UI tests: the fixture index is injected with ``dependency_overrides`` —
no index file and no lifespan load are involved."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from findex.index import Index
from findex.web.app import create_app, get_optional_index
from findex.web.settings import Settings


@pytest.fixture
def app(fixture_index: Index, index_file: Path) -> FastAPI:
    app = create_app(Settings(index_path=index_file))
    app.dependency_overrides[get_optional_index] = lambda: fixture_index
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:  # runs the lifespan; the override wins regardless
        yield c


def test_search_ok_and_shape(client: TestClient):
    r = client.get("/search", params={"q": "sea", "k": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3 and body["pages"] == 2 and len(body["results"]) == 2
    hit = body["results"][0]
    assert set(hit) == {"doc_id", "title", "score", "snippet", "snippet_html"}
    assert "<mark>" in hit["snippet_html"]
    assert r.headers["x-request-id"]


def test_search_pagination_is_disjoint(client: TestClient):
    p1 = client.get("/search", params={"q": "sea", "k": 2, "page": 1}).json()["results"]
    p2 = client.get("/search", params={"q": "sea", "k": 2, "page": 2}).json()["results"]
    ids1, ids2 = {h["doc_id"] for h in p1}, {h["doc_id"] for h in p2}
    assert len(ids2) == 1 and not ids1 & ids2


def test_search_scorer_switch(client: TestClient):
    bm25 = client.get("/search", params={"q": "platypus", "scorer": "bm25"}).json()["results"]
    tfidf = client.get("/search", params={"q": "platypus", "scorer": "tfidf"}).json()["results"]
    assert bm25[0]["score"] > bm25[1]["score"]  # length normalization
    assert tfidf[0]["score"] == tfidf[1]["score"]


@pytest.mark.parametrize(
    "params",
    [
        {},  # q missing
        {"q": ""},
        {"q": "x", "k": 0},
        {"q": "x", "k": 101},
        {"q": "x", "page": 0},
        {"q": "x", "scorer": "pagerank"},
        {"q": "a OR"},  # syntax error in the query language
    ],
)
def test_search_422(client: TestClient, params: dict[str, str | int]):
    r = client.get("/search", params=params)
    assert r.status_code == 422
    assert "detail" in r.json()


def test_document_ok_and_404(client: TestClient):
    r = client.get("/docs/0")
    assert r.status_code == 200 and r.json()["title"] == "The Whale"
    assert "Ishmael" in r.json()["text"]
    r = client.get("/docs/999")
    assert r.status_code == 404 and r.json() == {"detail": "no document 999"}
    assert client.get("/docs/abc").status_code == 422


def test_stats(client: TestClient):
    s = client.get("/stats").json()
    assert s["documents"] == 8 and s["positions"] is True and s["index_bytes"] > 0


def test_health_ok(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_503_and_search_503_when_unloaded(app: FastAPI):
    app.dependency_overrides[get_optional_index] = lambda: None
    with TestClient(app) as c:
        assert c.get("/health").status_code == 503
        r = c.get("/search", params={"q": "x"})
        assert r.status_code == 503 and r.json() == {"detail": "index not loaded"}


def test_unexpected_error_is_clean_500(app: FastAPI):
    def boom() -> Index:
        raise RuntimeError("database on fire")

    app.dependency_overrides[get_optional_index] = boom
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/stats")
    assert r.status_code == 500
    assert r.json()["detail"] == "internal server error" and r.json()["request_id"]
    assert "fire" not in r.text  # internals stay in the log


def test_ui_pages(client: TestClient):
    home = client.get("/")
    assert home.status_code == 200 and "<form" in home.text
    results = client.get("/", params={"q": "whale"})
    assert "<mark>whale</mark>" in results.text.lower() and "/doc/0" in results.text
    partial = client.get("/", params={"q": "whale"}, headers={"HX-Request": "true"})
    assert "<form" not in partial.text and "/doc/0" in partial.text  # htmx fragment only
    doc = client.get("/doc/0", params={"q": "whale"})
    assert doc.status_code == 200 and "<mark>whale</mark>" in doc.text
    assert client.get("/doc/999").status_code == 404
    assert client.get("/static/style.css").status_code == 200


def test_openapi_lists_routes(client: TestClient):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/search", "/docs/{doc_id}", "/stats", "/health"} <= set(paths)


def test_async_mode_registers_same_api(fixture_index: Index, index_file: Path):
    app = create_app(Settings(index_path=index_file, search_mode="async"))
    app.dependency_overrides[get_optional_index] = lambda: fixture_index
    with TestClient(app) as c:
        assert c.get("/search", params={"q": "sea"}).json()["total"] == 3
