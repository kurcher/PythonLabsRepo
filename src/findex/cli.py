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
from findex.index import Index, build_index
from findex.memory import utf8_stdout
from findex.parallel import ExecutorName, build_parallel, corpus_paths
from findex.query import QuerySyntaxError
from findex.scoring import SCORERS
from findex.search import search as run_search

log = logging.getLogger("findex")
app = typer.Typer(help="findex - full-text search over your own corpus.", no_args_is_help=True)
out = Console()
err = Console(stderr=True)

ScorerName = Literal["bm25", "tfidf"]
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
        for doc in docs:
            yield doc
            progress.advance(task)


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
) -> None:
    """Ranked search with highlighted snippets."""
    ix = _load(index_path)
    t0 = time.perf_counter()
    try:
        results = run_search(ix, query, SCORERS[scorer], k)
    except QuerySyntaxError as exc:
        raise _fail(f"bad query: {exc}", code=2) from exc
    took = (time.perf_counter() - t0) * 1000

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

    table = Table(title=f"{query!r} — {len(results)} results, {took:.1f} ms ({scorer})")
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

