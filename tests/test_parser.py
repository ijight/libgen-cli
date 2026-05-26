"""Parser tests against committed HTML fixtures."""

from __future__ import annotations

import re

import pytest

from libgen_cli.errors import ParseError
from libgen_cli.models import Topic
from libgen_cli.parser import (
    extract_keyed_download_url,
    parse_search_results,
)

_MD5_RE = re.compile(r"^[a-f0-9]{32}$")


def test_parse_nonfic_yields_results(nonfic_html: str) -> None:
    books = parse_search_results(nonfic_html, topic=Topic.NONFIC)
    assert len(books) >= 20
    for book in books:
        assert _MD5_RE.match(book.md5), f"bad md5 {book.md5!r}"
        assert book.topic is Topic.NONFIC
        assert book.title


def test_parse_fiction_yields_results(fiction_html: str) -> None:
    books = parse_search_results(fiction_html, topic=Topic.FICTION)
    assert len(books) >= 20
    for book in books:
        assert _MD5_RE.match(book.md5)
        assert book.topic is Topic.FICTION


def test_parse_uniqueness_and_metadata(nonfic_html: str) -> None:
    books = parse_search_results(nonfic_html, topic=Topic.NONFIC)
    md5s = [b.md5 for b in books]
    assert len(md5s) == len(set(md5s)), "duplicate MD5s found"
    extensions = {b.extension for b in books if b.extension}
    assert extensions, "expected at least one populated extension"


def test_parse_empty_results_returns_empty_list() -> None:
    html = "<html><body><form action='/index.php'><input name='req' value=''/></form></body></html>"
    assert parse_search_results(html) == []


def test_parse_unrecognised_layout_raises() -> None:
    with pytest.raises(ParseError):
        parse_search_results("<html><body><p>not a libgen page</p></body></html>")


def test_parse_blank_html_returns_empty_list() -> None:
    assert parse_search_results("") == []


def test_extract_keyed_url(book_page_html: str) -> None:
    url = extract_keyed_download_url(book_page_html, "https://libgen.li")
    assert url is not None
    assert "get.php?md5=" in url
    assert "key=" in url
    assert url.startswith("https://libgen.li/")


def test_extract_keyed_url_returns_none_when_absent() -> None:
    assert extract_keyed_download_url("<html></html>", "https://libgen.li") is None
