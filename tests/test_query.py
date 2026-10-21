import pytest

from findex.query import And, Not, Or, Phrase, QuerySyntaxError, Term, parse


def test_and_binds_tighter_than_or():
    assert parse("a OR b c") == Or(Term("a"), And(Term("b"), Term("c")))


def test_explicit_and_and_left_associativity():
    assert parse("a AND b AND c") == And(And(Term("a"), Term("b")), Term("c"))
    assert parse("a OR b OR c") == Or(Or(Term("a"), Term("b")), Term("c"))


def test_not_and_parentheses():
    expected = Term("python") & (Term("async") | Term("await")) & ~Term("java")
    assert parse("python AND (async OR await) NOT java") == expected


def test_phrase_and_normalization():
    assert parse('"Event Loop" python') == And(Phrase(("event", "loop")), Term("python"))
    assert parse("Well-Known") == Phrase(("well", "known"))
    assert parse('"Whale"') == Term("whale")


def test_lowercase_keywords_are_terms():
    assert parse("cats or dogs") == And(And(Term("cats"), Term("or")), Term("dogs"))


@pytest.mark.parametrize("bad", ["", "a OR", "(a b", "a )", 'a "b', '""', "NOT"])
def test_syntax_errors(bad: str):
    with pytest.raises(QuerySyntaxError):
        parse(bad)


def test_double_not():
    assert parse("NOT NOT a") == Not(Not(Term("a")))
