"""Shared HTTP client factory.

A single ``httpx.Client`` is reused across the application: connection pooling,
HTTP/1.1 keep-alive, and bounded timeouts give us most of the throughput we
need without an async runtime.
"""

from __future__ import annotations

from typing import Any

import httpx

from libgen_cli import __version__

DEFAULT_USER_AGENT = f"libgen-cli/{__version__} (+https://github.com/ianja/libgen-cli)"

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
DEFAULT_LIMITS = httpx.Limits(max_keepalive_connections=8, max_connections=32)


def make_client(
    *,
    timeout: httpx.Timeout | float | None = None,
    follow_redirects: bool = True,
    user_agent: str = DEFAULT_USER_AGENT,
    extra_headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """Build a configured :class:`httpx.Client`.

    Callers are responsible for closing the client (or using it as a context
    manager). The same client is safe to reuse across threads.
    """
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    if extra_headers:
        headers.update(extra_headers)

    effective_timeout: httpx.Timeout | float = DEFAULT_TIMEOUT if timeout is None else timeout

    return httpx.Client(
        timeout=effective_timeout,
        follow_redirects=follow_redirects,
        headers=headers,
        limits=DEFAULT_LIMITS,
        **kwargs,
    )
