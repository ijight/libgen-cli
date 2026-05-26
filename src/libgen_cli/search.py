"""Search orchestration: build URLs, fetch pages, fail over across mirrors.

The libgen.li-family unified search lives at
``/index.php?req=...&topics=...&res=...&view=simple``. We hit one mirror at a
time, in ranked order, returning the first successful parse. ``topics='l'`` is
non-fiction; ``'f'`` is fiction. Multi-topic searches issue one request per
topic and merge by MD5 (preserving first-seen order).
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlencode

import httpx

from libgen_cli.errors import NoMirrorsAvailableError, SearchError
from libgen_cli.models import Book, Topic
from libgen_cli.parser import parse_search_results

TOPIC_CODES: dict[Topic, str] = {
    Topic.NONFIC: "l",
    Topic.FICTION: "f",
}

ALLOWED_RESULTS_PER_PAGE = (25, 50, 100)


def build_search_url(
    mirror: str,
    query: str,
    *,
    topic: Topic = Topic.NONFIC,
    results_per_page: int = 25,
    page: int = 1,
    phrase: bool = True,
) -> str:
    """Build a single search URL on the given mirror."""
    if not query.strip():
        raise SearchError("query must not be empty")
    if results_per_page not in ALLOWED_RESULTS_PER_PAGE:
        results_per_page = min(ALLOWED_RESULTS_PER_PAGE, key=lambda v: abs(v - results_per_page))
    params = {
        "req": query,
        "topics": TOPIC_CODES[topic],
        "res": str(results_per_page),
        "view": "simple",
        "column": "def",
    }
    if phrase:
        params["phrase"] = "1"
    if page > 1:
        params["page"] = str(page)
    return f"{mirror.rstrip('/')}/index.php?{urlencode(params)}"


def _search_topic_on_mirror(
    client: httpx.Client,
    mirror: str,
    query: str,
    topic: Topic,
    *,
    results_per_page: int,
    page: int,
) -> list[Book]:
    url = build_search_url(
        mirror,
        query,
        topic=topic,
        results_per_page=results_per_page,
        page=page,
    )
    resp = client.get(url)
    resp.raise_for_status()
    return parse_search_results(resp.text, topic=topic)


def search_topic(
    client: httpx.Client,
    mirrors: list[str],
    query: str,
    topic: Topic,
    *,
    results_per_page: int = 25,
    page: int = 1,
) -> tuple[list[Book], str]:
    """Search a single ``topic`` across mirrors in order; return ``(books, mirror_used)``.

    Raises :class:`NoMirrorsAvailableError` if every mirror fails (network or HTTP
    error). A successful 200 with zero parsed rows is *not* a failure — it just
    means no matches.
    """
    if not mirrors:
        raise NoMirrorsAvailableError("no mirrors available for search")

    last_error: Exception | None = None
    for mirror in mirrors:
        try:
            books = _search_topic_on_mirror(
                client,
                mirror,
                query,
                topic,
                results_per_page=results_per_page,
                page=page,
            )
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            continue
        return books, mirror

    raise NoMirrorsAvailableError(f"all mirrors failed for topic {topic.value!r}: {last_error}")


def lookup_by_md5(
    client: httpx.Client,
    mirrors: list[str],
    md5: str,
    *,
    topics: Iterable[Topic] = (Topic.NONFIC, Topic.FICTION),
) -> Book | None:
    """Look up a single Book record by MD5.

    Libgen's ``req=md5:<hash>`` search returns the matching row; we try each
    requested topic until one yields a hit. Returns ``None`` if no topic does.
    """
    md5 = md5.lower().strip()
    if len(md5) != 32:
        return None
    for topic in topics:
        try:
            books, _ = search_topic(
                client,
                mirrors,
                f"md5:{md5}",
                topic,
                results_per_page=25,
                page=1,
            )
        except NoMirrorsAvailableError:
            continue
        for book in books:
            if book.md5 == md5:
                return book
    return None


def search(
    client: httpx.Client,
    mirrors: list[str],
    query: str,
    *,
    topics: Iterable[Topic] = (Topic.NONFIC, Topic.FICTION),
    results_per_page: int = 25,
    page: int = 1,
) -> list[Book]:
    """Search ``query`` across each requested topic, dedupe by MD5, preserve order."""
    seen: set[str] = set()
    out: list[Book] = []
    for topic in topics:
        try:
            books, _ = search_topic(
                client,
                mirrors,
                query,
                topic,
                results_per_page=results_per_page,
                page=page,
            )
        except NoMirrorsAvailableError:
            if not out:
                raise
            continue
        for b in books:
            if b.md5 in seen:
                continue
            seen.add(b.md5)
            out.append(b)
    return out
