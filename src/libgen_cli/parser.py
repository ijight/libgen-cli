"""HTML parsing for libgen.li-family search-result pages.

The modern libgen mirrors all serve a unified search at::

    /index.php?req={query}&topics={l|f|...}&res={n}&view=simple

Result rows live inside ``<table id="tablelibgen">`` and come in two flavours:

1. **Primary edition row** — 9 ``<td>`` cells with full metadata
   (title/series, authors, publisher, year, language, pages, size, ext, mirrors).
2. **Alternate file row** — a ``<td colspan="5">`` that carries a different file
   (often a different format) belonging to the same logical edition. Cells are
   abbreviated; we still surface them as standalone Books because each has its
   own MD5 and is independently downloadable.

This module is intentionally tolerant: missing fields become empty strings
rather than failing the whole parse. Only a missing MD5 disqualifies a row.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser, Node

from libgen_cli.errors import ParseError
from libgen_cli.models import Book, Topic

_MD5_RE = re.compile(r"md5=([a-fA-F0-9]{32})")
_LIBGEN_ID_RE = re.compile(r"ID:\s*(\d+)")


def _text(node: Node | None) -> str:
    if node is None:
        return ""
    return node.text(strip=True)


def _extract_md5(row: Node) -> str | None:
    html = row.html or ""
    m = _MD5_RE.search(html)
    return m.group(1).lower() if m else None


def _extract_libgen_id(row: Node) -> str:
    """Pull the numeric libgen ID from the tooltip ``title`` attribute, if present."""
    for el in row.css("[title]"):
        title = el.attributes.get("title") or ""
        m = _LIBGEN_ID_RE.search(title)
        if m:
            return m.group(1)
    return ""


def _extract_primary_title(first_td: Node) -> str:
    """Title extraction for the 9-cell primary row.

    Layout is roughly::

        <b>
          <a href="series.php?...">SERIES_NAME</a>
          <a href="edition.php?..."><i>ISSUE_OR_SUBTITLE</i></a>
        </b>
        <br>
        <a href="edition.php?..."><i></i>MAIN_TITLE</a>

    We prefer the post-``<br>`` link's text as the main title and fall back
    to the series anchor when there's nothing else.
    """
    bold = first_td.css_first("b")
    series_link = bold.css_first("a") if bold is not None else None
    series_name = _text(series_link)

    main_title = ""
    for a in first_td.css("a"):
        href = a.attributes.get("href") or ""
        if "edition.php" not in href:
            continue
        anchor_text = a.text(strip=True)
        if not anchor_text:
            continue
        if anchor_text == series_name:
            continue
        anchor_text = anchor_text.lstrip("#").strip()
        if anchor_text:
            main_title = anchor_text
            break

    if main_title and series_name and main_title != series_name:
        return f"{series_name} — {main_title}" if series_name else main_title
    return main_title or series_name


def _extract_alternate_title(first_td: Node) -> str:
    """Title for the 5-cell alternate row sits inside the colspan'd ``<span>``."""
    span = first_td.css_first("span")
    if span is None:
        return _text(first_td).split("\n", 1)[0].strip()
    fonts = span.css("font")
    for f in fonts:
        f.decompose()
    return span.text(strip=True)


def _parse_primary_row(row: Node, topic: Topic, md5: str) -> Book:
    tds = row.css("td")
    title = _extract_primary_title(tds[0])
    authors = _text(tds[1])
    publisher = _text(tds[2])
    year = _text(tds[3])
    language = _text(tds[4])
    pages = _text(tds[5])
    size = _text(tds[6])
    extension = _text(tds[7]).lower()
    return Book(
        md5=md5,
        title=title,
        authors=authors,
        publisher=publisher,
        year=year,
        language=language,
        pages=pages,
        size=size,
        extension=extension,
        topic=topic,
        libgen_id=_extract_libgen_id(row),
    )


def _parse_alternate_row(row: Node, topic: Topic, md5: str) -> Book:
    tds = row.css("td")
    title = _extract_alternate_title(tds[0])
    pages = _text(tds[1]) if len(tds) > 1 else ""
    size = _text(tds[2]) if len(tds) > 2 else ""
    extension = _text(tds[3]).lower() if len(tds) > 3 else ""
    return Book(
        md5=md5,
        title=title,
        pages=pages,
        size=size,
        extension=extension,
        topic=topic,
        libgen_id=_extract_libgen_id(row),
    )


def parse_search_results(html: str, topic: Topic = Topic.NONFIC) -> list[Book]:
    """Parse a search-results HTML page into a list of :class:`Book`.

    Returns an empty list when the page is well-formed but contains zero
    results. Raises :class:`ParseError` only when the result table is missing
    entirely (suggesting a layout change or a non-result page).
    """
    if not html:
        return []
    tree = HTMLParser(html)
    table = tree.css_first("#tablelibgen")
    if table is None:
        if tree.css_first('form input[name="req"]') is not None:
            return []
        raise ParseError("could not find #tablelibgen result table in HTML")

    books: list[Book] = []
    for row in table.css("tr"):
        tds = row.css("td")
        if not tds:
            continue
        md5 = _extract_md5(row)
        if not md5:
            continue
        is_alternate = bool(tds[0].attributes.get("colspan"))
        try:
            if is_alternate:
                books.append(_parse_alternate_row(row, topic, md5))
            elif len(tds) >= 9:
                books.append(_parse_primary_row(row, topic, md5))
            else:
                books.append(_parse_alternate_row(row, topic, md5))
        except (IndexError, AttributeError):
            continue
    return books


_KEYED_LINK_RE = re.compile(
    r'href="(?P<href>(?:https?://[^"]+/)?get\.php\?md5=(?P<md5>[a-fA-F0-9]{32})&(?:amp;)?key=(?P<key>[A-Za-z0-9]+))"'
)


def extract_keyed_download_url(html: str, mirror_base: str) -> str | None:
    """Extract a fully qualified ``get.php?md5=...&key=...`` URL from an ``ads.php`` page.

    Returns an absolute URL anchored to ``mirror_base`` when the link in the page
    is relative. Returns ``None`` if no keyed link is present.
    """
    if not html:
        return None
    m = _KEYED_LINK_RE.search(html)
    if not m:
        return None
    href = m.group("href").replace("&amp;", "&")
    if href.startswith("http://") or href.startswith("https://"):
        return href
    base = mirror_base.rstrip("/")
    if href.startswith("/"):
        return base + href
    return f"{base}/{href}"
