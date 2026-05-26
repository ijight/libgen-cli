"""Search orchestration tests with respx-mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from libgen_cli.errors import NoMirrorsAvailableError, SearchError
from libgen_cli.http import make_client
from libgen_cli.models import Topic
from libgen_cli.search import (
    build_search_url,
    lookup_by_md5,
    search,
    search_topic,
)


def test_build_search_url_basic() -> None:
    url = build_search_url("https://libgen.li", "tolkien", topic=Topic.FICTION)
    assert url.startswith("https://libgen.li/index.php?")
    assert "topics=f" in url
    assert "req=tolkien" in url
    assert "view=simple" in url
    assert "phrase=1" in url


def test_build_search_url_paging_and_results() -> None:
    url = build_search_url(
        "https://libgen.li",
        "rust",
        topic=Topic.NONFIC,
        results_per_page=50,
        page=3,
    )
    assert "topics=l" in url
    assert "res=50" in url
    assert "page=3" in url


def test_build_search_url_rejects_empty_query() -> None:
    with pytest.raises(SearchError):
        build_search_url("https://libgen.li", "   ")


def test_build_search_url_clamps_results_to_allowed() -> None:
    url = build_search_url("https://libgen.li", "x", results_per_page=42)
    assert "res=50" in url or "res=25" in url


@respx.mock
def test_search_topic_returns_books(nonfic_html: str) -> None:
    route = respx.get("https://libgen.li/index.php").respond(200, text=nonfic_html)
    with make_client() as client:
        books, mirror = search_topic(client, ["https://libgen.li"], "python", Topic.NONFIC)
    assert route.called
    assert mirror == "https://libgen.li"
    assert books, "expected non-empty book list"
    assert all(b.topic is Topic.NONFIC for b in books)


@respx.mock
def test_search_topic_falls_over_to_next_mirror(nonfic_html: str) -> None:
    respx.get("https://libgen.li/index.php").mock(side_effect=httpx.ConnectError("down"))
    respx.get("https://libgen.la/index.php").respond(200, text=nonfic_html)
    with make_client() as client:
        books, mirror = search_topic(
            client,
            ["https://libgen.li", "https://libgen.la"],
            "python",
            Topic.NONFIC,
        )
    assert mirror == "https://libgen.la"
    assert books


@respx.mock
def test_search_topic_raises_when_all_mirrors_fail() -> None:
    respx.get("https://libgen.li/index.php").mock(side_effect=httpx.ConnectError("down"))
    respx.get("https://libgen.la/index.php").respond(500)
    with make_client() as client, pytest.raises(NoMirrorsAvailableError):
        search_topic(
            client,
            ["https://libgen.li", "https://libgen.la"],
            "python",
            Topic.NONFIC,
        )


@respx.mock
def test_search_combines_topics_and_dedupes_md5(
    nonfic_html: str,
    fiction_html: str,
) -> None:
    """The same MD5 returned for both topics appears only once."""

    def handler(request: httpx.Request) -> httpx.Response:
        topics = request.url.params.get("topics")
        if topics == "l":
            return httpx.Response(200, text=nonfic_html)
        if topics == "f":
            return httpx.Response(200, text=fiction_html)
        return httpx.Response(404)

    respx.get("https://libgen.li/index.php").mock(side_effect=handler)
    with make_client() as client:
        books = search(client, ["https://libgen.li"], "python")
    md5s = [b.md5 for b in books]
    assert len(md5s) == len(set(md5s))


@respx.mock
def test_lookup_by_md5_returns_match(nonfic_html: str) -> None:
    respx.get("https://libgen.li/index.php").respond(200, text=nonfic_html)
    target = "44a7f3a19a9cd60fc54a1d9322f38120"
    with make_client() as client:
        book = lookup_by_md5(client, ["https://libgen.li"], target)
    assert book is not None
    assert book.md5 == target


@respx.mock
def test_lookup_by_md5_returns_none_when_missing(nonfic_html: str) -> None:
    respx.get("https://libgen.li/index.php").respond(200, text=nonfic_html)
    with make_client() as client:
        book = lookup_by_md5(client, ["https://libgen.li"], "f" * 32)
    assert book is None


def test_lookup_by_md5_rejects_invalid_length() -> None:
    with make_client() as client:
        assert lookup_by_md5(client, ["https://libgen.li"], "abc") is None
