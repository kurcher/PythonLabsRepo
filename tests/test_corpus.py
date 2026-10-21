import gzip
import json
from pathlib import Path

import pytest

from findex.corpus import iter_documents, read_document
from findex.stats import eager_stats, lazy_stats
from findex.stats import main as stats_main


def test_first_document_is_lazy(corpus_dir: Path):
    docs = iter_documents(corpus_dir)
    first = next(docs)
    assert first.doc_id == 0 and first.path.name == "01-whale.txt"
    assert first.text.startswith("The Whale")


def test_jsonl_gzip_and_bad_files(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    (tmp_path / "a.txt").write_text("plain text", encoding="utf-8")
    (tmp_path / "b.txt").write_bytes(b"\xff\xfe broken \x81")  # not UTF-8
    records = [{"title": "T1", "text": "first"}, {"text": "second"}]
    (tmp_path / "c.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n\nnot json\n", encoding="utf-8"
    )
    with gzip.open(tmp_path / "d.txt.gz", "wt", encoding="utf-8") as fh:
        fh.write("compressed")

    docs = list(iter_documents(tmp_path))
    assert [d.text for d in docs] == ["plain text", "T1\n\nfirst", "second", "compressed"]
    assert [d.doc_id for d in docs] == [0, 1, 2, 3]
    assert "skipping" in caplog.text and "bad JSON" in caplog.text
    assert read_document(docs[2].locator) == "second"
    assert read_document(docs[0].locator) == "plain text"


def test_lazy_and_eager_stats_agree(corpus_dir: Path):
    lazy = lazy_stats(iter_documents(corpus_dir))
    eager = eager_stats(iter_documents(corpus_dir))
    assert (lazy.documents, lazy.tokens, lazy.counts) == (
        eager.documents,
        eager.tokens,
        eager.counts,
    )
    assert lazy.documents == 8 and lazy.vocabulary == len(lazy.counts)


def test_stats_cli(corpus_dir: Path, capsys: pytest.CaptureFixture[str]):
    stats_main([str(corpus_dir), "--limit", "2", "--top", "3"])
    out = capsys.readouterr().out
    assert "documents:   2" in out and "peak memory" in out
