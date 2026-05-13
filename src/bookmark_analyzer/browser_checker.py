from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable

from .models import HttpCheckResult
from .rate_limit import DomainRateLimiter

BLOCKER_KEYWORDS = [
    "cloudflare",
    "attention required",
    "checking your browser",
    "just a moment",
    "captcha",
    "bot protection",
    "botfighter",
    "access denied",
    "cf-ray",
]


def _has_bot_protection(page_title: str, page_content: str) -> bool:
    haystack = f"{page_title}\n{page_content}".lower()
    return any(keyword in haystack for keyword in BLOCKER_KEYWORDS)


async def _check_with_browser_single(
    context,
    url: str,
    timeout_ms: int,
    semaphore: asyncio.Semaphore,
    rate_limiter: DomainRateLimiter,
    retry_attempts: int,
    retry_backoff_seconds: float,
) -> HttpCheckResult | None:
    async with semaphore:
        started = time.perf_counter()
        page = await context.new_page()
        try:
            for attempt in range(retry_attempts + 1):
                await rate_limiter.wait_for_slot(url)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = response.status if response is not None else None
                page_title = await page.title()
                page_content = (await page.content())[:8000]
                blocked = _has_bot_protection(page_title=page_title, page_content=page_content)
                if blocked:
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    return HttpCheckResult(
                        url=url,
                        ok=False,
                        status_code=status,
                        final_url=page.url,
                        error="Bot protection page detected (cloudflare/captcha/botfighter).",
                        via="playwright",
                        response_time_ms=elapsed_ms,
                    )

                if status in {408, 425, 429, 500, 502, 503, 504} and attempt < retry_attempts:
                    backoff = retry_backoff_seconds * (2**attempt)
                    await asyncio.sleep(backoff + random.uniform(0, retry_backoff_seconds))
                    continue

                elapsed_ms = int((time.perf_counter() - started) * 1000)
                ok = status is None or status < 400
                return HttpCheckResult(
                    url=url,
                    ok=ok,
                    status_code=status,
                    final_url=page.url,
                    error=None,
                    via="playwright",
                    response_time_ms=elapsed_ms,
                )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return HttpCheckResult(
                url=url,
                ok=False,
                status_code=None,
                final_url=page.url,
                error="No browser check result generated.",
                via="playwright",
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return HttpCheckResult(
                url=url,
                ok=False,
                status_code=None,
                final_url=None,
                error=f"{type(exc).__name__}: {exc}",
                via="playwright",
                response_time_ms=elapsed_ms,
            )
        finally:
            await page.close()

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return HttpCheckResult(
        url=url,
        ok=False,
        status_code=None,
        final_url=None,
        error="Browser check exited unexpectedly.",
        via="playwright",
        response_time_ms=elapsed_ms,
    )


async def check_urls_with_playwright(
    urls: list[str],
    timeout_ms: int,
    concurrency: int,
    user_agent: str,
    retry_attempts: int = 1,
    retry_backoff_seconds: float = 0.7,
    domain_min_interval_seconds: float = 0.2,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, HttpCheckResult]:
    if not urls:
        return {}

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright package not installed. Install dependencies with: pip install -e ."
        ) from exc

    semaphore = asyncio.Semaphore(concurrency)
    rate_limiter = DomainRateLimiter(domain_min_interval_seconds)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=user_agent)
        try:
            tasks: list[asyncio.Task[HttpCheckResult]] = []
            for url in urls:
                task = asyncio.create_task(_check_with_browser_single(
                    context=context,
                    url=url,
                    timeout_ms=timeout_ms,
                    semaphore=semaphore,
                    rate_limiter=rate_limiter,
                    retry_attempts=retry_attempts,
                    retry_backoff_seconds=retry_backoff_seconds,
                ))
                tasks.append(task)

            results: list[HttpCheckResult] = []
            completed = 0
            total = len(tasks)
            for task in asyncio.as_completed(tasks):
                result = await task
                results.append(result)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)
        finally:
            await context.close()
            await browser.close()

    return {item.url: item for item in results}


async def check_url_jobs_with_playwright(
    jobs: list[tuple[str, str]],
    timeout_ms: int,
    concurrency: int,
    user_agent: str,
    retry_attempts: int = 1,
    retry_backoff_seconds: float = 0.7,
    domain_min_interval_seconds: float = 0.2,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, HttpCheckResult]:
    if not jobs:
        return {}

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright package not installed. Install dependencies with: pip install -e ."
        ) from exc

    semaphore = asyncio.Semaphore(concurrency)
    rate_limiter = DomainRateLimiter(domain_min_interval_seconds)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=user_agent)
        try:
            async def _run_with_index(idx: int, url: str) -> tuple[int, HttpCheckResult]:
                return idx, await _check_with_browser_single(
                    context=context,
                    url=url,
                    timeout_ms=timeout_ms,
                    semaphore=semaphore,
                    rate_limiter=rate_limiter,
                    retry_attempts=retry_attempts,
                    retry_backoff_seconds=retry_backoff_seconds,
                )

            tasks: list[asyncio.Task[tuple[int, HttpCheckResult]]] = []
            for idx, (_, url) in enumerate(jobs):
                tasks.append(asyncio.create_task(_run_with_index(idx, url)))

            results: list[HttpCheckResult | None] = [None] * len(tasks)
            completed = 0
            total = len(tasks)
            for task in asyncio.as_completed(tasks):
                idx, result = await task
                results[idx] = result
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)
        finally:
            await context.close()
            await browser.close()

    return {job_id: result for (job_id, _), result in zip(jobs, results) if result is not None}
