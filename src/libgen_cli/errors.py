"""Typed exceptions for libgen-cli."""

from __future__ import annotations


class LibgenError(Exception):
    """Base class for all libgen-cli errors."""


class ConfigError(LibgenError):
    """Raised when the config file cannot be parsed."""


class MirrorError(LibgenError):
    """Raised when an individual mirror operation fails."""


class NoMirrorsAvailableError(LibgenError):
    """Raised when every configured mirror has been exhausted."""


class ParseError(LibgenError):
    """Raised when an HTML page cannot be parsed into the expected structure."""


class SearchError(LibgenError):
    """Raised when a search request fails or yields no parseable results."""


class DownloadError(LibgenError):
    """Raised when a download cannot be completed."""


class MD5MismatchError(DownloadError):
    """Raised when a downloaded file's MD5 does not match the expected value."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"MD5 mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual
