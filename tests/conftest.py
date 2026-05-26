"""Shared pytest fixtures for libgen-cli tests."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def nonfic_html() -> str:
    return (FIXTURE_DIR / "search_nonfic.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def fiction_html() -> str:
    return (FIXTURE_DIR / "search_fiction.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def book_page_html() -> str:
    return (FIXTURE_DIR / "book_page_ads.html").read_text(encoding="utf-8")


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect XDG_CONFIG_HOME to a tmp dir so config writes don't pollute the user."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LIBGEN_MIRROR", raising=False)
    return tmp_path
