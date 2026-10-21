"""The findex web service: JSON API + server-rendered UI.

INDEX_PATH=build/index-pos.bin findex serve --port 8000
open http://127.0.0.1:8000/  (UI)   http://127.0.0.1:8000/docs  (OpenAPI)
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from findex import store
from findex.index import Index
from findex.query import QuerySyntaxError
from findex.scoring import SCORERS
from findex.search import search
from findex.web.models import DocumentOut, Health, SearchHit, SearchResponse, StatsOut
from findex.web.settings import Settings, get_settings

log = logging.getLogger("findex.web")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

ScorerName = Literal["bm25", "tfidf"]


class State:
    """What the lifespan loads once per process."""

    index: Index | None = None
    index_path: Path | None = None
    started: float = time.time()


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings: Settings = app.state.settings
    t0 = time.perf_counter()
    try:
        state.index = store.load(settings.index_path)
        state.index_path = settings.index_path
        log.info(
            "loaded %r from %s in %.2f s",
            state.index,
            settings.index_path,
            time.perf_counter() - t0,
        )
    except (OSError, ValueError, TypeError) as exc:
        # Start anyway: /health reports 503 so the platform keeps us out of rotation.
        log.error("could not load index %s: %s", settings.index_path, exc)
        state.index = None
    state.started = time.time()
    yield
    state.index = None


# --- dependencies ------------------------------------------------------------------


def get_optional_index() -> Index | None:
    return state.index


def get_index(index: Annotated[Index | None, Depends(get_optional_index)]) -> Index:
    if index is None:
        raise HTTPException(status_code=503, detail="index not loaded")
    return index


IndexDep = Annotated[Index, Depends(get_index)]
OptionalIndexDep = Annotated[Index | None, Depends(get_optional_index)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# --- the search core, shared by the JSON route and the HTML page --------------------


def run_search(index: Index, q: str, k: int, scorer: str, page: int) -> SearchResponse:
    t0 = time.perf_counter()
    try:
        total = len(index.matching(q))
        results = search(index, q, SCORERS[scorer], k, offset=(page - 1) * k)
    except QuerySyntaxError as exc:
        raise HTTPException(status_code=422, detail=f"bad query: {exc}") from exc
    hits = [
        SearchHit(
            doc_id=r.doc_id,
            title=r.title,
            score=round(r.score, 4),
            snippet=r.snippet.text if r.snippet else "",
            snippet_html=r.snippet.render_html() if r.snippet else "",
        )
        for r in results
    ]
    return SearchResponse(
        query=q,
        scorer=scorer,
        total=total,
        page=page,
        k=k,
        pages=max(1, math.ceil(total / k)),
        took_ms=round((time.perf_counter() - t0) * 1000, 2),
        results=hits,
    )


QParam = Annotated[str, Query(min_length=1, max_length=500, description="query, e.g. white whale")]
KParam = Annotated[int, Query(ge=1, le=100, description="results per page")]
PageParam = Annotated[int, Query(ge=1, le=1000)]
ScorerParam = Annotated[ScorerName, Query()]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.7.0",
        summary="Full-text search (BM25 / TF-IDF) over a corpus you choose.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Unexpected bug: full traceback in the log under the request id; the client
            # gets a clean JSON 500 it can quote when reporting the problem.
            log.exception("request %s %s %s failed", rid, request.method, request.url.path)
            response = JSONResponse(
                {"detail": "internal server error", "request_id": rid}, status_code=500
            )
        response.headers["x-request-id"] = rid
        log.info(
            "%s %s %s -> %d %.1fms",
            rid,
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - t0) * 1000,
        )
        return response

    # --- JSON API --------------------------------------------------------------------

    if settings.search_mode == "async":

        @app.get("/search", response_model=SearchResponse, tags=["api"])
        async def search_async(  # pyright: ignore[reportUnusedFunction]  # registered by decorator
            index: IndexDep,
            q: QParam,
            k: KParam = 10,
            scorer: ScorerParam = "bm25",
            page: PageParam = 1,
        ) -> SearchResponse:
            """Ranked search, **async def**: the CPU work runs on the event loop itself."""
            return run_search(index, q, k, scorer, page)

    else:

        @app.get("/search", response_model=SearchResponse, tags=["api"])
        def search_sync(  # pyright: ignore[reportUnusedFunction]  # registered by decorator
            index: IndexDep,
            q: QParam,
            k: KParam = 10,
            scorer: ScorerParam = "bm25",
            page: PageParam = 1,
        ) -> SearchResponse:
            """Ranked search, **def**: FastAPI runs it in the threadpool, the loop stays free."""
            return run_search(index, q, k, scorer, page)

    @app.get("/docs/{doc_id}", response_model=DocumentOut, tags=["api"])
    def get_document(index: IndexDep, doc_id: int) -> DocumentOut:
        """One document's full text."""
        if doc_id not in index.doc_meta:
            raise HTTPException(status_code=404, detail=f"no document {doc_id}")
        meta = index.doc_meta[doc_id]
        return DocumentOut(
            doc_id=doc_id,
            title=meta.title,
            path=meta.path,
            length=index.doc_length(doc_id),
            text=index.text(doc_id),
        )

    @app.get("/stats", response_model=StatsOut, tags=["api"])
    def stats(index: IndexDep) -> StatsOut:
        path = state.index_path or settings.index_path
        return StatsOut(
            documents=index.num_docs,
            terms=len(index),
            tokens=sum(index.doc_lengths.values()),
            avg_doc_length=round(index.avg_doc_length, 1),
            positions=index.has_positions,
            index_file=path.name,
            index_bytes=path.stat().st_size if path.exists() else 0,
            uptime_s=round(time.time() - state.started, 1),
        )

    @app.get("/health", response_model=Health, tags=["ops"])
    def health(index: OptionalIndexDep) -> JSONResponse:
        """200 when the index is loaded and answers a lookup; 503 otherwise."""
        if index is None or index.num_docs == 0:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        _ = "the" in index  # touch the structure: a real lookup, not just "process alive"
        return JSONResponse({"status": "ok"})

    # --- HTML UI ---------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(
        request: Request,
        index: OptionalIndexDep,
        q: Annotated[str, Query(max_length=500)] = "",
        k: KParam = 10,
        scorer: ScorerParam = "bm25",
        page: PageParam = 1,
    ) -> HTMLResponse:
        result: SearchResponse | None = None
        error: str | None = None
        if index is None:
            error = "The index is not loaded yet."
        elif q.strip():
            try:
                result = run_search(index, q, k, scorer, page)
            except HTTPException as exc:
                error = str(exc.detail)
        context = {
            "q": q,
            "k": k,
            "scorer": scorer,
            "result": result,
            "error": error,
            "docs": index.num_docs if index else 0,
            "app_name": settings.app_name,
        }
        partial = request.headers.get("hx-request") == "true"  # htmx search-as-you-type
        name = "_results.html" if partial else "index.html"
        return templates.TemplateResponse(request, name, context)

    @app.get("/doc/{doc_id}", response_class=HTMLResponse, include_in_schema=False)
    def doc_page(request: Request, index: IndexDep, doc_id: int, q: str = "") -> HTMLResponse:
        if doc_id not in index.doc_meta:
            raise HTTPException(status_code=404, detail=f"no document {doc_id}")
        from findex.snippets import Snippet
        from findex.tokenize import WORD_RE, tokenize

        text = index.text(doc_id)
        wanted = set(tokenize(q))
        spans = tuple(
            (m.start(), m.end())
            for m in WORD_RE.finditer(text)
            if wanted and next(tokenize(m.group()), "") in wanted
        )
        body = Snippet(text, spans).render_html()
        context = {
            "title": index.title(doc_id),
            "body": body,
            "q": q,
            "doc_id": doc_id,
            "length": index.doc_length(doc_id),
            "app_name": settings.app_name,
        }
        return templates.TemplateResponse(request, "doc.html", context)

    return app
