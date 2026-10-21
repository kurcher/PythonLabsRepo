import inspect
import unicodedata

from findex.tokenize import tokenize


def toks(text: str) -> list[str]:
    return list(tokenize(text))


def test_is_a_generator():
    assert inspect.isgeneratorfunction(tokenize)


def test_empty_string():
    assert toks("") == []
    assert toks("  ... !!! — ") == []


def test_mixed_case_and_casefold():
    assert toks("Hello WORLD hElLo") == ["hello", "world", "hello"]
    assert toks("STRASSE Straße") == ["strasse", "strasse"]  # lower() would keep "ß"


def test_cyrillic():
    assert toks("Привіт, СВІТе! Їжак ґанок") == ["привіт", "світе", "їжак", "ґанок"]


def test_combining_mark_nfc():
    decomposed = "cafe\u0301"  # 'e' + COMBINING ACUTE ACCENT
    precomposed = "café"
    assert decomposed != precomposed
    assert toks(decomposed) == toks(precomposed) == ["café"]


def test_combining_mark_without_precomposed_form_is_dropped():
    stressed = unicodedata.normalize("NFC", "за\u0301мок")  # stays decomposed after NFC
    assert toks(stressed) == ["замок"]


def test_punctuation_splits():
    assert toks("end. Start,middle;(paren) [x] snake_case") == [
        "end",
        "start",
        "middle",
        "paren",
        "x",
        "snake",
        "case",
    ]


def test_apostrophes():
    assert toks("don't don’t 'quoted' п’ять пʼять") == [
        "don't",
        "don't",
        "quoted",
        "п'ять",
        "п'ять",
    ]


def test_hyphens_split():
    assert toks("well-known е-мейл") == ["well", "known", "е", "мейл"]


def test_digits():
    assert toks("In 1984 I paid 3.14 for an mp3") == [
        "in",
        "1984",
        "i",
        "paid",
        "3",
        "14",
        "for",
        "an",
        "mp3",
    ]
