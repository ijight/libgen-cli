"""Config persistence tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from libgen_cli.config import (
    config_path,
    env_mirror_overrides,
    load_mirror_overrides,
    save_mirror_ranking,
)
from libgen_cli.errors import ConfigError


def test_load_returns_empty_when_no_file(isolated_config: Path) -> None:
    assert load_mirror_overrides() == []


def test_save_then_load_roundtrip(isolated_config: Path) -> None:
    mirrors = ["https://libgen.li", "https://libgen.la"]
    written = save_mirror_ranking(mirrors)
    assert written == config_path()
    assert load_mirror_overrides() == mirrors


def test_save_overwrites_existing(isolated_config: Path) -> None:
    save_mirror_ranking(["https://a"])
    save_mirror_ranking(["https://b"])
    assert load_mirror_overrides() == ["https://b"]


def test_load_raises_on_malformed_toml(isolated_config: Path) -> None:
    cfg = config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("mirrors = [\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_mirror_overrides()


def test_load_tolerates_unexpected_shapes(isolated_config: Path) -> None:
    cfg = config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("mirrors = 'not-a-list'\n", encoding="utf-8")
    assert load_mirror_overrides() == []


def test_env_overrides_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIBGEN_MIRROR", " https://a , https://b ,, ")
    assert env_mirror_overrides() == ["https://a", "https://b"]


def test_env_overrides_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIBGEN_MIRROR", raising=False)
    assert env_mirror_overrides() == []
