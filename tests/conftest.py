from pathlib import Path

import pytest

from findex import store
from findex.corpus import iter_documents
from findex.index import Index, build_index

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def corpus_dir() -> Path:
    """Eight tiny hand-written documents (English, Ukrainian, a long one, a repetitive one)."""
    return FIXTURES / "corpus"


@pytest.fixture(scope="session")
def fixture_index(corpus_dir: Path) -> Index:
    """Built once per session — tests must not mutate it (copy it if you need to)."""
    return build_index(iter_documents(corpus_dir), positions=True)


@pytest.fixture(scope="session")
def index_file(fixture_index: Index, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ix") / "fixture.bin"
    store.save(fixture_index, path)
    return path
