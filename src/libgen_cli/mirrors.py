"""Libgen mirror discovery, health probing, and ranking.

The bundled defaults come from the Shadow Libraries reference page
(https://shadowlibraries.github.io/DirectDownloads/libgen/) and all share the
same HTML structure plus the canonical ``/get.php?md5=...`` download endpoint.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import httpx

from libgen_cli.config import (
    env_mirror_overrides,
    load_mirror_overrides,
    save_mirror_ranking,
)
from libgen_cli.errors import NoMirrorsAvailableError
from libgen_cli.models import MirrorStatus

DEFAULT_MIRRORS: tuple[str, ...] = (
    "https://libgen.li",
    "https://libgen.la",
    "https://libgen.gl",
    "https://libgen.vg",
    "https://libgen.bz",
    "https://libgen.rs",
)

PROBE_PATH = "/"
PROBE_TIMEOUT_SECONDS = 5.0


_HOST_RE = re.compile(r"^[A-Za-z0-9.\-_:]+$")


def normalise_mirror(url: str) -> str:
    """Coerce a user-supplied mirror string to ``scheme://host`` (no trailing slash).

    Bare hostnames are upgraded to ``https://``. Returns ``""`` for input that
    cannot be parsed as ``scheme://host`` (whitespace in the host, missing
    netloc, etc.).
    """
    url = url.strip().rstrip("/")
    if not url:
        return ""
    if any(ch.isspace() for ch in url):
        return ""
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    if not _HOST_RE.match(parsed.netloc):
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def resolve_mirrors(
    *,
    cli_mirrors: list[str] | None = None,
    allow_http: bool = False,
) -> list[str]:
    """Return the ordered list of mirrors to use for this invocation.

    Priority (highest first):

    1. Explicit ``--mirror`` flags on the CLI.
    2. ``LIBGEN_MIRROR`` environment variable.
    3. Persisted ranking in the config file.
    4. Bundled defaults.

    Duplicate URLs are de-duplicated while preserving first-seen order. Insecure
    ``http://`` mirrors are filtered unless ``allow_http`` is True.
    """
    sources: list[list[str]] = []
    if cli_mirrors:
        sources.append(cli_mirrors)
    sources.append(env_mirror_overrides())
    sources.append(load_mirror_overrides())
    sources.append(list(DEFAULT_MIRRORS))

    seen: set[str] = set()
    out: list[str] = []
    for src in sources:
        for raw in src:
            mirror = normalise_mirror(raw)
            if not mirror:
                continue
            if not allow_http and mirror.startswith("http://"):
                continue
            if mirror in seen:
                continue
            seen.add(mirror)
            out.append(mirror)

    if not out:
        raise NoMirrorsAvailableError("no mirrors configured (HTTPS-only filter may be too strict)")
    return out


def probe_one(
    client: httpx.Client, url: str, timeout: float = PROBE_TIMEOUT_SECONDS
) -> MirrorStatus:
    """Issue a single probe ``GET`` and return a :class:`MirrorStatus`."""
    started = time.perf_counter()
    try:
        resp = client.get(url + PROBE_PATH, timeout=timeout)
    except httpx.TimeoutException as exc:
        return MirrorStatus(
            url=url,
            ok=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"timeout: {exc}",
        )
    except httpx.HTTPError as exc:
        return MirrorStatus(
            url=url,
            ok=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=str(exc) or exc.__class__.__name__,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    ok = resp.status_code < 500
    return MirrorStatus(
        url=url,
        ok=ok,
        latency_ms=elapsed_ms,
        status_code=resp.status_code,
        error=None if ok else f"HTTP {resp.status_code}",
    )


def probe_all(
    client: httpx.Client,
    mirrors: list[str],
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    max_workers: int = 8,
) -> list[MirrorStatus]:
    """Probe every mirror concurrently. Result order matches the input order."""
    if not mirrors:
        return []
    workers = min(max_workers, len(mirrors))
    results: dict[str, MirrorStatus] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_one, client, url, timeout): url for url in mirrors}
        for fut in as_completed(futures):
            url = futures[fut]
            results[url] = fut.result()
    return [results[url] for url in mirrors]


def rank_by_status(statuses: list[MirrorStatus]) -> list[str]:
    """Return mirror URLs sorted by ``(ok desc, latency_ms asc)``."""
    return [s.url for s in sorted(statuses, key=lambda s: (not s.ok, s.latency_ms))]


def probe_and_rank(
    client: httpx.Client,
    mirrors: list[str] | None = None,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    persist: bool = False,
) -> tuple[list[MirrorStatus], list[str]]:
    """Probe every mirror, return ``(statuses, ranked_urls)``.

    When ``persist`` is True, the ranking (healthy mirrors first) is written
    back to the user config so the next invocation gets a head start.
    """
    pool = mirrors or resolve_mirrors()
    statuses = probe_all(client, pool, timeout=timeout)
    ranked = rank_by_status(statuses)
    if persist and ranked:
        healthy = [s.url for s in statuses if s.ok]
        if healthy:
            save_mirror_ranking(rank_by_status([s for s in statuses if s.ok]))
    return statuses, ranked
