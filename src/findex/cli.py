"""The ``findex`` command line: ``findex index | search | stats``.

Results go to stdout; every diagnostic (logs, progress, errors) goes to stderr, so
``findex search ix.bin "q" --json | jq .`` pipes cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Iterator
from itertools import islice
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from findex import store
from findex.corpus import Document, iter_documents
from findex.crawler import CrawlStats
from findex.index import Index, build_index
from findex.memory import utf8_stdout
from findex.parallel import ExecutorName, build_parallel, corpus_paths
from findex.query import QuerySyntaxError
from findex.scoring import SCORERS

log = logging.getLogger("findex")
app = typer.Typer(help="findex - full-text search over your own corpus.", no_args_is_help=True)
out = Console()
err = Console(stderr=True)

ScorerName = Literal["bm25", "tfidf"]
SearchMode = Literal["keyword", "semantic", "hybrid"]
DEFAULT_WORKERS = min(8, os.cpu_count() or 1)


def _fail(message: str, code: int = 1) -> typer.Exit:
    err.print(f"[bold red]error:[/] {escape(message)}")
    return typer.Exit(code)


@app.callback()
def main(
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="-v info, -vv debug")
    ] = 0,
) -> None:
    """Configure logging once, for every subcommand."""
    utf8_stdout()
    level = logging.WARNING - 10 * min(verbose, 2)
    logging.basicConfig(
        level=level,
        format="%(name)s: %(message)s",
        handlers=[RichHandler(console=err, show_path=False, show_time=False)],
        force=True,
    )


def _progress() -> Progress:
    return Progress(
        TextColumn("[bold]indexing"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err,
        transient=True,
    )


def _with_progress(docs: Iterator[Document], total: int | None) -> Iterator[Document]:
    with _progress() as progress:
        task = progress.add_task("docs", total=total)
        pending = 0
        for doc in docs:
            yield doc
            pending += 1
            if pending == 64:  # advance() takes a lock and re-renders: batch it (Lab 8, 15 %)
                progress.advance(task, pending)
                pending = 0
        progress.advance(task, pending)


def _load(path: Path) -> Index:
    if not path.is_file():
        raise _fail(f"index file not found: {path}")
    try:
        return store.load(path)
    except (ValueError, TypeError, OSError) as exc:
        raise _fail(f"cannot read index {path}: {exc}") from exc


@app.command()
def index(
    corpus: Annotated[Path, typer.Argument(help="directory of .txt / .jsonl files, or one file")],
    out_path: Annotated[Path, typer.Option("--out", "-o", help="index file to write")],
    positions: Annotated[bool, typer.Option(help="record positions (phrase queries)")] = False,
    limit: Annotated[int | None, typer.Option(help="index only the first N documents")] = None,
    fmt: Annotated[store.Format, typer.Option("--format")] = "binary",
    executor: Annotated[ExecutorName, typer.Option(help="how to parallelize")] = "processes",
    workers: Annotated[int, typer.Option(min=1, help="worker threads/processes")] = DEFAULT_WORKERS,
) -> None:
    """Build an index from a corpus and save it."""
    if not corpus.exists():
        raise _fail(f"corpus not found: {corpus}")
    t0 = time.perf_counter()
    if executor == "serial" and limit is not None:
        # --limit counts documents (a .jsonl file holds many), so stream them lazily
        docs = islice(iter_documents(corpus), limit)
        ix = build_index(_with_progress(docs, limit), positions=positions)
    else:
        paths = corpus_paths(corpus, limit)
        with _progress() as progress:
            task = progress.add_task("docs", total=len(paths))
            ix = build_parallel(
                paths,
                executor=executor,
                workers=workers,
                positions=positions,
                on_chunk=lambda n: progress.advance(task, n),
            ).index
    if ix.num_docs == 0:
        raise _fail(f"no documents found under {corpus}")
    store.save(ix, out_path, fmt=fmt)
    err.print(
        f"indexed [bold]{ix.num_docs:,}[/] docs, {len(ix):,} terms in "
        f"{time.perf_counter() - t0:.1f} s → {out_path} ({out_path.stat().st_size / 2**20:.1f} MiB)"
    )


@app.command()
def search(
    index_path: Annotated[Path, typer.Argument(metavar="INDEX")],
    query: Annotated[str, typer.Argument(help="e.g. 'white whale NOT ship' or '\"call me\"'")],
    k: Annotated[int, typer.Option("--k", "-k", min=1, max=1000)] = 10,
    scorer: Annotated[ScorerName, typer.Option()] = "bm25",
    as_json: Annotated[bool, typer.Option("--json", help="JSON lines on stdout")] = False,
    mode: Annotated[
        SearchMode, typer.Option(help="keyword (BM25/TF-IDF), semantic, or hybrid (RRF)")
    ] = "keyword",
) -> None:
    """Ranked search with highlighted snippets."""
    from findex.search import search_mode
    from findex.semantic import SemanticIndex, SemanticUnavailable, embeddings_dir

    ix = _load(index_path)
    semantic = None
    t0 = time.perf_counter()
    try:
        if mode != "keyword":
            semantic = SemanticIndex.load(embeddings_dir(index_path))
        _, results = search_mode(ix, query, mode, SCORERS[scorer], k, semantic=semantic)
    except QuerySyntaxError as exc:
        raise _fail(f"bad query: {exc}", code=2) from exc
    except SemanticUnavailable as exc:
        raise _fail(str(exc)) from exc
    took = (time.perf_counter() - t0) * 1000
    label = scorer if mode == "keyword" else mode

    if as_json:
        for r in results:
            record = {
                "doc_id": r.doc_id,
                "score": round(r.score, 4),
                "title": r.title,
                "snippet": r.snippet.text if r.snippet else "",
            }
            sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
        return

    table = Table(title=f"{query!r} — {len(results)} results, {took:.1f} ms ({label})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("score", justify="right")
    table.add_column("document", overflow="fold")
    for rank, r in enumerate(results, 1):
        snippet = ""
        if r.snippet:
            # escape everything first, then mark matches: user text must never be rich markup
            snippet = r.snippet.render(chr(1), chr(2))
            snippet = escape(snippet).replace(chr(1), "[bold yellow]").replace(chr(2), "[/]")
        table.add_row(str(rank), f"{r.score:.3f}", f"[bold]{escape(r.title)}[/]\n{snippet}")
    out.print(table)


@app.command()
def stats(
    index_path: Annotated[Path, typer.Argument(metavar="INDEX")],
    top: Annotated[int, typer.Option(min=0)] = 20,
) -> None:
    """Corpus statistics from a saved index (the Lab 1 numbers)."""
    ix = _load(index_path)
    cf: Counter[str] = Counter({term: sum(p.tf for p in plist) for term, plist in ix.items()})
    summary = Table.grid(padding=(0, 2))
    summary.add_row("documents", f"{ix.num_docs:,}")
    summary.add_row("tokens", f"{sum(ix.doc_lengths.values()):,}")
    summary.add_row("vocabulary", f"{len(ix):,}")
    summary.add_row("avg doc length", f"{ix.avg_doc_length:,.1f}")
    summary.add_row("positions", "yes" if ix.has_positions else "no")
    out.print(summary)
    if top:
        terms = Table(title=f"top {top} terms")
        terms.add_column("#", justify="right", style="dim")
        terms.add_column("term")
        terms.add_column("count", justify="right")
        terms.add_column("df", justify="right")
        for rank, (term, count) in enumerate(cf.most_common(top), 1):
            terms.add_row(str(rank), escape(term), f"{count:,}", f"{ix.df(term):,}")
        out.print(terms)


def _crawl_panel(stats: CrawlStats, max_pages: int) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_row("pages", f"[bold]{stats.pages}[/] / {max_pages}")
    grid.add_row("pages/s", f"{stats.pages_per_s:.1f}")
    grid.add_row("fetched", str(stats.fetched))
    grid.add_row("in flight", str(stats.in_flight))
    grid.add_row("queue", str(stats.queued))
    grid.add_row("errors", f"[red]{stats.errors}[/]" if stats.errors else "0")
    grid.add_row("robots-skipped", str(stats.skipped_robots))
    return grid


@app.command()
def crawl(
    seeds: Annotated[list[str], typer.Argument(help="start URL(s)")],
    max_pages: Annotated[int, typer.Option(min=1)] = 500,
    concurrency: Annotated[int, typer.Option(min=1, max=100)] = 10,
    per_host: Annotated[int, typer.Option(min=1, help="max parallel requests per host")] = 2,
    delay: Annotated[
        float, typer.Option(min=0.0, help="min seconds between requests per host")
    ] = 0.25,
    domain: Annotated[
        list[str] | None, typer.Option(help="allowed domain (repeatable); default: seed hosts")
    ] = None,
    scope: Annotated[
        list[str] | None, typer.Option(help="only follow URLs with this prefix (repeatable)")
    ] = None,
    out_path: Annotated[Path, typer.Option("--out", "-o")] = Path("data/crawl.jsonl"),
    log_path: Annotated[Path, typer.Option("--log")] = Path("crawl.log"),
    timeout: Annotated[float, typer.Option(min=0.01, help="per-request timeout, seconds")] = 10.0,
    debug: Annotated[bool, typer.Option(help="asyncio debug mode: report blocking calls")] = False,
) -> None:
    """Crawl politely (robots.txt, per-host limits) and stream pages to JSON lines."""
    import asyncio

    from rich.live import Live

    from findex.crawler import CrawlStats
    from findex.crawler import crawl as run_crawl

    out_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    crawl_log = logging.getLogger("findex.crawl")
    crawl_log.addHandler(handler)
    crawl_log.setLevel(logging.INFO)
    crawl_log.propagate = log.getEffectiveLevel() <= logging.INFO  # -v also shows it on stderr
    stats = CrawlStats()

    async def go() -> None:
        with (
            out_path.open("w", encoding="utf-8") as fh,
            Live(
                _crawl_panel(stats, max_pages), console=err, refresh_per_second=4, transient=False
            ) as live,
        ):
            async for page in run_crawl(
                seeds,
                max_pages=max_pages,
                concurrency=concurrency,
                per_host=per_host,
                delay=delay,
                allowed_domains=domain or None,
                scopes=scope or (),
                timeout=timeout,
                stats=stats,
            ):
                # one line per page, written as it arrives; indexing happens later,
                # outside the event loop (`findex index data/crawl.jsonl`)
                fh.write(json.dumps(page.to_json(), ensure_ascii=False) + "\n")
                live.update(_crawl_panel(stats, max_pages))
            live.update(_crawl_panel(stats, max_pages))

    t0 = time.perf_counter()
    try:
        asyncio.run(go(), debug=debug)
    except KeyboardInterrupt:
        err.print("[yellow]interrupted[/] — pages so far are saved")
    finally:
        crawl_log.removeHandler(handler)
        handler.close()
    err.print()
    err.print(
        f"crawled {stats.pages} pages ({stats.errors} errors) in {time.perf_counter() - t0:.1f} s"
        f" → {out_path}; per-URL log in {log_path}"
    )


@app.command()
def serve(
    index_path: Annotated[
        Path | None, typer.Option("--index", help="index file (default: $INDEX_PATH)")
    ] = None,
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    workers: Annotated[int, typer.Option(min=1, help="uvicorn worker processes")] = 1,
    reload: Annotated[bool, typer.Option(help="auto-reload on code changes (dev)")] = False,
) -> None:
    """Run the web UI + JSON API (uvicorn)."""
    import uvicorn

    if index_path is not None:
        os.environ["INDEX_PATH"] = str(index_path)  # read by pydantic-settings in each worker
    uvicorn.run(
        "findex.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        reload=reload,
        log_level="info",
    )


@app.command()
def embed(
    index_path: Annotated[Path, typer.Argument(metavar="INDEX")],
    model: Annotated[str, typer.Option(help="sentence-transformers model")] = "all-MiniLM-L6-v2",
    max_chunks: Annotated[
        int | None, typer.Option(min=1, help="chunks per document, evenly spaced (default: all)")
    ] = None,
    out_dir: Annotated[Path | None, typer.Option("--out", help="default: INDEX.emb/")] = None,
) -> None:
    """Embed every document for semantic / hybrid search (needs the `semantic` extra)."""
    from findex.semantic import SemanticUnavailable, embed_index, embeddings_dir

    ix = _load(index_path)
    target = out_dir or embeddings_dir(index_path)
    estimate = ix.num_docs * (max_chunks or 18)
    with Progress(
        TextColumn("[bold]embedding"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err,
        transient=True,
    ) as progress:
        task = progress.add_task("chunks", total=estimate)
        try:
            n = embed_index(
                ix,
                target,
                model=model,
                max_chunks=max_chunks,
                on_progress=lambda k: progress.advance(task, k),
            )
        except SemanticUnavailable as exc:
            raise _fail(str(exc)) from exc
    err.print(f"embedded [bold]{n:,}[/] chunks of {ix.num_docs:,} docs → {target}")
