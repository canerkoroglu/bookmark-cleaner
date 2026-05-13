from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class BookmarkInput:
    url: str
    source: str = "input"


@dataclass(slots=True)
class HttpCheckResult:
    url: str
    ok: bool
    status_code: int | None
    final_url: str | None
    error: str | None = None
    via: str = "aiohttp"
    response_time_ms: int | None = None


@dataclass(slots=True)
class AiClassification:
    category: str
    tags: list[str]
    summary: str
    confidence: float
    model: str


@dataclass(slots=True)
class PageMetadata:
    title: str | None
    description: str | None
    keywords: list[str]


@dataclass(slots=True)
class BookmarkReport:
    url: str
    domain: str
    deduplicated: bool
    duplicate_urls: list[str]
    check: HttpCheckResult
    classification: AiClassification | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["check"] = asdict(self.check)
        payload["classification"] = (
            asdict(self.classification) if self.classification else None
        )
        return payload
