"""Saving and loading an ``InvertedIndex`` in three formats.

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

from findex.index import DocMeta, InvertedIndex, Posting

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


def save(index: InvertedIndex, path: Path, fmt: Format = "binary") -> None:
    if fmt == "pickle":
        with path.open("wb") as fh:
            pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)
    elif fmt == "json":
        _save_json(index, path)
    else:
        _save_binary(index, path)


def load(path: Path, fmt: Format | None = None) -> InvertedIndex:
    fmt = fmt or detect_format(path)
    if fmt == "pickle":
        # Only ever load index files you built yourself — see the module docstring.
        with path.open("rb") as fh:
            index = pickle.load(fh)
        if not isinstance(index, InvertedIndex):
            raise TypeError(f"{path} does not contain an InvertedIndex")
        return index
    if fmt == "json":
        return _load_json(path)
    return _load_binary(path)


def _docs_header(index: InvertedIndex) -> list[list[object]]:
    return [
        [m.doc_id, m.path, m.title, index.doc_lengths[m.doc_id]] for m in index.doc_meta.values()
    ]


def _apply_docs_header(index: InvertedIndex, docs: list[list[object]]) -> None:
    for doc_id, doc_path, title, length in docs:
        assert isinstance(doc_id, int) and isinstance(length, int)
        index.doc_meta[doc_id] = DocMeta(doc_id, str(doc_path), str(title))
        index.doc_lengths[doc_id] = length


def _save_json(index: InvertedIndex, path: Path) -> None:
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


def _load_json(path: Path) -> InvertedIndex:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    index = InvertedIndex(has_positions=data["positions"])
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


def _save_binary(index: InvertedIndex, path: Path) -> None:
    blob = array("I")
    terms: dict[str, list[int]] = {}
    for term, plist in index.postings.items():
        offset = len(blob)
        blob.extend(p.doc_id for p in plist)
        blob.extend(p.tf for p in plist)
        npos = 0
        if index.has_positions:
            for p in plist:
                blob.extend(p.positions)
                npos += len(p.positions)
        terms[term] = [offset, len(plist), npos]
    header = json.dumps(
        {
            "byteorder": sys.byteorder,
            "positions": index.has_positions,
            "docs": _docs_header(index),
            "terms": terms,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    with path.open("wb") as fh:
        fh.write(MAGIC)
        fh.write(len(header).to_bytes(8, "little"))
        fh.write(header)
        blob.tofile(fh)


def _load_binary(path: Path) -> InvertedIndex:
    with path.open("rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path} is not a findex binary index")
        header = json.loads(fh.read(int.from_bytes(fh.read(8), "little")))
        blob = array("I")
        blob.frombytes(fh.read())
    if header["byteorder"] != sys.byteorder:
        blob.byteswap()

    index = InvertedIndex(has_positions=header["positions"])
    _apply_docs_header(index, header["docs"])
    postings: dict[str, list[Posting]] = {}
    for term, (offset, count, npos) in header["terms"].items():
        ids = blob[offset : offset + count]
        tfs = blob[offset + count : offset + 2 * count]
        if npos:
            pos = blob[offset + 2 * count : offset + 2 * count + npos]
            plist: list[Posting] = []
            start = 0
            for d, tf in zip(ids, tfs, strict=True):
                plist.append(Posting(d, tf, tuple(pos[start : start + tf])))
                start += tf
            postings[term] = plist
        else:
            postings[term] = [Posting(d, tf) for d, tf in zip(ids, tfs, strict=True)]
    index.postings = postings
    return index
