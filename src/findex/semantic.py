"""Semantic and hybrid search (Lab 8, M3).

    findex embed build/index.bin --model all-MiniLM-L6-v2      # -> build/index.bin.emb/
    findex search build/index.bin "giant sea creature hunted by a captain" --mode semantic
    findex search build/index.bin "white whale" --mode hybrid

Every document is cut into ~200-word chunks; each chunk is embedded, L2-normalized
and stored in ``E: float32[n_chunks, d]`` with ``chunk_doc: int32[n_chunks]``. A query
is embedded the same way; ``E @ q`` is then the cosine similarity of the query with
every chunk at once, and a document scores as its *best* chunk.

Needs the optional extra: ``uv sync --extra semantic`` (fastembed = ONNX MiniLM, no torch).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from findex.index import Index

log = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CHUNK_WORDS = 200  # MiniLM truncates at 256 word pieces ≈ 200 English words
RRF_K = 60
F32 = npt.NDArray[np.float32]


class SemanticUnavailable(RuntimeError):
    """No embeddings for this index, or the optional ``semantic`` extra is missing."""


def model_id(name: str) -> str:
    return name if "/" in name else f"sentence-transformers/{name}"


def embeddings_dir(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".emb")


_WS = re.compile(r"\s+")


def chunk_text(text: str, words: int = CHUNK_WORDS) -> list[str]:
    """Non-overlapping chunks of ``words`` words (the first line — the title — is
    prepended to every chunk so each chunk knows which book it belongs to)."""
    title, _, body = text.partition("\n")
    tokens = _WS.split(body.strip())
    if not tokens or tokens == [""]:
        return [title.strip()] if title.strip() else []
    return [
        f"{title.strip()}. {' '.join(tokens[i : i + words])}" for i in range(0, len(tokens), words)
    ]


def _load_model(name: str) -> Any:
    try:
        from fastembed import TextEmbedding  # pyright: ignore[reportMissingTypeStubs]
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise SemanticUnavailable("install the extra: uv sync --extra semantic") from exc
    return TextEmbedding(model_id(name))


def _normalize(m: F32) -> F32:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return (m / np.maximum(norms, 1e-12)).astype(np.float32)


def embed_index(
    index: Index,
    out_dir: Path,
    *,
    model: str = DEFAULT_MODEL,
    max_chunks: int | None = None,
    batch_size: int = 64,
    parallel: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Embed every document of ``index``; returns the number of chunks written."""
    embedder = _load_model(model)
    chunk_doc: list[int] = []
    chunk_pos: list[int] = []

    def chunks() -> Iterator[str]:
        for doc_id in sorted(index.doc_ids):
            pieces = chunk_text(index.text(doc_id))
            positions = range(len(pieces))
            if max_chunks is not None and len(pieces) > max_chunks:
                # evenly spaced across the whole document, not just its beginning
                positions = sorted({round(x) for x in np.linspace(0, len(pieces) - 1, max_chunks)})
            for pos in positions:
                chunk_doc.append(doc_id)
                chunk_pos.append(pos)
                yield pieces[pos]

    t0 = time.perf_counter()
    rows: list[F32] = []
    for vec in embedder.embed(chunks(), batch_size=batch_size, parallel=parallel):
        rows.append(np.asarray(vec, dtype=np.float32))
        if on_progress and len(rows) % batch_size == 0:
            on_progress(batch_size)
    matrix = _normalize(np.vstack(rows))
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "E.npy", matrix)
    np.save(out_dir / "chunk_doc.npy", np.asarray(chunk_doc, dtype=np.int32))
    np.save(out_dir / "chunk_pos.npy", np.asarray(chunk_pos, dtype=np.int32))
    meta = {
        "model": model_id(model),
        "chunks": len(rows),
        "dim": int(matrix.shape[1]),
        "chunk_words": CHUNK_WORDS,
        "max_chunks": max_chunks,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    log.info("embedded %d chunks in %.1f s -> %s", len(rows), meta["seconds"], out_dir)
    return len(rows)


@dataclass
class SemanticIndex:
    E: F32  # float32[n_chunks, d], rows L2-normalized
    chunk_doc: npt.NDArray[np.int32]
    chunk_pos: npt.NDArray[np.int32]
    model_name: str
    _embedder: Any = None

    @classmethod
    def load(cls, emb_dir: Path) -> SemanticIndex:
        if not (emb_dir / "E.npy").exists():
            raise SemanticUnavailable(f"no embeddings at {emb_dir} (run `findex embed`)")
        meta = json.loads((emb_dir / "meta.json").read_text(encoding="utf-8"))
        return cls(
            np.load(emb_dir / "E.npy", mmap_mode="r"),  # paged in on demand, shared by workers
            np.load(emb_dir / "chunk_doc.npy"),
            np.load(emb_dir / "chunk_pos.npy"),
            meta["model"],
        )

    def embed_query(self, query: str) -> F32:
        if self._embedder is None:
            self._embedder = _load_model(self.model_name)
        vec = np.asarray(next(iter(self._embedder.query_embed(query))), dtype=np.float32)
        return vec / max(float(np.linalg.norm(vec)), 1e-12)

    def search(self, query: str, k: int) -> list[tuple[int, float, int]]:
        """Top ``k`` documents as ``(doc_id, cosine, best_chunk_position)``."""
        sims = self.E @ self.embed_query(query)  # cosine == dot product after normalization
        n_docs = int(self.chunk_doc.max()) + 1
        best = np.full(n_docs, -np.inf, dtype=np.float32)
        np.maximum.at(best, self.chunk_doc, sims)  # best chunk per document
        k = min(k, int(np.isfinite(best).sum()))
        if k <= 0:
            return []
        top = np.argpartition(-best, k - 1)[:k]
        top = top[np.lexsort((top, -best[top]))]
        # which chunk won, for the snippet: among this doc's chunks, the max similarity
        out: list[tuple[int, float, int]] = []
        for doc_id in top.tolist():
            rows = np.flatnonzero(self.chunk_doc == doc_id)
            win = rows[int(np.argmax(sims[rows]))]
            out.append((doc_id, float(best[doc_id]), int(self.chunk_pos[win])))
        return out


def rrf(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: ``score(d) = Σ 1 / (k + rank_r(d))`` over the rankings.

    Uses only ranks, so BM25 scores (unbounded) and cosines (in [-1, 1]) never need
    to be put on the same scale.
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, 1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
