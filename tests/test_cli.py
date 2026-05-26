"""End-to-end CLI smoke tests using Typer's CliRunner + respx."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import respx
from typer.testing import CliRunner

from libgen_cli.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "libgen-cli" in result.output


def test_no_args_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "search" in result.output
    assert "download" in result.output


def test_mirrors_command_lists_defaults(runner: CliRunner, isolated_config: Path) -> None:
    result = runner.invoke(app, ["mirrors"])
    assert result.exit_code == 0, result.output
    assert "libgen.li" in result.output


@respx.mock
def test_search_command_emits_ndjson_when_piped(
    runner: CliRunner,
    nonfic_html: str,
    fiction_html: str,
    isolated_config: Path,
) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        topics = request.url.params.get("topics")
        if topics == "l":
            return httpx.Response(200, text=nonfic_html)
        if topics == "f":
            return httpx.Response(200, text=fiction_html)
        return httpx.Response(404)

    respx.get("https://libgen.li/index.php").mock(side_effect=handler)
    respx.route().pass_through()

    result = runner.invoke(
        app,
        ["search", "tolkien", "--json", "-m", "https://libgen.li"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "expected NDJSON output on stdout"
    sample = json.loads(lines[0])
    assert "md5" in sample
    assert "title" in sample


@respx.mock
def test_link_command_prints_resolved_url(runner: CliRunner, isolated_config: Path) -> None:
    md5 = hashlib.md5(b"x").hexdigest()
    respx.head(f"https://libgen.li/get.php?md5={md5}").respond(
        200, headers={"content-type": "application/octet-stream"}
    )
    result = runner.invoke(
        app,
        ["link", md5, "-m", "https://libgen.li"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stderr
    assert md5 in result.stdout


@respx.mock
def test_download_dry_run(
    runner: CliRunner,
    tmp_path: Path,
    isolated_config: Path,
) -> None:
    md5 = hashlib.md5(b"y").hexdigest()
    # Stub the metadata lookup search calls used by --no-lookup
    respx.head(f"https://libgen.li/get.php?md5={md5}").respond(
        200, headers={"content-type": "application/octet-stream"}
    )
    out_dir = tmp_path / "books"
    result = runner.invoke(
        app,
        [
            "download",
            md5,
            "--no-lookup",
            "--dry-run",
            "-o",
            str(out_dir),
            "-m",
            "https://libgen.li",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stderr
    assert "dry-run" in result.stdout.lower() or md5 in result.stdout
    # No files should be written
    assert list(out_dir.glob("*")) == [] or all(
        not p.is_file() or (p.suffix == ".bin" and p.stat().st_size == 0) for p in out_dir.iterdir()
    )


def test_link_command_rejects_short_md5(runner: CliRunner, isolated_config: Path) -> None:
    result = runner.invoke(app, ["link", "abc"])
    assert result.exit_code != 0
    assert "32" in result.stderr or "32" in result.output


@respx.mock
def test_search_ext_filter_narrows_results(
    runner: CliRunner,
    nonfic_html: str,
    fiction_html: str,
    isolated_config: Path,
) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        topics = request.url.params.get("topics")
        if topics == "l":
            return httpx.Response(200, text=nonfic_html)
        if topics == "f":
            return httpx.Response(200, text=fiction_html)
        return httpx.Response(404)

    respx.get("https://libgen.li/index.php").mock(side_effect=handler)
    respx.route().pass_through()

    unfiltered = runner.invoke(
        app,
        ["search", "x", "--json", "-m", "https://libgen.li"],
        catch_exceptions=False,
    )
    assert unfiltered.exit_code == 0
    unfiltered_count = len([line for line in unfiltered.stdout.splitlines() if line.strip()])
    assert unfiltered_count > 0

    filtered = runner.invoke(
        app,
        ["search", "x", "--ext", "cbr", "--json", "-m", "https://libgen.li"],
        catch_exceptions=False,
    )
    assert filtered.exit_code == 0
    lines = [line for line in filtered.stdout.splitlines() if line.strip()]
    assert lines, "expected filtered output"
    for line in lines:
        rec = json.loads(line)
        assert rec["extension"] == "cbr"
    assert len(lines) <= unfiltered_count


def test_search_invalid_year_errors_out(runner: CliRunner, isolated_config: Path) -> None:
    result = runner.invoke(
        app,
        ["search", "x", "--year", "bogus", "-m", "https://libgen.li"],
    )
    assert result.exit_code != 0
