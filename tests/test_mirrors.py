"""Mirror probe and ranking tests."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import respx

from libgen_cli.http import make_client
from libgen_cli.mirrors import (
    DEFAULT_MIRRORS,
    normalise_mirror,
    probe_all,
    probe_and_rank,
    probe_one,
    rank_by_status,
    resolve_mirrors,
)
from libgen_cli.models import MirrorStatus


def test_normalise_strips_trailing_slash_and_adds_https() -> None:
    assert normalise_mirror("libgen.li") == "https://libgen.li"
    assert normalise_mirror("https://libgen.li/") == "https://libgen.li"
    assert normalise_mirror("https://libgen.li") == "https://libgen.li"


def test_normalise_rejects_garbage() -> None:
    assert normalise_mirror("") == ""
    assert normalise_mirror("not a url") == ""


def test_resolve_priority_cli_over_env_over_config_over_default(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIBGEN_MIRROR", "https://from-env")
    out = resolve_mirrors(cli_mirrors=["https://from-cli"])
    assert out[0] == "https://from-cli"
    assert "https://from-env" in out
    for default in DEFAULT_MIRRORS:
        assert default in out


def test_resolve_filters_http_by_default(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIBGEN_MIRROR", "http://insecure-mirror")
    out = resolve_mirrors()
    assert "http://insecure-mirror" not in out


def test_resolve_allows_http_when_explicit(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIBGEN_MIRROR", "http://insecure-mirror")
    out = resolve_mirrors(allow_http=True)
    assert "http://insecure-mirror" in out


@respx.mock
def test_probe_one_ok() -> None:
    respx.get("https://libgen.li/").respond(200, html="<html></html>")
    with make_client(timeout=5) as client:
        status = probe_one(client, "https://libgen.li", timeout=2)
    assert status.ok is True
    assert status.status_code == 200
    assert status.error is None


@respx.mock
def test_probe_one_handles_500() -> None:
    respx.get("https://libgen.li/").respond(500)
    with make_client(timeout=5) as client:
        status = probe_one(client, "https://libgen.li", timeout=2)
    assert status.ok is False
    assert status.status_code == 500
    assert status.error == "HTTP 500"


@respx.mock
def test_probe_one_handles_timeout() -> None:
    respx.get("https://libgen.li/").mock(side_effect=httpx.ConnectTimeout("boom"))
    with make_client(timeout=5) as client:
        status = probe_one(client, "https://libgen.li", timeout=2)
    assert status.ok is False
    assert "timeout" in (status.error or "").lower()


@respx.mock
def test_probe_all_rank_orders_by_health_then_latency() -> None:
    fast = "https://fast"
    slow = "https://slow"
    dead = "https://dead"

    respx.get(f"{fast}/").respond(200)

    def slow_handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.05)
        return httpx.Response(200)

    respx.get(f"{slow}/").mock(side_effect=slow_handler)
    respx.get(f"{dead}/").respond(503)

    with make_client(timeout=5) as client:
        statuses = probe_all(client, [fast, slow, dead], timeout=5)

    assert {s.url for s in statuses} == {fast, slow, dead}
    ranked = rank_by_status(statuses)
    assert ranked.index(fast) < ranked.index(slow)
    assert ranked.index(dead) == len(ranked) - 1


@respx.mock
def test_probe_and_rank_persists_healthy_only(
    isolated_config: Path,
) -> None:
    healthy = "https://healthy"
    broken = "https://broken"
    respx.get(f"{healthy}/").respond(200)
    respx.get(f"{broken}/").respond(503)

    with make_client(timeout=5) as client:
        statuses, ranked = probe_and_rank(client, [healthy, broken], timeout=5, persist=True)

    from libgen_cli.config import load_mirror_overrides

    persisted = load_mirror_overrides()
    assert persisted == [healthy]
    assert ranked[0] == healthy
    assert any(not s.ok for s in statuses)


def test_rank_by_status_pure_function() -> None:
    statuses = [
        MirrorStatus(url="b", ok=True, latency_ms=50.0),
        MirrorStatus(url="a", ok=True, latency_ms=10.0),
        MirrorStatus(url="c", ok=False, latency_ms=5.0),
    ]
    assert rank_by_status(statuses) == ["a", "b", "c"]
