"""Download orchestration: URL resolution, streamed transfers, MD5 verification.

Resolution order per book::

    for mirror in ranked_mirrors:
        1.  GET {mirror}/get.php?md5={md5}    (canonical primitive)
        2.  if step 1 returned HTML, scrape {mirror}/ads.php?md5={md5}
            for the keyed `get.php?md5=...&key=...` URL and retry.
    if no mirror produced bytes -> DownloadError

Bytes are streamed into ``<dst>.part`` while the MD5 is hashed on the fly.
Successful verification triggers an atomic ``os.replace`` to ``<dst>``.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import httpx

from libgen_cli.errors import DownloadError, MD5MismatchError, NoMirrorsAvailableError
from libgen_cli.models import Book, DownloadResult
from libgen_cli.naming import filename_for
from libgen_cli.parser import extract_keyed_download_url

CHUNK_SIZE = 64 * 1024
MIN_BINARY_BYTES = 4096
PROGRESS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class ResolvedURL:
    """A direct URL that should yield binary bytes."""

    url: str
    mirror: str
    via_key: bool


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Progress update emitted during a download.

    ``kind`` is one of: ``start``, ``advance``, ``finish``, ``fail``, ``retry``.
    """

    kind: str
    md5: str
    total_bytes: int | None = None
    bytes_so_far: int = 0
    message: str | None = None
    mirror: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


