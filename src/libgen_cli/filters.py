"""Client-side post-parse filters: ``--ext``, ``--lang``, ``--year``.

Server-side filtering on libgen.li-family mirrors is unreliable (the documented
``ext:`` / ``lang:`` Google-mode modifiers don't actually narrow results in
practice), so we apply filters after parsing.

All filter inputs are case-insensitive. Multi-value flags accept comma-separated
values; pass an iterable of strings or a single comma-joined string and we'll
normalise it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from libgen_cli.errors import LibgenError
from libgen_cli.models import Book

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_YEAR_RANGE_RE = re.compile(r"^\s*(\d{4})\s*-\s*(\d{4})\s*$")


class FilterParseError(LibgenError):
    """Raised when a filter value cannot be parsed."""


def _normalise_csv(value: str | Iterable[str] | None) -> list[str]:
    """Accept ``"epub,pdf"``, ``["epub", "pdf"]``, ``["epub,pdf"]``; return ``["epub", "pdf"]``."""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = []
        for raw in value:
            items.extend(raw.split(","))
    return [item.strip().lower() for item in items if item.strip()]


def parse_year_filter(value: str | None) -> tuple[int | None, int | None]:
    """Parse a ``--year`` argument into ``(min, max)`` inclusive bounds.

    Accepted forms:

    - ``"2020"`` -> ``(2020, 2020)``
    - ``"2010-2020"`` -> ``(2010, 2020)``
    - ``"-2020"`` or ``"2020-"`` -> open-ended ranges
    - ``None`` or ``""`` -> ``(None, None)``
    """
    if not value:
        return (None, None)
    raw = value.strip()
    if raw.isdigit() and len(raw) == 4:
        y = int(raw)
        return (y, y)
    m = _YEAR_RANGE_RE.match(raw)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    if raw.endswith("-") and raw[:-1].isdigit():
        return (int(raw[:-1]), None)
    if raw.startswith("-") and raw[1:].isdigit():
        return (None, int(raw[1:]))
    raise FilterParseError(f"invalid --year value {value!r}; expected YYYY or YYYY-YYYY")


@dataclass(frozen=True, slots=True)
class BookFilter:
    """Composable client-side filter applied to parsed search results."""

    extensions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    year_min: int | None = None
    year_max: int | None = None

    @classmethod
    def from_options(
        cls,
        *,
        ext: str | Iterable[str] | None = None,
        lang: str | Iterable[str] | None = None,
        year: str | None = None,
    ) -> BookFilter:
        y_min, y_max = parse_year_filter(year)
        return cls(
            extensions=tuple(_normalise_csv(ext)),
            languages=tuple(_normalise_csv(lang)),
            year_min=y_min,
            year_max=y_max,
        )

    @property
    def is_empty(self) -> bool:
        return (
            not self.extensions
            and not self.languages
            and self.year_min is None
            and self.year_max is None
        )

    def matches(self, book: Book) -> bool:
        if self.extensions and book.extension.lower() not in self.extensions:
            return False
        if self.languages and not _lang_matches(book.language, self.languages):
            return False
        if self.year_min is not None or self.year_max is not None:
            years = [int(m.group(0)) for m in _YEAR_RE.finditer(book.year)]
            if not years:
                return False
            if self.year_min is not None and max(years) < self.year_min:
                return False
            if self.year_max is not None and min(years) > self.year_max:
                return False
        return True

    def apply(self, books: Iterable[Book]) -> list[Book]:
        if self.is_empty:
            return list(books)
        return [b for b in books if self.matches(b)]


def _lang_matches(book_language: str, requested: tuple[str, ...]) -> bool:
    """Case-insensitive prefix match.

    ``--lang en`` matches "English"; ``--lang english`` matches "English"; we
    do *not* substring-match to avoid false positives like "en" matching
    "French" via mid-word coincidence.
    """
    book_lower = book_language.lower().strip()
    if not book_lower:
        return False
    for target in requested:
        if not target:
            continue
        if book_lower == target or book_lower.startswith(target):
            return True
    return False
