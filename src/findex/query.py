"""Query language: a recursive-descent parser producing an operator-overloading tree.

Grammar (``AND`` is optional — juxtaposition means AND)::

    or_expr  := and_expr ('OR' and_expr)*
    and_expr := not_expr ('AND'? not_expr)*
    not_expr := 'NOT' not_expr | atom
    atom     := WORD | '"' PHRASE '"' | '(' or_expr ')'

``a OR b c`` parses as ``Or(a, And(b, c))``: AND binds tighter than OR, exactly like
``*`` binds tighter than ``+``, because ``and_expr`` sits *below* ``or_expr``.

Nodes compose with Python operators: ``Term("python") & (Term("async") | Term("await"))
& ~Term("java")`` builds the same tree as parsing the string.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from findex.tokenize import tokenize

if TYPE_CHECKING:
    from findex.index import Index


class QuerySyntaxError(ValueError):
    pass


class Node:
    def __and__(self, other: Node) -> And:
        return And(self, other)

    def __or__(self, other: Node) -> Or:
        return Or(self, other)

    def __invert__(self) -> Not:
        return Not(self)

    def evaluate(self, index: Index) -> frozenset[int]:
        raise NotImplementedError

    def terms(self) -> Iterator[str]:
        """Positive terms (not under a NOT) — the ones that contribute to the score."""
        return iter(())


@dataclass(frozen=True, slots=True)
class Term(Node):
    text: str

    def evaluate(self, index: Index) -> frozenset[int]:
        return frozenset(p.doc_id for p in index.get(self.text, ()))

    def terms(self) -> Iterator[str]:
        yield self.text


@dataclass(frozen=True, slots=True)
class Phrase(Node):
    words: tuple[str, ...]

    def evaluate(self, index: Index) -> frozenset[int]:
        if not index.has_positions:
            raise QuerySyntaxError("phrase queries need an index built with --positions")
        plists = [index.get(w, []) for w in self.words]
        if not all(plists):
            return frozenset()
        candidates = frozenset(p.doc_id for p in plists[0])
        for pl in plists[1:]:
            candidates &= frozenset(p.doc_id for p in pl)
        by_doc = [{p.doc_id: p.positions for p in pl if p.doc_id in candidates} for pl in plists]
        hits: set[int] = set()
        for doc_id in candidates:
            starts = set(by_doc[0][doc_id])
            for offset, positions in enumerate(by_doc[1:], 1):
                starts &= {pos - offset for pos in positions[doc_id]}
                if not starts:
                    break
            if starts:
                hits.add(doc_id)
        return frozenset(hits)

    def terms(self) -> Iterator[str]:
        yield from self.words


@dataclass(frozen=True, slots=True)
class And(Node):
    left: Node
    right: Node

    def evaluate(self, index: Index) -> frozenset[int]:
        # a AND NOT b is a difference, so we never materialize "all docs minus b"
        if isinstance(self.right, Not):
            return self.left.evaluate(index) - self.right.child.evaluate(index)
        if isinstance(self.left, Not):
            return self.right.evaluate(index) - self.left.child.evaluate(index)
        return self.left.evaluate(index) & self.right.evaluate(index)

    def terms(self) -> Iterator[str]:
        yield from self.left.terms()
        yield from self.right.terms()


@dataclass(frozen=True, slots=True)
class Or(Node):
    left: Node
    right: Node

    def evaluate(self, index: Index) -> frozenset[int]:
        return self.left.evaluate(index) | self.right.evaluate(index)

    def terms(self) -> Iterator[str]:
        yield from self.left.terms()
        yield from self.right.terms()


@dataclass(frozen=True, slots=True)
class Not(Node):
    child: Node

    def evaluate(self, index: Index) -> frozenset[int]:
        return frozenset(index.doc_ids) - self.child.evaluate(index)


# --- lexer + parser ---------------------------------------------------------------

_LEX = re.compile(r'\s*(?:(?P<phrase>"[^"]*")|(?P<paren>[()])|(?P<word>[^\s()"]+))')
KEYWORDS = {"AND", "OR", "NOT"}


def lex(query: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    query = query.strip()
    while pos < len(query):
        m = _LEX.match(query, pos)
        if not m:
            raise QuerySyntaxError(f"unterminated quote at position {pos}")
        tokens.append(m.group(m.lastgroup or "word"))
        pos = m.end()
    return tokens


def _word_node(raw: str) -> Node | None:
    """A query word tokenizes like document text; ``well-known`` → Phrase(well, known)."""
    words = tuple(tokenize(raw))
    if not words:
        return None
    return Term(words[0]) if len(words) == 1 else Phrase(words)


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> str:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def or_expr(self) -> Node:
        node = self.and_expr()
        while self.peek() == "OR":
            self.take()
            node = Or(node, self.and_expr())
        return node

    def and_expr(self) -> Node:
        node = self.not_expr()
        while (tok := self.peek()) is not None and tok not in ("OR", ")"):
            if tok == "AND":
                self.take()
            node = And(node, self.not_expr())
        return node

    def not_expr(self) -> Node:
        if self.peek() == "NOT":
            self.take()
            return Not(self.not_expr())
        return self.atom()

    def atom(self) -> Node:
        tok = self.peek()
        if tok is None:
            raise QuerySyntaxError("unexpected end of query")
        self.take()
        if tok == "(":
            node = self.or_expr()
            if self.peek() != ")":
                raise QuerySyntaxError("missing ')'")
            self.take()
            return node
        if tok == ")" or tok in KEYWORDS:
            raise QuerySyntaxError(f"unexpected {tok!r}")
        if tok.startswith('"'):
            words = tuple(tokenize(tok.strip('"')))
            if not words:
                raise QuerySyntaxError("empty phrase")
            return Term(words[0]) if len(words) == 1 else Phrase(words)
        node = _word_node(tok)
        if node is None:  # pure punctuation: skip it, parse what follows
            return self.atom()
        return node


def parse(query: str) -> Node:
    tokens = lex(query)
    if not tokens:
        raise QuerySyntaxError("empty query")
    parser = _Parser(tokens)
    node = parser.or_expr()
    if parser.peek() is not None:
        raise QuerySyntaxError(f"unexpected {parser.peek()!r}")
    return node
