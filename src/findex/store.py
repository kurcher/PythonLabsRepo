"""Saving and loading an ``Index`` in three formats.

* ``pickle`` — one call each way, fast, and **never safe on an untrusted file**:
  unpickling can call any importable callable (``__reduce__`` → ``os.system``),
  so ``pickle.load`` on a downloaded index is remote code execution.
* ``json`` — safe and human-readable, but every number becomes text.
* ``binary`` — a JSON header (documents + term -> offset/count) followed by one
  contiguous ``array('I')`` holding every term's doc_ids, tfs and positions.
  Loading it only parses data; nothing in the file is ever executed.
"""

from __future__ import annotations

import json
import pickle
import sys
from array import array
from pathlib import Path
from typing import Literal

from findex.decorators import timed
from findex.index import DocMeta, Index, Posting
from findex.packed import PackedPostings, pack

Format = Literal["binary", "pickle", "json"]
FORMATS: tuple[Format, ...] = ("binary", "pickle", "json")
MAGIC = b"FIDX1"


def detect_format(path: Path) -> Format:
    with path.open("rb") as fh:
        head = fh.read(len(MAGIC))
    if head == MAGIC:
        return "binary"
    if head[:1] == b"{":
        return "json"
    return "pickle"


def save(index: Index, path: Path, fmt: Format = "binary") -> None:
    if fmt == "pickle":
        with path.open("wb") as fh:
            pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)
    elif fmt == "json":
        _save_json(index, path)
    else:
        _save_binary(index, path)


@timed
def load(path: Path, fmt: Format | None = None) -> Index:
    fmt = fmt or detect_format(path)
    if fmt == "pickle":
        # Only ever load index files you built yourself — see the module docstring.
        with path.open("rb") as fh:
            index = pickle.load(fh)
        if not isinstance(index, Index):
            raise TypeError(f"{path} does not contain an Index")
        return index
    if fmt == "json":
        return _load_json(path)
    return _load_binary(path)


def _docs_header(index: Index) -> list[list[object]]:
    return [
        [m.doc_id, m.path, m.title, index.doc_lengths[m.doc_id]] for m in index.doc_meta.values()
    ]


def _apply_docs_header(index: Index, docs: list[list[object]]) -> None:
    for doc_id, doc_path, title, length in docs:
        assert isinstance(doc_id, int) and isinstance(length, int)
        index.doc_meta[doc_id] = DocMeta(doc_id, str(doc_path), str(title))
        index.doc_lengths[doc_id] = length


def _save_json(index: Index, path: Path) -> None:
    data = {
        "positions": index.has_positions,
        "docs": _docs_header(index),
        "postings": {
            term: [
                [p.doc_id, p.tf, list(p.positions)] if index.has_positions else [p.doc_id, p.tf]
                for p in plist
            ]
            for term, plist in index.postings.items()
        },
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))


def _load_json(path: Path) -> Index:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    index = Index(has_positions=data["positions"])
    _apply_docs_header(index, data["docs"])
    if index.has_positions:
        index.postings = {
            t: [Posting(d, tf, tuple(pos)) for d, tf, pos in plist]
            for t, plist in data["postings"].items()
        }
    else:
        index.postings = {
            t: [Posting(d, tf) for d, tf in plist] for t, plist in data["postings"].items()
        }
    return index


def _save_binary(index: Index, path: Path) -> None:
    packed = pack(index.postings, index.has_positions)
    header = json.dumps(
        {
            "byteorder": sys.byteorder,
            "positions": index.has_positions,
            "docs": _docs_header(index),
            "terms": packed.terms,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    with path.open("wb") as fh:
        fh.write(MAGIC)
        fh.write(len(header).to_bytes(8, "little"))
        fh.write(header)
        fh.write(packed.raw_bytes())


def _load_binary(path: Path) -> Index:
    """Read the header and one buffer; ``Posting`` objects are built lazily per term
    (Lab 8: this used to build all ~5 M of them here and took 12 s)."""
    with path.open("rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path} is not a findex binary index")
        header = json.loads(fh.read(int.from_bytes(fh.read(8), "little")))
        blob = array("I")
        blob.frombytes(fh.read())
    if header["byteorder"] != sys.byteorder:
        blob.byteswap()

    index = Index(has_positions=header["positions"])
    _apply_docs_header(index, header["docs"])
    terms = {t: (int(o), int(c), int(n)) for t, (o, c, n) in header["terms"].items()}
    index.postings = PackedPostings(blob, terms)
    return index
