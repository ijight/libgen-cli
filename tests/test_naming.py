"""Filename-derivation tests."""

from __future__ import annotations

from libgen_cli.models import Book, Topic
from libgen_cli.naming import filename_for, sanitise_component


def _book(**kwargs: object) -> Book:
    base = {
        "md5": "a" * 32,
        "title": "X",
        "extension": "pdf",
        "topic": Topic.NONFIC,
    }
    base.update(kwargs)
    return Book(**base)  # type: ignore[arg-type]


def test_sanitise_strips_invalid_chars() -> None:
    assert sanitise_component('a/b\\c:d*e?f<g>h|i"j') == "abcdefghij"


def test_sanitise_collapses_whitespace() -> None:
    assert sanitise_component("  hello   world  ") == "hello world"


def test_sanitise_handles_reserved_windows_names() -> None:
    out = sanitise_component("CON")
    assert out == "_CON"


def test_filename_full_metadata() -> None:
    b = _book(title="The Selfish Gene", authors="Richard Dawkins", year="1976", extension="EPUB")
    assert filename_for(b) == "The Selfish Gene - Richard Dawkins (1976).epub"


def test_filename_falls_back_to_md5_when_title_empty() -> None:
    b = _book(title="", extension="pdf")
    assert filename_for(b) == ("a" * 32 + ".pdf")


def test_filename_truncates_long_title() -> None:
    b = _book(title="X" * 500, extension="pdf")
    name = filename_for(b)
    stem = name.rsplit(".", 1)[0]
    assert len(stem) <= 200


def test_filename_unknown_extension_uses_bin() -> None:
    b = _book(extension="")
    assert filename_for(b).endswith(".bin")
