"""Typer-based CLI surface for libgen-cli."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from libgen_cli import __version__
from libgen_cli.annas_archive import DEFAULT_AA_MIRRORS, search_aa
from libgen_cli.download import (
    ProgressEvent,
    download_book,
    download_many,
    resolve_download_url,
)
from libgen_cli.errors import LibgenError, NoMirrorsAvailableError
from libgen_cli.filters import BookFilter, FilterParseError
from libgen_cli.http import make_client
from libgen_cli.mirrors import (
    DEFAULT_MIRRORS,
    probe_and_rank,
    resolve_mirrors,
)
from libgen_cli.models import Book, DownloadResult, MirrorStatus, Topic
from libgen_cli.pick import pick_books
from libgen_cli.search import lookup_by_md5, search

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="libgen",
    help="Lightweight, fast CLI for searching and downloading from arbitrary Libgen mirrors.",
    add_completion=False,
)


# ---------- helpers ---------------------------------------------------------


def _topic_choices(topic: str) -> tuple[Topic, ...]:
    topic = topic.lower()
    if topic in {"both", "all"}:
        return (Topic.NONFIC, Topic.FICTION)
    if topic in {"nonfic", "nonfiction", "l"}:
        return (Topic.NONFIC,)
    if topic in {"fiction", "f"}:
        return (Topic.FICTION,)
    raise typer.BadParameter(f"unknown topic {topic!r}; expected nonfic|fiction|both")


def _build_filter(
    ext: list[str] | None,
    lang: list[str] | None,
    year: str | None,
) -> BookFilter:
    try:
        return BookFilter.from_options(ext=ext, lang=lang, year=year)
    except FilterParseError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _mirrors_or_die(
    cli_mirrors: Sequence[str] | None,
    *,
    allow_http: bool = False,
) -> list[str]:
    try:
        return resolve_mirrors(
            cli_mirrors=list(cli_mirrors) if cli_mirrors else None,
            allow_http=allow_http,
        )
    except NoMirrorsAvailableError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _emit_books_table(
    books: Sequence[Book],
    *,
    mirror: str | None,
    show_source: bool = False,
) -> None:
    if not books:
        console.print("[yellow]no results[/yellow]")
        return
    table = Table(
        title=f"results ({len(books)})" + (f" — via {mirror}" if mirror else ""),
        show_lines=False,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Authors", overflow="fold")
    table.add_column("Year", justify="right")
    table.add_column("Lang")
    table.add_column("Ext")
    table.add_column("Size", justify="right")
    if show_source:
        table.add_column("Src", no_wrap=True)
    table.add_column("MD5", style="cyan", no_wrap=True)
    for idx, book in enumerate(books, start=1):
        src_label = "aa" if book.source == "annas-archive" else "lbgn"
        row = [
            str(idx),
            book.title or "—",
            book.authors or "—",
            book.year or "—",
            book.language or "—",
            book.extension or "—",
            book.size or "—",
        ]
        if show_source:
            row.append(src_label)
        row.append(book.md5)
        table.add_row(*row)
    console.print(table)


def _emit_books_ndjson(books: Iterable[Book]) -> None:
    out = sys.stdout
    for book in books:
        out.write(json.dumps(book.to_dict(), ensure_ascii=False))
        out.write("\n")
    out.flush()


def _search_combined(
    client: httpx.Client,
    mirrors: list[str],
    aa_mirrors: list[str],
    query: str,
    *,
    topics: tuple[Topic, ...],
    results_per_page: int,
    page: int,
    ext: list[str] | None = None,
    lang: list[str] | None = None,
) -> list[Book]:
    """Run libgen and AA searches in parallel, merge and dedup by MD5.

    Libgen results take precedence on MD5 collision.  AA results for MD5s
    already in libgen are dropped; novel AA entries are appended at the end.
    """
    lg_books: list[Book] = []
    aa_books: list[Book] = []
    lg_exc: Exception | None = None

    def _do_libgen() -> list[Book]:
        return search(client, mirrors, query, topics=topics, results_per_page=results_per_page, page=page)

    def _do_aa() -> list[Book]:
        aa_ext = ext[0] if ext and len(ext) == 1 else None
        aa_lang = lang[0] if lang and len(lang) == 1 else None
        return search_aa(client, aa_mirrors, query, ext=aa_ext, lang=aa_lang)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_lg = pool.submit(_do_libgen) if mirrors else None
        fut_aa = pool.submit(_do_aa) if aa_mirrors else None

        if fut_lg is not None:
            try:
                lg_books = fut_lg.result()
            except Exception as exc:
                lg_exc = exc
        if fut_aa is not None:
            try:
                aa_books = fut_aa.result()
            except Exception:
                aa_books = []

    if lg_exc is not None and not aa_books:
        raise lg_exc  # type: ignore[misc]

    seen: set[str] = {b.md5 for b in lg_books}
    merged = list(lg_books)
    for book in aa_books:
        if book.md5 not in seen:
            seen.add(book.md5)
            merged.append(book)
    return merged


def _read_md5s_from_path(path: Path) -> list[str]:
    md5s: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        md5s.append(line.lower())
    return md5s


def _read_records_from_stdin() -> list[Book | str]:
    """Auto-detect NDJSON book records vs plain MD5 lines on stdin."""
    out: list[Book | str] = []
    for raw in sys.stdin:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "md5" in data:
                try:
                    out.append(Book.from_dict(data))
                    continue
                except (KeyError, ValueError, TypeError):
                    pass
            md5_value = data.get("md5") if isinstance(data, dict) else None
            if isinstance(md5_value, str):
                out.append(md5_value.lower())
            continue
        out.append(line.lower())
    return out


def _book_from_md5(
    client: httpx.Client,
    mirrors: list[str],
    md5: str,
    *,
    do_lookup: bool,
    topics: Iterable[Topic],
) -> Book:
    md5 = md5.lower().strip()
    if do_lookup:
        try:
            book = lookup_by_md5(client, mirrors, md5, topics=topics)
        except (httpx.HTTPError, LibgenError):
            book = None
        if book is not None:
            return book
    return Book(md5=md5, title=md5, extension="bin")


# ---------- progress reporter ----------------------------------------------


class RichProgressReporter:
    """Map :class:`ProgressEvent` callbacks onto a single rich Progress."""

    def __init__(self, progress: Progress) -> None:
        self.progress = progress
        self.tasks: dict[str, TaskID] = {}

    def __call__(self, event: ProgressEvent) -> None:
        md5_short = event.md5[:12]
        task_id = self.tasks.get(event.md5)
        if event.kind == "start":
            if task_id is None:
                task_id = self.progress.add_task(
                    description=f"{md5_short}", total=event.total_bytes
                )
                self.tasks[event.md5] = task_id
            self.progress.update(
                task_id,
                completed=event.bytes_so_far,
                total=event.total_bytes,
            )
        elif event.kind == "advance" and task_id is not None:
            self.progress.update(
                task_id,
                completed=event.bytes_so_far,
                total=event.total_bytes,
            )
        elif event.kind == "finish" and task_id is not None:
            self.progress.update(
                task_id,
                completed=self.progress.tasks[
                    [t.id for t in self.progress.tasks].index(task_id)
                ].total
                or event.bytes_so_far,
            )
            self.progress.console.print(f"[green]\u2713[/green] {md5_short} -> {event.message}")
        elif event.kind == "fail":
            self.progress.console.print(
                f"[red]x[/red] {md5_short} on {event.mirror}: {event.message}"
            )


# ---------- commands --------------------------------------------------------


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the libgen-cli version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    if version:
        console.print(f"libgen-cli {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command("search")
def cmd_search(
    query: Annotated[str, typer.Argument(help="Search query (title, author, etc.)")],
    topic: Annotated[
        str,
        typer.Option("-t", "--topic", help="nonfic | fiction | both"),
    ] = "both",
    n: Annotated[
        int,
        typer.Option("-n", "--results", help="Results per page (25, 50, or 100)."),
    ] = 25,
    page: Annotated[int, typer.Option("-p", "--page", help="Page number (1-based).")] = 1,
    ext: Annotated[
        list[str] | None,
        typer.Option(
            "-e",
            "--ext",
            help="Filter by file extension (repeat or comma-separate, e.g. epub,pdf).",
        ),
    ] = None,
    lang: Annotated[
        list[str] | None,
        typer.Option(
            "-l",
            "--lang",
            help="Filter by language (case-insensitive prefix match, e.g. en or english).",
        ),
    ] = None,
    year: Annotated[
        str | None,
        typer.Option(
            "-y",
            "--year",
            help="Filter by year, e.g. 2020 or 2010-2020.",
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Force NDJSON output (one record per line)."),
    ] = False,
    mirror: Annotated[
        list[str] | None,
        typer.Option("-m", "--mirror", help="Override mirror; may be repeated."),
    ] = None,
    allow_http: Annotated[
        bool,
        typer.Option("--allow-http", help="Allow plaintext (insecure) mirrors."),
    ] = False,
    no_aa: Annotated[
        bool,
        typer.Option("--no-aa", help="Disable Anna's Archive results."),
    ] = False,
) -> None:
    """Search libgen and Anna's Archive across the configured mirrors."""
    topics = _topic_choices(topic)
    book_filter = _build_filter(ext, lang, year)
    mirrors = _mirrors_or_die(mirror, allow_http=allow_http)
    aa_mirrors = [] if no_aa else list(DEFAULT_AA_MIRRORS)
    use_ndjson = json_out or not sys.stdout.isatty()

    try:
        with make_client() as client:
            books = _search_combined(
                client,
                mirrors,
                aa_mirrors,
                query,
                topics=topics,
                results_per_page=n,
                page=page,
                ext=ext,
                lang=lang,
            )
    except LibgenError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    books = book_filter.apply(books)
    has_aa = any(b.source == "annas-archive" for b in books)

    if use_ndjson:
        _emit_books_ndjson(books)
    else:
        _emit_books_table(
            books,
            mirror=mirrors[0] if mirrors else None,
            show_source=has_aa,
        )


