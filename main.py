from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import json
from pathlib import Path

from db import JobQueue
from env_loader import load_env_file
from exporter import export_jobs_to_csv, CsvIncrementalExporter
from parser import extract_urls_from_firefox_json


LOGGER = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        LOGGER.warning("Ignoring invalid integer for %s=%r; using %s", name, value, default)
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        LOGGER.warning("Ignoring invalid float for %s=%r; using %s", name, value, default)
        return default


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(
        prog="bookmarks",
        description="Analyze Firefox bookmarks with a SQLite job queue.",
    )
    parser.add_argument("--input", type=Path, help="Firefox bookmarks JSON export path")
    parser.add_argument("--output", required=True, type=Path, help="CSV output path")
    parser.add_argument("--db", type=Path, default=Path("bookmarks_queue.sqlite3"), help="SQLite queue path")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N URLs/jobs")
    parser.add_argument("--resume", action="store_true", default=True, help="Resume from previous queue state")
    parser.add_argument("--reset", action="store_true", help="Clear queue and start fresh")
    parser.add_argument(
        "--skip-ai",
        action="store_true",
        help="Only run deterministic browser/status/metadata checks; leave AI comments blank.",
    )
    parser.add_argument(
        "--ai-only",
        action="store_true",
        help="Skip browser checks and only fill missing AI comments for existing done rows in the DB.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=_env_int("BA_CONCURRENCY", 5),
        help="Number of parallel workers. Defaults to BA_CONCURRENCY or 5.",
    )
    parser.add_argument(
        "--retry-limit",
        type=int,
        default=_env_int("BA_RETRY_ATTEMPTS", 3),
        help="Max persistent queue retries per URL. Defaults to BA_RETRY_ATTEMPTS or 3.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=_env_float("BA_RETRY_BACKOFF_SECONDS", 0.7),
        help="Base retry backoff in seconds. Defaults to BA_RETRY_BACKOFF_SECONDS or 0.7.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=_env_int("BA_BROWSER_TIMEOUT_MS", 10_000),
        help="Playwright navigation timeout in milliseconds. Defaults to BA_BROWSER_TIMEOUT_MS or 10000.",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=_env_float("BA_REQUEST_TIMEOUT_SECONDS", 30.0),
        help="Gemini request timeout. Defaults to BA_REQUEST_TIMEOUT_SECONDS or 30.",
    )
    parser.add_argument(
        "--domain-min-interval-seconds",
        type=float,
        default=_env_float("BA_DOMAIN_MIN_INTERVAL_SECONDS", 0.0),
        help="Minimum seconds between browser requests to the same domain.",
    )
    parser.add_argument(
        "--gemini-model",
        default=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        help="Gemini model name",
    )
    parser.add_argument(
        "--gemini-rpm",
        type=int,
        default=_env_int("BA_GEMINI_RPM", 6),
        help="Maximum Gemini requests per minute. Example: 6 means one request every 10 seconds.",
    )
    parser.add_argument(
        "--gemini-tpm",
        type=int,
        default=_env_int("BA_GEMINI_TPM", 0),
        help="Approximate Gemini tokens per minute limit. Defaults to BA_GEMINI_TPM or unlimited.",
    )
    parser.add_argument(
        "--gemini-rpd",
        type=int,
        default=_env_int("BA_GEMINI_RPD", 0),
        help="Gemini requests per day limit for this run. Defaults to BA_GEMINI_RPD or unlimited.",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.ai_only and args.input is None:
        raise SystemExit("--input is required unless you use --ai-only")
    if args.ai_only and args.reset:
        raise SystemExit("--ai-only cannot be used with --reset")
    if not args.ai_only:
        # args.input is required above; validate the path exists and is a file
        assert args.input is not None
        if not args.input.exists() or not args.input.is_file():
            parent = args.input.parent
            if parent.exists():
                files = sorted([p.name for p in parent.iterdir() if p.is_file()])
                raise SystemExit(
                    f"Input file {args.input} not found. Files in {parent}: {', '.join(files) or '<no files>'}"
                )
            else:
                raise SystemExit(
                    f"Input file {args.input} not found and directory {parent} does not exist."
                )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    queue = JobQueue(args.db)
    db_lock = asyncio.Lock()

    try:
        try:
            from ai import GeminiClient
            from browser import BrowserValidator
            from worker import run_ai_support, run_browser_workers
        except ImportError as exc:
            raise RuntimeError(
                "Missing runtime dependency. Run: pip install -r requirements.txt && playwright install"
            ) from exc

        queue.init_schema()
        if args.reset:
            LOGGER.info("Resetting queue at %s", args.db)
            queue.reset()

        recovered = queue.recover_processing_jobs()
        if recovered:
            LOGGER.warning("Recovered %s interrupted processing job(s)", recovered)

        run_limit = args.limit
        if not args.ai_only:
            assert args.input is not None
            try:
                urls = extract_urls_from_firefox_json(args.input)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SystemExit(f"Failed to parse JSON from {args.input}: {exc}") from exc
            except OSError as exc:
                raise SystemExit(f"Failed to read {args.input}: {exc}") from exc
            LOGGER.info("Found %s URL(s) in %s", len(urls), args.input)
            urls_to_insert = urls[: args.limit] if args.limit is not None else urls
            inserted = queue.insert_urls(urls_to_insert)
            LOGGER.info(
                "Parsed %s URL(s), considered %s for this run, inserted %s new job(s)",
                len(urls),
                len(urls_to_insert),
                inserted,
            )

            total_eligible = queue.count_eligible(args.retry_limit)
            LOGGER.info("Eligible browser-check jobs: %s", total_eligible)

        ai_client = GeminiClient(
            api_key=os.getenv("GEMINI_API_KEY"),
            model=args.gemini_model,
            timeout_seconds=args.request_timeout_seconds,
            requests_per_minute=args.gemini_rpm,
            tokens_per_minute=args.gemini_tpm,
            requests_per_day=args.gemini_rpd,
            retry_attempts=args.retry_limit,
            retry_backoff_seconds=args.retry_backoff_seconds,
        )
        LOGGER.info(
            "Runtime settings: concurrency=%s browser_timeout_ms=%s retry_limit=%s retry_backoff=%.2fs domain_interval=%.2fs",
            args.concurrency,
            args.timeout_ms,
            args.retry_limit,
            args.retry_backoff_seconds,
            args.domain_min_interval_seconds,
        )
        LOGGER.info(
            "Gemini model=%s timeout=%.2fs limits=%s rpm / %s tpm / %s rpd (%.2fs between requests)",
            args.gemini_model,
            args.request_timeout_seconds,
            args.gemini_rpm,
            args.gemini_tpm or "unlimited",
            args.gemini_rpd or "unlimited",
            ai_client.request_interval_seconds,
        )
        if not args.ai_only:
            # Create incremental CSV snapshot writer so rows are appended as jobs finish.
            csv_exporter = CsvIncrementalExporter(args.output)

            LOGGER.info("Phase 1/2: deterministic browser/status/metadata checks")
            async with BrowserValidator(
                timeout_ms=args.timeout_ms,
                retry_attempts=args.retry_limit,
                retry_backoff_seconds=args.retry_backoff_seconds,
                domain_min_interval_seconds=args.domain_min_interval_seconds,
            ) as browser:
                await run_browser_workers(
                    queue=queue,
                    browser=browser,
                    concurrency=args.concurrency,
                    retry_limit=args.retry_limit,
                    limit=run_limit,
                    db_lock=db_lock,
                    stop_event=stop_event,
                    csv_exporter=csv_exporter,
                )

            # Final deterministic snapshot (overwrite) to ensure canonical CSV before AI phase
            export_jobs_to_csv(queue, args.output)
            LOGGER.info("Exported deterministic CSV to %s", args.output)

        if not args.skip_ai and not stop_event.is_set():
            LOGGER.info("Phase 2/2: AI comments support")
            await run_ai_support(
                queue=queue,
                ai_client=ai_client,
                concurrency=args.concurrency,
                limit=run_limit if args.ai_only else None,
                db_lock=db_lock,
                stop_event=stop_event,
            )
        elif args.skip_ai:
            LOGGER.info("Skipping AI comments phase because --skip-ai was set")

        export_jobs_to_csv(queue, args.output)
        LOGGER.info("Exported final CSV to %s", args.output)
        LOGGER.info("Final queue counts: %s", queue.counts_by_status())
        return 130 if stop_event.is_set() else 0
    finally:
        queue.close()


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
