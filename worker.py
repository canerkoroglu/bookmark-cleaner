from __future__ import annotations

import asyncio
import logging

from ai import GeminiClient
from browser import BrowserValidator
from db import JobQueue


LOGGER = logging.getLogger(__name__)


async def run_browser_workers(
    *,
    queue: JobQueue,
    browser: BrowserValidator,
    concurrency: int,
    retry_limit: int,
    limit: int | None,
    db_lock: asyncio.Lock,
    stop_event: asyncio.Event,
    csv_exporter = None,
    total_urls: int | None = None,
) -> None:
    processed_this_run = 0
    processed_lock = asyncio.Lock()

    async def should_continue() -> bool:
        nonlocal processed_this_run
        if stop_event.is_set():
            return False
        if limit is None:
            return True
        async with processed_lock:
            if processed_this_run >= limit:
                return False
            processed_this_run += 1
            return True

    async def worker(worker_id: int) -> None:
        while await should_continue():
            async with db_lock:
                job = queue.claim_next(retry_limit)
            if job is None:
                return

            progress_str = f"{job.id}/{total_urls}" if total_urls else f"{job.id}"
            LOGGER.info("worker=%s processing %s job_id=%s url=%s", worker_id, progress_str, job.id, job.url)
            result = await browser.validate(job.url)

            if result.ok:
                async with db_lock:
                    queue.mark_done(
                        job.id,
                        http_status=result.http_status,
                        title=result.title,
                        description=result.description,
                        keywords=result.keywords,
                    )
                LOGGER.info("worker=%s done job_id=%s", worker_id, job.id)
                # Append incremental CSV snapshot (async-safe)
                if csv_exporter is not None:
                    await csv_exporter.append_row(
                        site_url=job.url,
                        status="done",
                        title=result.title,
                        description=result.description,
                        keywords=result.keywords,
                        comments="",
                    )
                continue

            async with db_lock:
                queue.mark_failed(
                    job.id,
                    error=result.error or "Unknown browser validation failure",
                    http_status=result.http_status,
                    title=result.title,
                    description=result.description,
                    keywords=result.keywords,
                    comments="Unknown purpose",
                )
            LOGGER.warning("worker=%s failed job_id=%s error=%s", worker_id, job.id, result.error)
            if csv_exporter is not None:
                await csv_exporter.append_row(
                    site_url=job.url,
                    status="failed",
                    title=result.title,
                    description=result.description,
                    keywords=result.keywords,
                    comments=result.error or "",
                )

    tasks = [asyncio.create_task(worker(i + 1)) for i in range(max(1, concurrency))]
    await asyncio.gather(*tasks)


async def run_failed_url_recheck(
    *,
    queue: JobQueue,
    browser: BrowserValidator,
    concurrency: int,
    db_lock: asyncio.Lock,
    stop_event: asyncio.Event,
    csv_exporter = None,
) -> tuple[int, int]:
    """Recheck failed URLs and return (total_rechecked, newly_fixed)."""
    async with db_lock:
        failed_rows = queue.fetch_failed_rows()
    
    if not failed_rows:
        LOGGER.info("Recheck: no failed URLs to retry")
        return 0, 0

    total_failed = len(failed_rows)
    LOGGER.info("Recheck: rechecking %s failed URL(s)", total_failed)
    
    # Put failed jobs in a queue
    work_queue: asyncio.Queue = asyncio.Queue()
    for row in failed_rows:
        work_queue.put_nowait(row)
    
    newly_fixed = 0
    newly_fixed_lock = asyncio.Lock()
    
    async def worker(worker_id: int) -> None:
        nonlocal newly_fixed
        while not stop_event.is_set():
            try:
                row = work_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            
            job_id = int(row["id"])
            url = str(row["url"])
            progress_str = f"{job_id}/{total_failed}"
            LOGGER.info("recheck_worker=%s processing %s url=%s", worker_id, progress_str, url)
            
            result = await browser.validate(url)
            
            if result.ok:
                async with db_lock:
                    queue.mark_done(
                        job_id,
                        http_status=result.http_status,
                        title=result.title,
                        description=result.description,
                        keywords=result.keywords,
                    )
                async with newly_fixed_lock:
                    newly_fixed += 1
                LOGGER.info("recheck_worker=%s FIXED job_id=%s", worker_id, job_id)
                if csv_exporter is not None:
                    await csv_exporter.append_row(
                        site_url=url,
                        status="done",
                        title=result.title,
                        description=result.description,
                        keywords=result.keywords,
                        comments="",
                    )
            else:
                LOGGER.debug("recheck_worker=%s still failed job_id=%s error=%s", worker_id, job_id, result.error)
            
            work_queue.task_done()
    
    tasks = [asyncio.create_task(worker(i + 1)) for i in range(max(1, concurrency))]
    await asyncio.gather(*tasks)
    
    return total_failed, newly_fixed


async def run_ai_support(
    *,
    queue: JobQueue,
    ai_client: GeminiClient,
    concurrency: int,
    limit: int | None,
    db_lock: asyncio.Lock,
    stop_event: asyncio.Event,
) -> None:
    async with db_lock:
        rows = queue.fetch_ai_candidates(limit)

    if not rows:
        LOGGER.info("AI support: no rows need comments")
        return

    LOGGER.info("AI support: filling comments for %s row(s)", len(rows))
    work_queue: asyncio.Queue = asyncio.Queue()
    for row in rows:
        work_queue.put_nowait(row)

    async def worker(worker_id: int) -> None:
        while not stop_event.is_set():
            try:
                row = work_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            job_id = int(row["id"])
            url = str(row["url"])
            LOGGER.info("ai_worker=%s summarizing job_id=%s url=%s", worker_id, job_id, url)
            comments = await ai_client.summarize(
                url=url,
                title=str(row["title"] or ""),
                description=str(row["description"] or ""),
            )
            if comments:
                async with db_lock:
                    queue.update_comments(job_id, comments)
                LOGGER.info("ai_worker=%s updated comments job_id=%s", worker_id, job_id)
            else:
                LOGGER.warning("ai_worker=%s left comments blank job_id=%s", worker_id, job_id)

            work_queue.task_done()

    tasks = [asyncio.create_task(worker(i + 1)) for i in range(max(1, concurrency))]
    await asyncio.gather(*tasks)
