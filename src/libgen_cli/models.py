"""Core data models for libgen-cli."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Topic(StrEnum):
    """Libgen content section."""

    NONFIC = "nonfic"
    FICTION = "fiction"


@dataclass(frozen=True, slots=True)
class Book:
    """A single search-result row, normalised across nonfic and fiction sections.

    ``md5`` is the canonical identifier we use everywhere downstream — every
    libgen mirror serves the same file at ``/get.php?md5={md5}``.
    """

    md5: str
    title: str
    authors: str = ""
    year: str = ""
    publisher: str = ""
    language: str = ""
    pages: str = ""
    size: str = ""
    extension: str = ""
    topic: Topic = Topic.NONFIC
    libgen_id: str = ""
    source: str = "libgen"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["topic"] = self.topic.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Book:
        topic_raw = data.get("topic", Topic.NONFIC.value)
        topic = topic_raw if isinstance(topic_raw, Topic) else Topic(topic_raw)
        return cls(
            md5=str(data["md5"]).lower(),
            title=str(data.get("title", "")),
            authors=str(data.get("authors", "")),
            year=str(data.get("year", "")),
            publisher=str(data.get("publisher", "")),
            language=str(data.get("language", "")),
            pages=str(data.get("pages", "")),
            size=str(data.get("size", "")),
            extension=str(data.get("extension", "")),
            topic=topic,
            libgen_id=str(data.get("libgen_id", "")),
            source=str(data.get("source", "libgen")),
        )


@dataclass(frozen=True, slots=True)
class MirrorStatus:
    """Result of probing a single mirror."""

    url: str
    ok: bool
    latency_ms: float
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Outcome of a single download attempt for one ``Book``."""

    md5: str
    path: str | None
    success: bool
    error: str | None = None
    bytes_written: int = 0
    mirror_used: str | None = None
    skipped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
