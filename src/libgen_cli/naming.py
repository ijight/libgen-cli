"""Cross-platform-safe filename derivation for downloaded books."""

from __future__ import annotations

import re

from libgen_cli.models import Book

_INVALID_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_WHITESPACE = re.compile(r"\s+")
_RESERVED_WIN = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_STEM_LEN = 200


def sanitise_component(value: str) -> str:
    """Replace forbidden characters and collapse whitespace."""
    cleaned = _INVALID_CHARS.sub("", value)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip().strip(".")
    if cleaned.upper() in _RESERVED_WIN:
        cleaned = f"_{cleaned}"
    return cleaned


def filename_for(book: Book) -> str:
    """Render a deterministic, cross-platform-safe filename for the given Book.

    Format: ``{title} - {authors} ({year}).{ext}`` — fields collapse gracefully
    if they're empty. Falls back to the MD5 when the title is missing.
    """
    title = sanitise_component(book.title) or book.md5
    authors = sanitise_component(book.authors)
    year = sanitise_component(book.year)
    ext = sanitise_component(book.extension).lower() or "bin"

    parts = [title]
    if authors:
        parts.append(f"- {authors}")
    if year:
        parts.append(f"({year})")
    stem = " ".join(parts)

    if len(stem) > MAX_STEM_LEN:
        stem = stem[:MAX_STEM_LEN].rstrip()
    return f"{stem}.{ext}"
