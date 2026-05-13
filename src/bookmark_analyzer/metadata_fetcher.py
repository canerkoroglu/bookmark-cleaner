from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

from .models import HttpCheckResult, PageMetadata
from .rate_limit import DomainRateLimiter


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)

    def title(self) -> str | None:
        value = "".join(self.title_parts).strip()
        return value or None


_META_DESCRIPTION_RE = re.compile(
    r'<meta[^>]+(?:name|property)\s*=\s*["\'](?:description|og:description)["\'][^>]*content\s*=\s*["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_META_KEYWORDS_RE = re.compile(
    r'<meta[^>]+name\s*=\s*["\']keywords["\'][^>]*content\s*=\s*["\']([^"\']*)["\']',
    re.IGNORECASE,
)


def _parse_metadata_from_html(text: str) -> PageMetadata:
    parser = _TitleParser()
    parser.feed(text)
    title = parser.title()

    description_match = _META_DESCRIPTION_RE.search(text)
    description = html.unescape(description_match.group(1)).strip() if description_match else None

    keywords_match = _META_KEYWORDS_RE.search(text)
    keywords: list[str] = []
    if keywords_match:
        raw = html.unescape(keywords_match.group(1)).strip()
        keywords = [item.strip() for item in raw.split(",") if item.strip()]

    return PageMetadata(
        title=title,
        description=description,
        keywords=keywords,
    )


async def fetch_metadata_for_jobs(
    jobs: list[tuple[str, str]],
    check_results_by_job_id: dict[str, HttpCheckResult],
    *,
    timeout_seconds: float,
    concurrency: int,
    user_agent: str,
    domain_min_interval_seconds: float,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, PageMetadata]:
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "aiohttp package not installed. Install dependencies with: pip install -e ."
        ) from exc

    semaphore = asyncio.Semaphore(max(1, min(concurrency, 20)))
    rate_limiter = DomainRateLimiter(domain_min_interval_seconds)
    connector_ctor = getattr(aiohttp, "TCPConnector")
    session_ctor = getattr(aiohttp, "ClientSession")
    timeout_ctor = getattr(aiohttp, "ClientTimeout")
    connector = connector_ctor(ssl=False, limit=max(1, min(concurrency, 20)))
    headers = {"User-Agent": user_agent}

    async def _fetch_one(job_id: str, url: str, session: Any) -> tuple[str, PageMetadata | None]:
        check = check_results_by_job_id.get(job_id)
        if check is None or not check.ok:
            return job_id, None

        async with semaphore:
            try:
                await rate_limiter.wait_for_slot(url)
                timeout = timeout_ctor(total=timeout_seconds)
                async with session.get(url, timeout=timeout, allow_redirects=True) as response:
                    if response.status >= 400:
                        return job_id, None
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "html" not in content_type and "xml" not in content_type:
                        return job_id, None
                    body = await response.text(errors="ignore")
                    snippet = body[:200_000]
                    return job_id, _parse_metadata_from_html(snippet)
            except Exception:  # noqa: BLE001
                return job_id, None

    async with session_ctor(headers=headers, connector=connector) as session:
        tasks: list[asyncio.Task[tuple[str, PageMetadata | None]]] = []
        for job_id, url in jobs:
            tasks.append(asyncio.create_task(_fetch_one(job_id, url, session)))

        pairs: list[tuple[str, PageMetadata | None]] = []
        completed = 0
        total = len(tasks)
        for task in asyncio.as_completed(tasks):
            pairs.append(await task)
            completed += 1
            if progress_callback:
                progress_callback(completed, total)

    return {job_id: meta for job_id, meta in pairs if meta is not None}