@app.command("download")
def cmd_download(
    md5s: Annotated[
        list[str] | None,
        typer.Argument(
            help="One or more MD5 hashes; comma-separated values are split.",
        ),
    ] = None,
    out: Annotated[
        Path,
        typer.Option("-o", "--out", help="Output directory."),
    ] = Path("."),
    bulk: Annotated[
        Path | None,
        typer.Option("-b", "--bulk", help="Read MD5s from a file (one per line)."),
    ] = None,
    from_stdin: Annotated[
        bool,
        typer.Option("--from-stdin", help="Read NDJSON or MD5s from stdin."),
    ] = False,
    concurrency: Annotated[
        int,
        typer.Option("-j", "--concurrency", help="Parallel downloads."),
    ] = 4,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Resolve URLs but do not write files."),
    ] = False,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Re-download even if the file exists."),
    ] = False,
    no_lookup: Annotated[
        bool,
        typer.Option("--no-lookup", help="Skip metadata lookup; use MD5 as filename."),
    ] = False,
    mirror: Annotated[
        list[str] | None,
        typer.Option("-m", "--mirror", help="Override mirror; may be repeated."),
    ] = None,
    allow_http: Annotated[
        bool,
        typer.Option("--allow-http", help="Allow plaintext (insecure) mirrors."),
    ] = False,
    no_aa: Annotated[
        bool,
        typer.Option("--no-aa", help="Disable Anna's Archive fallback for downloads."),
    ] = False,
) -> None:
    """Download one or more books by MD5."""
    mirrors = _mirrors_or_die(mirror, allow_http=allow_http)
    aa_mirrors = [] if no_aa else list(DEFAULT_AA_MIRRORS)
    books = list(_collect_download_targets(md5s or [], bulk, from_stdin, mirrors, no_lookup))
    if not books:
        err_console.print("[yellow]no MD5s supplied[/yellow]")
        raise typer.Exit(code=2)

    out.mkdir(parents=True, exist_ok=True)
    results = _run_downloads(books, mirrors, out, concurrency, dry_run, overwrite, aa_mirrors=aa_mirrors)
    _summarise_results(results, dry_run=dry_run)


