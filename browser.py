from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, TimeoutError
from playwright.async_api import async_playwright


LOGGER = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class BrowserResult:
    ok: bool
    http_status: int | None
    title: str
    description: str
    keywords: str
    error: str | None = None


class BrowserValidator:
    def __init__(
        self,
        timeout_ms: int = 10_000,
        user_agent: str = DEFAULT_USER_AGENT,
        retry_attempts: int = 2,
        retry_backoff_seconds: float = 0.7,
        domain_min_interval_seconds: float = 0.0,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.user_agent = user_agent
        self.retry_attempts = max(0, retry_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.domain_min_interval_seconds = max(0.0, domain_min_interval_seconds)
        self._domain_lock = asyncio.Lock()
        self._next_domain_at: dict[str, float] = {}
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "BrowserValidator":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def validate(self, url: str) -> BrowserResult:
        if self._context is None:
            raise RuntimeError("BrowserValidator must be used as an async context manager")

        total_attempts = self.retry_attempts + 1
        last_result: BrowserResult | None = None
        for attempt in range(total_attempts):
            result = await self._validate_once(url)
            if result.ok or not _is_retryable(result):
                return result

            last_result = result
            if attempt < total_attempts - 1:
                backoff = self.retry_backoff_seconds * (2**attempt)
                LOGGER.debug(
                    "Browser retry %s/%s for %s in %.2fs after %s",
                    attempt + 1,
                    self.retry_attempts,
                    url,
                    backoff,
                    result.error,
                )
                await asyncio.sleep(backoff)

        return last_result or BrowserResult(
            ok=False,
            http_status=None,
            title="",
            description="",
            keywords="",
            error="Browser validation failed without a result",
        )

    async def _validate_once(self, url: str) -> BrowserResult:
        if self._context is None:
            raise RuntimeError("BrowserValidator must be used as an async context manager")

        page = await self._context.new_page()
        try:
            await self._wait_for_domain_slot(url)
            try:
                response = await page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
            except TimeoutError as exc:
                # networkidle can hang on sites that stream or keep connections open.
                # Try a faster fallback to domcontentloaded with a shorter timeout.
                LOGGER.debug("networkidle timeout for %s, trying domcontentloaded fallback", url)
                try:
                    fallback_timeout = min(5000, max(2000, int(self.timeout_ms // 2)))
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=fallback_timeout)
                except TimeoutError as exc2:
                    # Both attempts timed out — report original timeout
                    return BrowserResult(
                        ok=False,
                        http_status=None,
                        title="",
                        description="",
                        keywords="",
                        error=f"Timeout after {self.timeout_ms}ms (networkidle then domcontentloaded): {exc2}",
                    )
            http_status = response.status if response is not None else None
            title = (await page.title()).strip()
            description = await _meta_content(page, "description")
            keywords = await _meta_content(page, "keywords")
            body_text = (await page.locator("body").inner_text(timeout=1500)).strip()

            if not title and not description and not body_text:
                return BrowserResult(
                    ok=False,
                    http_status=http_status,
                    title=title,
                    description=description,
                    keywords=keywords,
                    error="Empty page",
                )

            if http_status is not None and http_status >= 400:
                return BrowserResult(
                    ok=False,
                    http_status=http_status,
                    title=title,
                    description=description,
                    keywords=keywords,
                    error=f"HTTP {http_status}",
                )

            return BrowserResult(
                ok=True,
                http_status=http_status,
                title=title,
                description=description,
                keywords=keywords,
            )
        except TimeoutError as exc:
            return BrowserResult(
                ok=False,
                http_status=None,
                title="",
                description="",
                keywords="",
                error=f"Timeout after {self.timeout_ms}ms: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return BrowserResult(
                ok=False,
                http_status=None,
                title="",
                description="",
                keywords="",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            await page.close()

    async def _wait_for_domain_slot(self, url: str) -> None:
        if self.domain_min_interval_seconds <= 0:
            return

        domain = urlparse(url).netloc.lower()
        if not domain:
            return

        loop = asyncio.get_running_loop()
        while True:
            async with self._domain_lock:
                now = loop.time()
                wait_seconds = self._next_domain_at.get(domain, 0.0) - now
                if wait_seconds <= 0:
                    self._next_domain_at[domain] = now + self.domain_min_interval_seconds
                    return
            await asyncio.sleep(wait_seconds)


async def _meta_content(page: Page, name: str) -> str:
    value = await page.evaluate(
        """
        (name) => {
            const element = document.querySelector(`meta[name="${name}" i]`);
            return element ? element.getAttribute("content") : "";
        }
        """,
        name,
    )
    return str(value or "").strip()


def _is_retryable(result: BrowserResult) -> bool:
    if result.http_status in {408, 425, 429, 500, 502, 503, 504}:
        return True
    if result.http_status is None and result.error:
        return True
    return result.error == "Empty page"
