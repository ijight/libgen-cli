"""Anna's Archive search and download integration.

Search hits ``/search?q=...`` on the annas-archive.gl mirror (cached, no JS
challenge needed) and parses the card-based result list.  Downloads use the
free ``/slow_download/{md5}/0/{idx}`` partner-server endpoints which redirect
to an external file host.

The download endpoints require a DDoS-Guard JS challenge from some networks;
the code tries each of the 8 slow-partner slots across all mirrors and returns
None if every attempt is blocked, allowing the caller to fall back to libgen.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import quote_plus

import httpx
from selectolax.parser import HTMLParser, Node

from libgen_cli.errors import SearchError
from libgen_cli.models import Book, Topic

DEFAULT_AA_MIRRORS: tuple[str, ...] = (
    "https://annas-archive.gl",
)

_AA_SLOW_SERVER_COUNT = 8

_MD5_PATH_RE = re.compile(r"/md5/([a-f0-9]{32})", re.I)
_META_DOT = "·"  # · middle dot separator used in the metadata line
_META_RE = re.compile(
    r"(?P<lang>[A-Za-z][a-z]*)(?:\s+\[[a-z]{2}\])?\s*·\s*"
    r"(?P<ext>[A-Za-z0-9+]+)\s*·\s*"
    r"(?P<size>[0-9][0-9.,]*\s*[KMGT]?B)\s*·\s*"
    r"(?P<year>\d{4})",
    re.I,
)
_FICTION_RE = re.compile(r"fiction", re.I)


def build_aa_search_url(
    mirror: str,
    query: str,
    *,
    ext: str | None = None,
    lang: str | None = None,
) -> str:
    base = mirror.rstrip("/")
    params = f"q={quote_plus(query)}"
    if ext:
        params += f"&ext={quote_plus(ext.lower())}"
    if lang:
        params += f"&lang={quote_plus(lang.lower())}"
    return f"{base}/search?{params}"


def _parse_aa_card(card: Node) -> Book | None:
    # --- MD5 and title ---
    md5 = ""
    title = ""
    for a in card.css("a[href]"):
        href = a.attributes.get("href") or ""
        m = _MD5_PATH_RE.search(href)
        if not m:
            continue
        if not md5:
            md5 = m.group(1).lower()
        text = a.text(strip=True)
        if text and not title:
            title = text

    if not md5:
        return None

    # --- Author and publisher from search links ---
    author = ""
    publisher_year = ""
    for a in card.css('a[href*="/search?q="]'):
        raw_html = a.html or ""
        text = a.text(strip=True)
        if not text:
            continue
        if "mdi--user-edit" in raw_html:
            author = text
        elif "mdi--company" in raw_html:
            publisher_year = text

    publisher = ""
    year = ""
    if publisher_year:
        # "PublisherName, 1969"
        if ", " in publisher_year:
            parts = publisher_year.rsplit(", ", 1)
            if parts[1].isdigit() and len(parts[1]) == 4:
                publisher = parts[0]
                year = parts[1]
            else:
                publisher = publisher_year
        else:
            publisher = publisher_year

    # --- Language / extension / size from the metadata summary line ---
    lang = ""
    ext = ""
    size = ""
    topic = Topic.NONFIC
    for el in card.css("div"):
        text = el.text(strip=True)
        if _META_DOT not in text:
            continue
        m = _META_RE.search(text)
        if not m:
            continue
        lang = m.group("lang")
        ext = m.group("ext").lower()
        size = m.group("size")
        if not year:
            year = m.group("year")
        if _FICTION_RE.search(text):
            topic = Topic.FICTION
        break

    return Book(
        md5=md5,
        title=title,
        authors=author,
        publisher=publisher,
        year=year,
        language=lang,
        size=size,
        extension=ext,
        topic=topic,
        source="annas-archive",
    )


def parse_aa_search_results(html: str) -> list[Book]:
    """Parse an Anna's Archive search-results page into a list of :class:`Book`."""
    if not html:
        return []
    tree = HTMLParser(html)
    books: list[Book] = []
    for card in tree.css("div.flex.pt-3.pb-3"):
        try:
            book = _parse_aa_card(card)
            if book and book.md5:
                books.append(book)
        except Exception:
            continue
    return books


def search_aa(
    client: httpx.Client,
    mirrors: Sequence[str],
    query: str,
    *,
    ext: str | None = None,
    lang: str | None = None,
) -> list[Book]:
    """Search Anna's Archive, falling back across mirrors on failure.

    Returns an empty list (never raises) so callers can treat it as optional
    enrichment on top of a libgen search.
    """
    last_exc: Exception | None = None
    for mirror in mirrors:
        url = build_aa_search_url(mirror, query, ext=ext, lang=lang)
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            last_exc = exc
            continue
        books = parse_aa_search_results(resp.text)
        if books:
            return books
        # Empty result from this mirror — try next (might be a challenge page)
        last_exc = SearchError(f"no results from {mirror}")
    return []


def _parse_aa_slow_download_links(html: str, base: str, md5: str) -> list[str]:
    """Extract /slow_download/ hrefs from an AA book detail page."""
    if not html:
        return []
    tree = HTMLParser(html)
    links: list[str] = []
    seen: set[str] = set()
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if f"/slow_download/{md5}" not in href.lower():
            continue
        if href.startswith("/"):
            url = f"{base}{href}"
        elif href.startswith("http"):
            url = href
        else:
            continue
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def resolve_aa_download_url(
    client: httpx.Client,
    mirrors: Sequence[str],
    md5: str,
) -> str | None:
    """Try the free slow-partner-server download endpoints on AA mirrors.

    Returns a direct (binary-serving) URL if one of the slots responds
    correctly, otherwise None.

    GETs the book detail page first: warms the DDoS-Guard session AND
    extracts the actual slow_download links the page advertises (falling back
    to constructed URLs if the page is inaccessible).  Uses a range GET
    (bytes=0-0) instead of HEAD because some partner servers reject HEAD.
    """
    from libgen_cli.download import _is_html  # local import to avoid circularity

    for mirror in mirrors:
        base = mirror.rstrip("/")
        book_page_url = f"{base}/md5/{md5}"

        page_html = ""
        try:
            page_resp = client.get(book_page_url)
            if page_resp.status_code < 400:
                page_html = page_resp.text
        except httpx.HTTPError:
            pass

        links = _parse_aa_slow_download_links(page_html, base, md5)
        if not links:
            links = [f"{base}/slow_download/{md5}/0/{idx}" for idx in range(_AA_SLOW_SERVER_COUNT)]

        for url in links:
            try:
                resp = client.get(
                    url,
                    follow_redirects=True,
                    headers={"Referer": book_page_url, "Range": "bytes=0-0"},
                )
            except httpx.HTTPError:
                continue
            # 206 = partial content (range accepted), 200 = full response, 416 = range not supported
            # but file server is alive; all three mean the URL is live and non-HTML
            if resp.status_code in (200, 206, 416) and not _is_html(resp.headers.get("content-type")):
                return str(resp.url)
    return None
