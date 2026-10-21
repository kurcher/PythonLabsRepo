import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from findex.cli import app

runner = CliRunner()


def test_help_lists_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("index", "search", "stats"):
        assert cmd in result.output


def test_index_then_search_json(corpus_dir: Path, tmp_path: Path):
    out = tmp_path / "ix.bin"
    result = runner.invoke(app, ["index", str(corpus_dir), "--out", str(out), "--positions"])
    assert result.exit_code == 0, result.output
    assert out.is_file()

    result = runner.invoke(app, ["search", str(out), '"event loop"', "--json"])
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["doc_id"] for r in lines] == [2]
    assert set(lines[0]) == {"doc_id", "score", "title", "snippet"}


def test_search_table_output(index_file: Path):
    result = runner.invoke(app, ["search", str(index_file), "sea", "-k", "2"])
    assert result.exit_code == 0
    assert "Sea Stories" in result.stdout


def test_stats(index_file: Path):
    result = runner.invoke(app, ["stats", str(index_file), "--top", "3"])
    assert result.exit_code == 0
    assert "documents" in result.stdout and "8" in result.stdout
    assert "filler" in result.stdout  # the most frequent fixture term


def test_missing_index_is_a_clean_error(tmp_path: Path):
    result = runner.invoke(app, ["search", str(tmp_path / "nope.bin"), "x"])
    assert result.exit_code == 1
    assert "index file not found" in result.output
    assert "Traceback" not in result.output


def test_bad_query_exits_2(index_file: Path):
    result = runner.invoke(app, ["search", str(index_file), "a OR"])
    assert result.exit_code == 2
    assert "bad query" in result.output


@pytest.mark.parametrize(
    "args", [["search"], ["index"], ["stats"], ["search", "x", "q", "-k", "0"]]
)
def test_usage_errors(args: list[str]):
    assert runner.invoke(app, args).exit_code == 2


def test_verbose_logs_go_to_stderr(index_file: Path):
    result = runner.invoke(app, ["-vv", "search", str(index_file), "whale", "--json"])
    assert result.exit_code == 0
    assert "took" in result.stderr  # @timed logs
    for line in result.stdout.splitlines():
        json.loads(line)  # stdout stays pure JSON lines
