from pathlib import Path

import pytest

from findex import store
from findex.corpus import iter_documents
from findex.index import build_index
from findex.parallel import EXECUTORS, ExecutorName, build_parallel, chunked, corpus_paths


def test_chunked_preserves_order_and_items():
    items = list(range(10))
    chunks = chunked(items, 3)
    assert [list(c) for c in chunks] == [[0, 1, 2, 3], [4, 5, 6], [7, 8, 9]]
    assert chunked(items, 50) == [[i] for i in items]
    assert chunked([], 4) == [[]]


@pytest.mark.slow
@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("positions", [False, True])
def test_parallel_build_is_byte_identical(
    corpus_dir: Path, tmp_path: Path, executor: ExecutorName, positions: bool
):
    serial = build_index(iter_documents(corpus_dir), positions=positions)
    report = build_parallel(
        corpus_paths(corpus_dir), executor=executor, workers=3, positions=positions
    )
    assert report.index == serial
    assert report.index.doc_meta == serial.doc_meta
    assert report.index.doc_lengths == serial.doc_lengths
    a, b = tmp_path / "serial.bin", tmp_path / "parallel.bin"
    store.save(serial, a)
    store.save(report.index, b)
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.slow
def test_worker_exception_surfaces():
    # 42 is not a Path: the worker crashes with AttributeError inside the child
    # process, and pool.map must re-raise it in the parent instead of hanging.
    bad: list[Path] = [42]  # type: ignore[list-item]  # deliberately wrong input
    with pytest.raises(AttributeError, match="name"):
        build_parallel(bad, executor="processes", workers=2)