@app.command("link")
def cmd_link(
    md5: Annotated[str, typer.Argument(help="MD5 hash to resolve.")],
    mirror: Annotated[
        list[str] | None,
        typer.Option("-m", "--mirror", help="Override mirror; may be repeated."),
    ] = None,
    allow_http: Annotated[
        bool,
        typer.Option("--allow-http", help="Allow plaintext (insecure) mirrors."),
    ] = False,
) -> None:
    """Resolve a direct download URL for an MD5 without downloading."""
    md5 = md5.lower().strip()
    if len(md5) != 32:
        err_console.print(f"[red]error:[/red] expected 32-char MD5, got {len(md5)}")
        raise typer.Exit(code=2)
    mirrors = _mirrors_or_die(mirror, allow_http=allow_http)
    with make_client() as client:
        for m in mirrors:
            resolved = resolve_download_url(client, m, md5)
            if resolved is not None:
                console.print(resolved.url)
                return
    err_console.print(f"[red]error:[/red] no mirror could resolve {md5}")
    raise typer.Exit(code=1)


@app.command("pick")
def cmd_pick(
    query: Annotated[str, typer.Argument(help="Search query.")],
    topic: Annotated[str, typer.Option("-t", "--topic", help="nonfic | fiction | both")] = "both",
    n: Annotated[int, typer.Option("-n", "--results", help="Results per page (25/50/100).")] = 25,
    ext: Annotated[
        list[str] | None,
        typer.Option(
            "-e",
            "--ext",
            help="Filter by file extension (repeat or comma-separate, e.g. epub,pdf).",
        ),
    ] = None,
    lang: Annotated[
        list[str] | None,
        typer.Option(
            "-l",
            "--lang",
            help="Filter by language (case-insensitive prefix match, e.g. en or english).",
        ),
    ] = None,
    year: Annotated[
        str | None,
        typer.Option(
            "-y",
            "--year",
            help="Filter by year, e.g. 2020 or 2010-2020.",
        ),
    ] = None,
    out: Annotated[Path, typer.Option("-o", "--out", help="Output directory.")] = Path("."),
    concurrency: Annotated[
        int, typer.Option("-j", "--concurrency", help="Parallel downloads.")
    ] = 4,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Resolve URLs but do not write files."),
    ] = False,
    mirror: Annotated[
        list[str] | None,
        typer.Option("-m", "--mirror", help="Override mirror; may be repeated."),
    ] = None,
    allow_http: Annotated[
        bool,
        typer.Option("--allow-http", help="Allow plaintext (insecure) mirrors."),
    ] = False,
    no_aa: Annotated[
        bool,
        typer.Option("--no-aa", help="Disable Anna's Archive results."),
    ] = False,
) -> None:
    """Search libgen and Anna's Archive, multi-select interactively, then download."""
    topics = _topic_choices(topic)
    book_filter = _build_filter(ext, lang, year)
    mirrors = _mirrors_or_die(mirror, allow_http=allow_http)
    aa_mirrors = [] if no_aa else list(DEFAULT_AA_MIRRORS)
    with make_client() as client:
        try:
            books = _search_combined(
                client,
                mirrors,
                aa_mirrors,
                query,
                topics=topics,
                results_per_page=n,
                page=1,
                ext=ext,
                lang=lang,
            )
        except LibgenError as exc:
            err_console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(code=1) from exc

    books = book_filter.apply(books)
    if not books:
        console.print("[yellow]no results[/yellow]")
        raise typer.Exit(code=0)

    chosen = pick_books(books)
    if not chosen:
        console.print("[yellow]nothing selected[/yellow]")
        raise typer.Exit(code=0)

    out.mkdir(parents=True, exist_ok=True)
    results = _run_downloads(chosen, mirrors, out, concurrency, dry_run, overwrite=False, aa_mirrors=aa_mirrors)
    _summarise_results(results, dry_run=dry_run)


