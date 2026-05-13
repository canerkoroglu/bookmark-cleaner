from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import Any

from .models import HttpCheckResult
from .rate_limit import DomainRateLimiter

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


async def _check_single_url(
    session: Any,
    url: str,
    timeout_seconds: float,
    semaphore: asyncio.Semaphore,
    rate_limiter: DomainRateLimiter,
    retry_attempts: int,
    retry_backoff_seconds: float,
) -> HttpCheckResult:
    import aiohttp

    async with semaphore:
        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(retry_attempts + 1):
            try:
                await rate_limiter.wait_for_slot(url)
                timeout_ctor = getattr(aiohttp, "ClientTimeout")
                timeout = timeout_ctor(total=timeout_seconds)
                async with session.head(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                ) as response:
                    # Some servers don't support HEAD correctly; fallback to GET.
                    if response.status in {405, 501}:
                        async with session.get(
                            url,
                            timeout=timeout,
                            allow_redirects=True,
                        ) as get_response:
                            if (
                                get_response.status in RETRYABLE_STATUS_CODES
                                and attempt < retry_attempts
                            ):
                                backoff = retry_backoff_seconds * (2**attempt)
                                await asyncio.sleep(backoff + random.uniform(0, retry_backoff_seconds))
                                continue
                            elapsed_ms = int((time.perf_counter() - started) * 1000)
                            ok = get_response.status < 400
                            return HttpCheckResult(
                                url=url,
                                ok=ok,
                                status_code=get_response.status,
                                final_url=str(get_response.url),
                                via="aiohttp-get",
                                response_time_ms=elapsed_ms,
                            )

                    if response.status in RETRYABLE_STATUS_CODES and attempt < retry_attempts:
                        backoff = retry_backoff_seconds * (2**attempt)
                        await asyncio.sleep(backoff + random.uniform(0, retry_backoff_seconds))
                        continue

                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    ok = response.status < 400
                    return HttpCheckResult(
                        url=url,
                        ok=ok,
                        status_code=response.status,
                        final_url=str(response.url),
                        via="aiohttp-head",
                        response_time_ms=elapsed_ms,
                    )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retry_attempts:
                    backoff = retry_backoff_seconds * (2**attempt)
                    await asyncio.sleep(backoff + random.uniform(0, retry_backoff_seconds))
                    continue
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                return HttpCheckResult(
                    url=url,
                    ok=False,
                    status_code=None,
                    final_url=None,
                    error=f"{type(exc).__name__}: {exc}",
                    response_time_ms=elapsed_ms,
                )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return HttpCheckResult(
            url=url,
            ok=False,
            status_code=None,
            final_url=None,
            error=f"{type(last_error).__name__}: {last_error}" if last_error else "Unknown error",
            response_time_ms=elapsed_ms,
        )


async def check_urls_async(
    urls: list[str],
    timeout_seconds: float,
    concurrency: int,
    user_agent: str,
    retry_attempts: int = 2,
    retry_backoff_seconds: float = 0.7,
    domain_min_interval_seconds: float = 0.2,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, HttpCheckResult]:
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "aiohttp package not installed. Install dependencies with: pip install -e ."
        ) from exc

    semaphore = asyncio.Semaphore(concurrency)
    rate_limiter = DomainRateLimiter(domain_min_interval_seconds)
    headers = {"User-Agent": user_agent}
    connector_ctor = getattr(aiohttp, "TCPConnector")
    session_ctor = getattr(aiohttp, "ClientSession")
    connector = connector_ctor(ssl=False, limit=concurrency)

    async with session_ctor(headers=headers, connector=connector) as session:
        tasks: list[asyncio.Task[HttpCheckResult]] = []
        for url in urls:
            task = asyncio.create_task(_check_single_url(
                session=session,
                url=url,
                timeout_seconds=timeout_seconds,
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

    return {result.url: result for result in results}


async def check_url_jobs_async(
    jobs: list[tuple[str, str]],
    timeout_seconds: float,
    concurrency: int,
    user_agent: str,
    retry_attempts: int = 2,
    retry_backoff_seconds: float = 0.7,
    domain_min_interval_seconds: float = 0.2,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, HttpCheckResult]:
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "aiohttp package not installed. Install dependencies with: pip install -e ."
        ) from exc

    if not jobs:
        return {}

    semaphore = asyncio.Semaphore(concurrency)
    rate_limiter = DomainRateLimiter(domain_min_interval_seconds)
    headers = {"User-Agent": user_agent}
    connector_ctor = getattr(aiohttp, "TCPConnector")
    session_ctor = getattr(aiohttp, "ClientSession")
    connector = connector_ctor(ssl=False, limit=concurrency)

    async with session_ctor(headers=headers, connector=connector) as session:
        async def _run_with_index(idx: int, url: str) -> tuple[int, HttpCheckResult]:
            return idx, await _check_single_url(
                session=session,
                url=url,
                timeout_seconds=timeout_seconds,
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

    return {job_id: result for (job_id, _), result in zip(jobs, results) if result is not None}
