"""Pydantic models: the API's contract at the edge. Internals stay dataclasses."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    doc_id: int
    title: str
    score: float
    snippet: str = Field(description="plain-text snippet around the best match")
    snippet_html: str = Field(description="the same snippet, HTML-escaped, matches in <mark>")


class SearchResponse(BaseModel):
    query: str
    scorer: str
    total: int = Field(description="number of matching documents")
    page: int
    k: int
    pages: int
    took_ms: float
    results: list[SearchHit]


class DocumentOut(BaseModel):
    doc_id: int
    title: str
    path: str
    length: int = Field(description="tokens")
    text: str


class StatsOut(BaseModel):
    documents: int
    terms: int
    tokens: int
    avg_doc_length: float
    positions: bool
    index_file: str
    index_bytes: int
    uptime_s: float
    semantic_chunks: int = 0


class Health(BaseModel):
    status: str