def _is_html(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.lower().split(";", 1)[0].strip() in {
        "text/html",
        "application/xhtml+xml",
    }


def _candidate_urls(mirror: str, md5: str) -> tuple[str, str]:
    base = mirror.rstrip("/")
    return f"{base}/get.php?md5={md5}", f"{base}/ads.php?md5={md5}"


def _fetch_keyed_url(client: httpx.Client, mirror: str, md5: str) -> str | None:
    _, ads_url = _candidate_urls(mirror, md5)
    try:
        resp = client.get(ads_url)
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None
    return extract_keyed_download_url(resp.text, mirror)


def resolve_download_url(
    client: httpx.Client,
    mirror: str,
    md5: str,
) -> ResolvedURL | None:
    """Probe ``mirror`` for a direct (binary) URL for the given MD5.

    Returns ``None`` if the mirror cannot serve the file (HTTP error, HTML
    response, or no extractable keyed link).
    """
    direct_url, _ = _candidate_urls(mirror, md5)
    try:
        resp = client.head(direct_url, follow_redirects=True)
    except httpx.HTTPError:
        resp = None

    if (
        resp is not None
        and resp.status_code < 400
        and not _is_html(resp.headers.get("content-type"))
    ):
        return ResolvedURL(url=str(resp.url), mirror=mirror, via_key=False)

    keyed = _fetch_keyed_url(client, mirror, md5)
    if not keyed:
        return None
    try:
        keyed_resp = client.head(keyed, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if keyed_resp.status_code >= 400 or _is_html(keyed_resp.headers.get("content-type")):
        return None
    return ResolvedURL(url=str(keyed_resp.url), mirror=mirror, via_key=True)


def _stream_to_part(
    client: httpx.Client,
    url: str,
    part_path: Path,
    expected_md5: str,
    *,
    md5: str,
    progress: ProgressCallback | None,
    mirror: str,
) -> int:
    """Stream ``url`` into ``part_path`` (resuming if it already exists).

    Returns the total number of bytes written. Raises :class:`MD5MismatchError`
    on hash mismatch and :class:`DownloadError` on other failures.
    """
    headers: dict[str, str] = {}
    initial_size = 0
    if part_path.exists():
        initial_size = part_path.stat().st_size
        if initial_size > 0:
            headers["Range"] = f"bytes={initial_size}-"

    hasher = hashlib.md5(usedforsecurity=False)
    if initial_size > 0:
        with part_path.open("rb") as existing:
            for chunk in iter(lambda: existing.read(CHUNK_SIZE), b""):
                hasher.update(chunk)

    bytes_written = initial_size

    with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
        if resp.status_code == 416:
            initial_size = 0
            bytes_written = 0
            hasher = hashlib.md5(usedforsecurity=False)
            with client.stream("GET", url, follow_redirects=True) as fresh:
                _ensure_binary(fresh)
                total = _content_length(fresh)
                if progress is not None:
                    progress(ProgressEvent("start", md5, total, 0, mirror=mirror))
                with part_path.open("wb") as fh:
                    for chunk in fresh.iter_bytes(CHUNK_SIZE):
                        fh.write(chunk)
                        hasher.update(chunk)
                        bytes_written += len(chunk)
                        if progress is not None:
                            progress(
                                ProgressEvent(
                                    "advance",
                                    md5,
                                    total,
                                    bytes_written,
                                    mirror=mirror,
                                )
                            )
        else:
            _ensure_binary(resp)
            partial = resp.status_code == 206
            if not partial and initial_size > 0:
                initial_size = 0
                bytes_written = 0
                hasher = hashlib.md5(usedforsecurity=False)
                part_path.unlink(missing_ok=True)
            total = _content_length(resp, initial=initial_size)
            mode = "ab" if partial else "wb"
            if progress is not None:
                progress(
                    ProgressEvent(
                        "start",
                        md5,
                        total,
                        bytes_written,
                        mirror=mirror,
                    )
                )
            with part_path.open(mode) as fh:
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    fh.write(chunk)
                    hasher.update(chunk)
                    bytes_written += len(chunk)
                    if progress is not None:
                        progress(
                            ProgressEvent(
                                "advance",
                                md5,
                                total,
                                bytes_written,
                                mirror=mirror,
                            )
                        )

    actual_md5 = hasher.hexdigest()
    if actual_md5.lower() != expected_md5.lower():
        raise MD5MismatchError(expected=expected_md5, actual=actual_md5)
    return bytes_written


def _ensure_binary(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise DownloadError(f"HTTP {resp.status_code} from {resp.url}")
    if _is_html(resp.headers.get("content-type")):
        raise DownloadError(f"got HTML, expected binary: {resp.url}")


def _content_length(resp: httpx.Response, *, initial: int = 0) -> int | None:
    raw = resp.headers.get("content-length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError:
        return None
    return length + initial


def download_book(
    client: httpx.Client,
    book: Book,
    mirrors: Sequence[str],
    *,
    out_dir: Path,
    dry_run: bool = False,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
) -> DownloadResult:
    """Download a single :class:`Book` to ``out_dir``.

    Returns a :class:`DownloadResult` summarising the outcome. Never raises;
    failures surface via ``result.success == False`` so bulk callers can
    aggregate.
    """
    md5 = book.md5.lower()
    if not mirrors:
        return DownloadResult(md5=md5, path=None, success=False, error="no mirrors available")

    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / filename_for(book)
    part_path = final_path.with_suffix(final_path.suffix + ".part")

    if final_path.exists() and not overwrite:
        return DownloadResult(
            md5=md5,
            path=str(final_path),
            success=True,
            skipped=True,
            mirror_used=None,
            bytes_written=final_path.stat().st_size,
        )

    last_error: str | None = None
    for mirror in mirrors:
        if progress is not None:
            progress(ProgressEvent("retry", md5, mirror=mirror, message="resolving"))
        resolved = resolve_download_url(client, mirror, md5)
        if resolved is None:
            last_error = f"unable to resolve direct URL on {mirror}"
            continue

        if dry_run:
            if progress is not None:
                progress(ProgressEvent("finish", md5, mirror=mirror, message=resolved.url))
            return DownloadResult(
                md5=md5,
                path=str(final_path),
                success=True,
                mirror_used=mirror,
                extra={"resolved_url": resolved.url, "via_key": resolved.via_key},
            )

        try:
            bytes_written = _stream_to_part(
                client,
                resolved.url,
                part_path,
                expected_md5=md5,
                md5=md5,
                progress=progress,
                mirror=mirror,
            )
        except MD5MismatchError as exc:
            last_error = str(exc)
            part_path.unlink(missing_ok=True)
            if progress is not None:
                progress(ProgressEvent("fail", md5, mirror=mirror, message=str(exc)))
            continue
        except (DownloadError, httpx.HTTPError, OSError) as exc:
            last_error = str(exc) or exc.__class__.__name__
            if progress is not None:
                progress(ProgressEvent("fail", md5, mirror=mirror, message=last_error))
            continue

        os.replace(part_path, final_path)
        if progress is not None:
            progress(
                ProgressEvent(
                    "finish",
                    md5,
                    bytes_so_far=bytes_written,
                    mirror=mirror,
                    message=str(final_path),
                )
            )
        return DownloadResult(
            md5=md5,
            path=str(final_path),
            success=True,
            mirror_used=mirror,
            bytes_written=bytes_written,
            extra={"via_key": resolved.via_key},
        )

    return DownloadResult(
        md5=md5,
        path=None,
        success=False,
        error=last_error or "all mirrors exhausted",
    )


def download_many(
    client: httpx.Client,
    books: Iterable[Book],
    mirrors: Sequence[str],
    *,
    out_dir: Path,
    concurrency: int = 4,
    dry_run: bool = False,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
) -> list[DownloadResult]:
    """Download a batch concurrently using a thread pool.

    Order of returned results matches the input order; mirror-failover and
    MD5 verification are handled per-book by :func:`download_book`.
    """
    book_list = list(books)
    if not book_list:
        return []
    if not mirrors:
        raise NoMirrorsAvailableError("no mirrors available")

    workers = max(1, min(concurrency, len(book_list)))
    ordered: dict[int, DownloadResult] = {}
    if workers == 1:
        return [
            download_book(
                client,
                b,
                mirrors,
                out_dir=out_dir,
                dry_run=dry_run,
                overwrite=overwrite,
                progress=progress,
            )
            for b in book_list
        ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                download_book,
                client,
                book,
                mirrors,
                out_dir=out_dir,
                dry_run=dry_run,
                overwrite=overwrite,
                progress=progress,
            ): idx
            for idx, book in enumerate(book_list)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                ordered[idx] = fut.result()
            except Exception as exc:
                book = book_list[idx]
                ordered[idx] = DownloadResult(
                    md5=book.md5,
                    path=None,
                    success=False,
                    error=f"{exc.__class__.__name__}: {exc}",
                )
                print(
                    f"[libgen] internal error for {book.md5}: {exc}",
                    file=sys.stderr,
                )

    return [ordered[i] for i in range(len(book_list))]
