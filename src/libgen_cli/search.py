"""Search orchestration: build URLs, fetch pages, fail over across mirrors.

The libgen.li-family unified search lives at::

    /index.php?req=...&topics[]=l&topics[]=f&res=...&view=simple

The ``topics[]=`` PHP-array notation is mandatory: a singular ``topics=l``
parameter is silently ignored by the backend, which then returns a comics-heavy
default ranking instead of the requested section. (We learned this the hard
way.)

Topic codes: ``l`` libgen (sci-tech), ``f`` fiction, ``c`` comics, ``a``
articles, ``m`` magazines, ``r`` russian fiction, ``s`` standards.
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


def _normalise_topics(topics: Iterable[Topic] | Topic) -> tuple[Topic, ...]:
    if isinstance(topics, Topic):
        return (topics,)
    out = tuple(topics)
    if not out:
        raise SearchError("at least one topic is required")
    return out


def build_search_url(
    mirror: str,
    query: str,
    *,
    topics: Iterable[Topic] | Topic = (Topic.NONFIC,),
    results_per_page: int = 25,
    page: int = 1,
    phrase: bool = True,
) -> str:
    """Build a single search URL hitting one or more topics in one request."""
    if not query.strip():
        raise SearchError("query must not be empty")
    topic_tuple = _normalise_topics(topics)
    if results_per_page not in ALLOWED_RESULTS_PER_PAGE:
        results_per_page = min(ALLOWED_RESULTS_PER_PAGE, key=lambda v: abs(v - results_per_page))

    params: list[tuple[str, str]] = [("req", query)]
    for t in topic_tuple:
        params.append(("topics[]", TOPIC_CODES[t]))
    params.extend(
        [
            ("res", str(results_per_page)),
            ("view", "simple"),
            ("column", "def"),
        ]
    )
    if phrase:
        params.append(("phrase", "1"))
    if page > 1:
        params.append(("page", str(page)))
    return f"{mirror.rstrip('/')}/index.php?{urlencode(params)}"


def _fetch_search(
    client: httpx.Client,
    mirror: str,
    query: str,
    topics: tuple[Topic, ...],
    *,
    results_per_page: int,
    page: int,
) -> list[Book]:
    url = build_search_url(
        mirror,
        query,
        topics=topics,
        results_per_page=results_per_page,
        page=page,
    )
    resp = client.get(url)
    resp.raise_for_status()
    primary_topic = topics[0]
    return parse_search_results(resp.text, topic=primary_topic)


def search(
    client: httpx.Client,
    mirrors: list[str],
    query: str,
    *,
    topics: Iterable[Topic] = (Topic.NONFIC, Topic.FICTION),
    results_per_page: int = 25,
    page: int = 1,
) -> list[Book]:
    """Search ``query`` across the requested topics, falling over across mirrors.

    All topics are sent in a single request (``topics[]=l&topics[]=f``) so we
    only do one HTTP round-trip per attempted mirror.
    """
    if not mirrors:
        raise NoMirrorsAvailableError("no mirrors available for search")
    topic_tuple = _normalise_topics(topics)

    last_error: Exception | None = None
    for mirror in mirrors:
        try:
            return _fetch_search(
                client,
                mirror,
                query,
                topic_tuple,
                results_per_page=results_per_page,
                page=page,
            )
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            continue

    raise NoMirrorsAvailableError(f"all mirrors failed for search: {last_error}")


def search_topic(
    client: httpx.Client,
    mirrors: list[str],
    query: str,
    topic: Topic,
    *,
    results_per_page: int = 25,
    page: int = 1,
) -> tuple[list[Book], str]:
    """Single-topic convenience wrapper. Returns ``(books, mirror_used)``."""
    if not mirrors:
        raise NoMirrorsAvailableError("no mirrors available for search")

    last_error: Exception | None = None
    for mirror in mirrors:
        try:
            books = _fetch_search(
                client,
                mirror,
                query,
                (topic,),
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
    """Look up a single Book record by MD5 across the requested topics.

    Issues a single multi-topic request per mirror; returns the first row whose
    MD5 matches.
    """
    md5 = md5.lower().strip()
    if len(md5) != 32:
        return None
    try:
        books = search(
            client,
            mirrors,
            f"md5:{md5}",
            topics=topics,
            results_per_page=25,
            page=1,
        )
    except NoMirrorsAvailableError:
        return None
    for book in books:
        if book.md5 == md5:
            return book
    return None