@app.command("mirrors")
def cmd_mirrors(
    probe: Annotated[
        bool,
        typer.Option("--probe", help="Probe mirrors live and persist the ranking."),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Per-mirror probe timeout (seconds)."),
    ] = 5.0,
    mirror: Annotated[
        list[str] | None,
        typer.Option("-m", "--mirror", help="Probe this URL instead of defaults."),
    ] = None,
    allow_http: Annotated[
        bool,
        typer.Option("--allow-http", help="Allow plaintext mirrors."),
    ] = False,
) -> None:
    """List configured mirrors. With ``--probe``, run a health check."""
    mirrors = _mirrors_or_die(mirror, allow_http=allow_http)
    if not probe:
        table = Table(title=f"mirrors ({len(mirrors)})")
        table.add_column("#", style="dim", justify="right")
        table.add_column("URL")
        table.add_column("Default", justify="center")
        defaults = set(DEFAULT_MIRRORS)
        for i, m in enumerate(mirrors, start=1):
            table.add_row(str(i), m, "yes" if m in defaults else "")
        console.print(table)
        return

    with make_client(timeout=timeout) as client:
        statuses, ranked = probe_and_rank(client, mirrors, timeout=timeout, persist=True)
    _emit_mirror_status_table(statuses)
    if ranked:
        console.print(f"[dim]ranking persisted; top mirror: {ranked[0]}[/dim]")


