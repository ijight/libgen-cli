"""Interactive multi-select picker built on questionary."""

from __future__ import annotations

from collections.abc import Sequence

import questionary

from libgen_cli.models import Book


def _format_choice(book: Book) -> str:
    title = book.title or book.md5
    parts = [title]
    if book.authors:
        parts.append(book.authors)
    meta_bits = []
    if book.year:
        meta_bits.append(book.year)
    if book.extension:
        meta_bits.append(book.extension)
    if book.size:
        meta_bits.append(book.size)
    if meta_bits:
        parts.append("(" + ", ".join(meta_bits) + ")")
    return " — ".join(parts)


def pick_books(books: Sequence[Book]) -> list[Book]:
    """Present a multi-select checkbox UI and return the chosen books.

    Returns an empty list if the user aborts (Ctrl-C or empty selection).
    """
    if not books:
        return []
    choices = [questionary.Choice(title=_format_choice(b), value=b.md5) for b in books]
    selected: list[str] | None = questionary.checkbox(
        "Select books to download (space to toggle, enter to confirm):",
        choices=choices,
    ).ask()
    if not selected:
        return []
    by_md5 = {b.md5: b for b in books}
    return [by_md5[md5] for md5 in selected if md5 in by_md5]
