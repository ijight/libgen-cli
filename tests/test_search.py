"""Search orchestration tests with respx-mocked HTTP."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

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


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_build_search_url_uses_php_array_notation() -> None:
    url = build_search_url("https://libgen.li", "tolkien", topics=Topic.FICTION)
    assert url.startswith("https://libgen.li/index.php?")
    params = _params(url)
    assert params["topics[]"] == ["f"]
    assert "topics" not in params, "must use topics[] not topics="
    assert params["req"] == ["tolkien"]
    assert params["view"] == ["simple"]
    assert params["phrase"] == ["1"]


def test_build_search_url_multi_topic_in_one_request() -> None:
    url = build_search_url("https://libgen.li", "x", topics=(Topic.NONFIC, Topic.FICTION))
    params = _params(url)
    assert sorted(params["topics[]"]) == ["f", "l"]


def test_build_search_url_paging_and_results() -> None:
    url = build_search_url(
        "https://libgen.li",
        "rust",
        topics=Topic.NONFIC,
        results_per_page=50,
        page=3,
    )
    params = _params(url)
    assert params["topics[]"] == ["l"]
    assert params["res"] == ["50"]
    assert params["page"] == ["3"]


def test_build_search_url_rejects_empty_query() -> None:
    with pytest.raises(SearchError):
        build_search_url("https://libgen.li", "   ")


def test_build_search_url_rejects_empty_topics() -> None:
    with pytest.raises(SearchError):
        build_search_url("https://libgen.li", "x", topics=())


def test_build_search_url_clamps_results_to_allowed() -> None:
    url = build_search_url("https://libgen.li", "x", results_per_page=42)
    params = _params(url)
    assert params["res"][0] in {"25", "50"}


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
def test_search_sends_topics_array_in_single_request(
    nonfic_html: str,
) -> None:
    """One HTTP call per attempted mirror with all topics packed in topics[]."""
    seen_topic_lists: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_topic_lists.append(request.url.params.get_list("topics[]"))
        return httpx.Response(200, text=nonfic_html)

    route = respx.get("https://libgen.li/index.php").mock(side_effect=handler)
    with make_client() as client:
        books = search(
            client, ["https://libgen.li"], "python", topics=(Topic.NONFIC, Topic.FICTION)
        )
    assert route.call_count == 1, "expected a single HTTP request per mirror"
    assert seen_topic_lists == [["l", "f"]]
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