def _emit_mirror_status_table(statuses: list[MirrorStatus]) -> None:
    table = Table(title=f"mirror probe ({len(statuses)})")
    table.add_column("URL")
    table.add_column("Status")
    table.add_column("Latency", justify="right")
    table.add_column("HTTP", justify="right")
    table.add_column("Note")
    for s in statuses:
        table.add_row(
            s.url,
            "[green]OK[/green]" if s.ok else "[red]FAIL[/red]",
            f"{s.latency_ms:.0f} ms",
            str(s.status_code) if s.status_code else "—",
            s.error or "",
        )
    console.print(table)


# ---------- inner helpers ---------------------------------------------------


def _collect_download_targets(
    cli_md5s: list[str],
    bulk: Path | None,
    from_stdin: bool,
    mirrors: list[str],
    no_lookup: bool,
) -> Iterable[Book]:
    raw_md5s: list[str] = []
    explicit_books: list[Book] = []

    for entry in cli_md5s:
        for piece in entry.split(","):
            piece = piece.strip()
            if piece:
                raw_md5s.append(piece.lower())

    if bulk is not None:
        raw_md5s.extend(_read_md5s_from_path(bulk))

    if from_stdin:
        for item in _read_records_from_stdin():
            if isinstance(item, Book):
                explicit_books.append(item)
            else:
                raw_md5s.append(item)

    for md5 in raw_md5s:
        if len(md5) != 32:
            err_console.print(f"[yellow]warn:[/yellow] skipping non-MD5 token {md5!r}")
            continue

    seen: set[str] = set()
    targets: list[Book] = []
    for book in explicit_books:
        if book.md5 in seen:
            continue
        seen.add(book.md5)
        targets.append(book)

    needing_lookup = [md5 for md5 in raw_md5s if len(md5) == 32 and md5 not in seen]
    if not needing_lookup:
        return targets

    with make_client() as client:
        for md5 in needing_lookup:
            if md5 in seen:
                continue
            seen.add(md5)
            targets.append(
                _book_from_md5(
                    client,
                    mirrors,
                    md5,
                    do_lookup=not no_lookup,
                    topics=(Topic.NONFIC, Topic.FICTION),
                )
            )
    return targets


def _run_downloads(
    books: Sequence[Book],
    mirrors: Sequence[str],
    out: Path,
    concurrency: int,
    dry_run: bool,
    overwrite: bool,
    *,
    aa_mirrors: Sequence[str] = (),
) -> list[DownloadResult]:
    columns: tuple[ProgressColumn, ...]
    if dry_run:
        columns = (SpinnerColumn(), TextColumn("[bold]{task.description}"))
    else:
        columns = (
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        )

    with (
        make_client() as client,
        Progress(
            *columns,
            console=console,
            transient=False,
        ) as progress,
    ):
        reporter = RichProgressReporter(progress)
        if len(books) == 1:
            return [
                download_book(
                    client,
                    books[0],
                    mirrors,
                    aa_mirrors=aa_mirrors,
                    out_dir=out,
                    dry_run=dry_run,
                    overwrite=overwrite,
                    progress=reporter,
                )
            ]
        return download_many(
            client,
            books,
            mirrors,
            aa_mirrors=aa_mirrors,
            out_dir=out,
            concurrency=concurrency,
            dry_run=dry_run,
            overwrite=overwrite,
            progress=reporter,
        )


def _summarise_results(results: list[DownloadResult], *, dry_run: bool) -> None:
    if not results:
        return
    succeeded = sum(1 for r in results if r.success and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    failed = [r for r in results if not r.success]

    if dry_run:
        console.print(f"[dim]dry-run: resolved {succeeded}/{len(results)} URL(s)[/dim]")
        for r in results:
            if r.success and r.extra:
                console.print(f"  {r.md5} -> {r.extra.get('resolved_url')} (via {r.mirror_used})")
        if failed:
            for r in failed:
                err_console.print(f"  [red]fail[/red] {r.md5}: {r.error}")
        if failed:
            raise typer.Exit(code=1)
        return

    summary = (
        f"[green]{succeeded}[/green] downloaded"
        + (f", [yellow]{skipped}[/yellow] skipped" if skipped else "")
        + (f", [red]{len(failed)}[/red] failed" if failed else "")
    )
    console.print(summary)
    for r in failed:
        err_console.print(f"  [red]fail[/red] {r.md5}: {r.error}")
    if failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
