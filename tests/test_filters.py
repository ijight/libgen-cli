"""Filter logic tests for ``BookFilter`` and ``parse_year_filter``."""

from __future__ import annotations

import pytest

from libgen_cli.filters import BookFilter, FilterParseError, parse_year_filter
from libgen_cli.models import Book, Topic


def _book(**overrides: object) -> Book:
    base = {
        "md5": "a" * 32,
        "title": "Sample",
        "extension": "pdf",
        "language": "English",
        "year": "2020",
        "topic": Topic.NONFIC,
    }
    base.update(overrides)
    return Book(**base)  # type: ignore[arg-type]


# ---------- parse_year_filter ----------------------------------------------


def test_year_single() -> None:
    assert parse_year_filter("2020") == (2020, 2020)


def test_year_range() -> None:
    assert parse_year_filter("2010-2020") == (2010, 2020)


def test_year_open_lower() -> None:
    assert parse_year_filter("-2020") == (None, 2020)


def test_year_open_upper() -> None:
    assert parse_year_filter("2010-") == (2010, None)


def test_year_blank() -> None:
    assert parse_year_filter(None) == (None, None)
    assert parse_year_filter("") == (None, None)


def test_year_invalid_raises() -> None:
    with pytest.raises(FilterParseError):
        parse_year_filter("twenty twenty")
    with pytest.raises(FilterParseError):
        parse_year_filter("123")


# ---------- BookFilter ------------------------------------------------------


def test_empty_filter_is_noop() -> None:
    f = BookFilter.from_options()
    assert f.is_empty
    assert f.matches(_book())


def test_ext_filter_single() -> None:
    f = BookFilter.from_options(ext="epub")
    assert not f.matches(_book(extension="pdf"))
    assert f.matches(_book(extension="EPUB"))


def test_ext_filter_csv() -> None:
    f = BookFilter.from_options(ext="epub,pdf")
    assert f.matches(_book(extension="pdf"))
    assert f.matches(_book(extension="epub"))
    assert not f.matches(_book(extension="cbz"))


def test_ext_filter_repeated_list() -> None:
    f = BookFilter.from_options(ext=["epub", "pdf"])
    assert f.matches(_book(extension="pdf"))
    assert not f.matches(_book(extension="mobi"))


def test_lang_filter_prefix() -> None:
    f = BookFilter.from_options(lang="en")
    assert f.matches(_book(language="English"))
    assert not f.matches(_book(language="Spanish"))


def test_lang_filter_full_word() -> None:
    f = BookFilter.from_options(lang="english")
    assert f.matches(_book(language="English"))
    assert not f.matches(_book(language="French"))


def test_lang_filter_csv() -> None:
    f = BookFilter.from_options(lang="en,fr")
    assert f.matches(_book(language="English"))
    assert f.matches(_book(language="French"))
    assert not f.matches(_book(language="German"))


def test_year_filter_exact_when_year_appears() -> None:
    f = BookFilter.from_options(year="2020")
    assert f.matches(_book(year="2020"))
    assert f.matches(_book(year="2020 March 14"))
    assert not f.matches(_book(year="2019"))


def test_year_filter_range() -> None:
    f = BookFilter.from_options(year="2010-2020")
    assert f.matches(_book(year="2015"))
    assert f.matches(_book(year="2010"))
    assert f.matches(_book(year="2020"))
    assert not f.matches(_book(year="2009"))
    assert not f.matches(_book(year="2021"))


def test_year_filter_skips_books_without_year() -> None:
    f = BookFilter.from_options(year="2020")
    assert not f.matches(_book(year=""))


def test_combined_filters_intersection() -> None:
    f = BookFilter.from_options(ext="epub", lang="en", year="2010-")
    assert f.matches(_book(extension="epub", language="English", year="2015"))
    assert not f.matches(
        _book(extension="pdf", language="English", year="2015"),
    )
    assert not f.matches(
        _book(extension="epub", language="French", year="2015"),
    )
    assert not f.matches(
        _book(extension="epub", language="English", year="2005"),
    )


def test_apply_returns_subset_in_order() -> None:
    books = [
        _book(md5="a" * 32, extension="epub"),
        _book(md5="b" * 32, extension="pdf"),
        _book(md5="c" * 32, extension="epub"),
    ]
    f = BookFilter.from_options(ext="epub")
    out = f.apply(books)
    assert [b.md5 for b in out] == ["a" * 32, "c" * 32]


def test_invalid_year_propagates_via_from_options() -> None:
    with pytest.raises(FilterParseError):
        BookFilter.from_options(year="bogus")
