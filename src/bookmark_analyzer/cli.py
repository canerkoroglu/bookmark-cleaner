from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import io
import sys
import time
from pathlib import Path
from typing import cast

from .config import Settings
from .env_loader import load_env_file
from .exporters import export_job_summary_csv, export_report_summary_csv, export_reports
from .io_utils import (
    STATUS_FOUND,
    STATUS_NOT_FOUND,
    annotate_firefox_export_status,
    extract_firefox_bookmark_jobs,
    is_firefox_bookmark_export,
    load_json_data,
    load_urls,
    merge_ai_into_firefox_export_by_url,
)
from .metadata_fetcher import fetch_metadata_for_jobs
from .models import PageMetadata
from .pipeline import BookmarkAnalyzer
from .storage import fetch_recent_results, init_history_db, insert_run_log, upsert_results


class _TeeStream(io.TextIOBase):
    def __init__(self, *streams: io.TextIOBase) -> None:
        self._streams = streams

    def write(self, s: str) -> int:
        for stream in self._streams:
            try:
                stream.write(s)
            except ValueError:
                # Stream may already be closed during interpreter shutdown.
                continue
        return len(s)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except ValueError:
                continue

    def isatty(self) -> bool:
        return any(getattr(stream, "isatty", lambda: False)() for stream in self._streams)


def _setup_run_log(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    log_path = output_dir / f"{timestamp}.log"
    log_file = log_path.open("a", encoding="utf-8")
    base_stdout = cast(io.TextIOBase, sys.__stdout__ if sys.__stdout__ is not None else sys.stdout)
    sys.stdout = _TeeStream(base_stdout, log_file)
    return log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bookmark-analyzer",
        description="Analyze bookmarks with async URL checks, browser fallback, and AI classification.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to input file (.txt line-delimited URLs or .json array).",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory where JSON output files will be written.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Concurrent checks/classifications.",
    )
    parser.add_argument(
        "--disable-playwright-fallback",
        action="store_true",
        help="Disable browser-based fallback checks.",
    )
    parser.add_argument(
        "--disable-ai-classification",
        action="store_true",
        help="Disable AI URL classification.",
    )
    parser.add_argument(
        "--resume-hours",
        type=float,
        default=None,
        help="Reuse recent URL statuses from SQLite history if checked within this many hours.",
    )
    parser.add_argument(
        "--summary-csv",
        default=None,
        help="Optional path for CSV summary output.",
    )
    parser.add_argument(
        "--max-urls",
        type=int,
        default=None,
        help="Optional cap to process only the first N URLs/jobs (useful for quick tests).",
    )
    parser.add_argument(
        "--live-logs",
        action="store_true",
        help="Print live progress logs for AI batch classification.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run checks/classification without writing files, CSVs, or DB updates.",
    )
    parser.add_argument(
        "--ai-confidence-threshold",
        type=float,
        default=None,
        help="Drop AI classifications below this confidence (0.0-1.0).",
    )
    parser.add_argument(
        "--category-rules-file",
        default=None,
        help="Optional JSON rules file for category overrides by domain/URL substring.",
    )
    parser.add_argument(
        "--ai-merge-exported-json",
        default=None,
        help="Optional Firefox JSON path to enrich by matching uri with AI results from --input URL list.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    started_ts = time.time()
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    json_data = load_json_data(input_path) if input_path.suffix.lower() == ".json" else None
    firefox_mode = json_data is not None and is_firefox_bookmark_export(json_data)

    settings = Settings.from_env(concurrency_override=args.concurrency)
    if args.live_logs:
        settings.ai_live_logs = True
    if args.ai_confidence_threshold is not None:
        settings.ai_confidence_threshold = args.ai_confidence_threshold
    if args.category_rules_file:
        settings.category_rules_file = args.category_rules_file

    analyzer = BookmarkAnalyzer(
        settings=settings,
        enable_playwright_fallback=not args.disable_playwright_fallback,
        enable_ai_classification=(not args.disable_ai_classification and not firefox_mode),
        deduplicate_by_domain=not firefox_mode,
    )

    progress_state: dict[str, int] = {}
    active_progress_phase: str | None = None

    def _flush_progress_line() -> None:
        nonlocal active_progress_phase
        if active_progress_phase is not None:
            print()
            active_progress_phase = None

    def _progress(phase: str, done: int, total: int) -> None:
        nonlocal active_progress_phase
        if total <= 0:
            return
        previous = progress_state.get(phase, 0)
        if done <= previous and done != total:
            return
        if active_progress_phase is not None and active_progress_phase != phase:
            print()
        active_progress_phase = phase
        print(f"\r[progress] {phase}: {done}/{total} completed", end="", flush=True)
        progress_state[phase] = done
        if done >= total:
            print()
            active_progress_phase = None

    if args.disable_ai_classification:
        _flush_progress_line()
        print("[AI] disabled by --disable-ai-classification")
    elif firefox_mode:
        _flush_progress_line()
        print("[AI] disabled for Firefox JSON mode (metadata update only; AI should run as a separate process)")
    elif settings.ai_provider == "gemini":
        if settings.gemini_api_key:
            mode_text = "firefox-json-enrich" if firefox_mode else "standard"
            _flush_progress_line()
            print(f"[AI] enabled: provider=gemini model={settings.gemini_model} mode={mode_text}")
        elif settings.openai_api_key:
            mode_text = "firefox-json-enrich" if firefox_mode else "standard"
            _flush_progress_line()
            print(f"[AI] enabled: fallback provider=openai model={settings.openai_model} mode={mode_text}")
        else:
            _flush_progress_line()
            print("[AI] disabled: no GEMINI_API_KEY found")
    else:
        if settings.openai_api_key:
            mode_text = "firefox-json-enrich" if firefox_mode else "standard"
            _flush_progress_line()
            print(f"[AI] enabled: provider=openai model={settings.openai_model} mode={mode_text}")
        else:
            _flush_progress_line()
            print("[AI] disabled: no OPENAI_API_KEY found")

    resume_hours = settings.resume_hours if args.resume_hours is None else args.resume_hours
    history_db_path = Path(settings.history_db_path)
    if not args.dry_run:
        init_history_db(history_db_path)
    else:
        _flush_progress_line()
        print("Dry run enabled: no files or database records will be written.")
    if firefox_mode:
        firefox_csv_path: Path | None = None
        exported_json_path = output_dir / "exported.json"
        valid_urls_txt_path = output_dir / "valid_urls.txt"
        broken_urls_txt_path = output_dir / "broken.txt"
        jobs = extract_firefox_bookmark_jobs(json_data)
        if not jobs:
            raise ValueError(f"No Firefox bookmark URI nodes found in input file: {input_path}")
        if args.max_urls is not None and args.max_urls > 0:
            jobs = jobs[: args.max_urls]
            _flush_progress_line()
            print(f"Quick test mode: processing first {len(jobs)} Firefox bookmark jobs")
        _flush_progress_line()
        print("[phase] metadata-only update for Firefox bookmark nodes")

        cached_results_by_url = (
            fetch_recent_results(
                db_path=history_db_path,
                urls=[url for _, url in jobs],
                max_age_hours=resume_hours,
            )
            if not args.dry_run
            else {}
        )
        _flush_progress_line()
        print(f"[phase] starting cleaning checks for {len(jobs)} bookmark nodes")
        job_results, checked_count, cached_count = await analyzer.analyze_url_jobs(
            jobs,
            cached_results_by_url=cached_results_by_url,
            progress_callback=_progress,
        )
        total = len(job_results)
        valid = len([item for item in job_results.values() if item.ok])
        broken = total - valid
        status_by_node_id = {
            job_id: (STATUS_FOUND if result.ok else STATUS_NOT_FOUND)
            for job_id, result in job_results.items()
        }
        valid_urls = [url for job_id, url in jobs if job_id in job_results and job_results[job_id].ok]
        broken_urls = [url for job_id, url in jobs if job_id in job_results and not job_results[job_id].ok]
        _flush_progress_line()
        print(f"[phase] fetching metadata for {len(jobs)} bookmark nodes")
        metadata_by_node_id = await fetch_metadata_for_jobs(
            jobs,
            check_results_by_job_id=job_results,
            timeout_seconds=settings.request_timeout_seconds,
            concurrency=settings.concurrency,
            user_agent=settings.user_agent,
            domain_min_interval_seconds=settings.domain_min_interval_seconds,
            progress_callback=lambda done, total: _progress("metadata-fetch", done, total),
        )

        for job_id, _ in jobs:
            existing_meta = metadata_by_node_id.get(job_id)
            title_value = existing_meta.title if existing_meta else None
            description_value = existing_meta.description if existing_meta else None
            keywords_value = list(existing_meta.keywords) if existing_meta else []
            if job_id in job_results and not job_results[job_id].ok and "404_not_found" not in keywords_value:
                keywords_value.append("404_not_found")
            metadata_by_node_id[job_id] = PageMetadata(
                title=title_value,
                description=description_value,
                keywords=keywords_value,
            )

        if args.dry_run:
            updated, matched, metadata_updates = 0, len(status_by_node_id), 0
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            updated, matched, metadata_updates = annotate_firefox_export_status(
                input_path=input_path,
                output_path=exported_json_path,
                status_by_node_id=status_by_node_id,
                metadata_by_node_id=metadata_by_node_id,
            )
            valid_urls_txt_path.write_text("\n".join(valid_urls) + "\n", encoding="utf-8")
            broken_urls_txt_path.write_text("\n".join(broken_urls) + "\n", encoding="utf-8")
            upsert_results(history_db_path, list(job_results.values()))

        if args.summary_csv and not args.dry_run:
            csv_rows: list[dict[str, str | int | bool | None]] = []
            for job_id, url in jobs:
                result = job_results.get(job_id)
                if result is None:
                    continue
                csv_rows.append(
                    {
                        "job_id": job_id,
                        "url": url,
                        "ok": result.ok,
                        "status_code": result.status_code,
                        "via": result.via,
                        "response_time_ms": result.response_time_ms,
                        "error": result.error,
                        "source": "cache" if url in cached_results_by_url else "fresh",
                    }
                )
            firefox_csv_path = export_job_summary_csv(csv_rows, Path(args.summary_csv))

        if not args.dry_run:
            finished_ts = time.time()
            insert_run_log(
                history_db_path,
                started_ts=started_ts,
                finished_ts=finished_ts,
                input_path=str(input_path),
                mode="firefox-json",
                total=total,
                valid=valid,
                broken=broken,
                cached=cached_count,
                checked=checked_count,
            )
        _flush_progress_line()
        print(f"Total URLs: {total}")
        print(f"Valid: {valid} | Broken: {broken}")
        print(f"Check: {valid} + {broken} = {total}")
        print(f"Fresh checks: {checked_count} | Cached: {cached_count}")
        print(f"Annotated bookmarks (matched): {matched}")
        print(f"Metadata-updated nodes (description/keyword): {updated}")
        print(f"Metadata field updates applied: {metadata_updates}")
        if not args.dry_run:
            print(f"Updated file: {exported_json_path}")
            print(f"Valid URLs TXT: {valid_urls_txt_path}")
            print(f"Broken URLs TXT: {broken_urls_txt_path}")
        else:
            print("Updated file: (skipped in dry run)")
        if firefox_csv_path is not None:
            print(f"Summary CSV: {firefox_csv_path}")
        print(f"History DB: {history_db_path}" if not args.dry_run else "History DB: (skipped in dry run)")
        return 0

    urls = load_urls(input_path)
    if not urls:
        raise ValueError(f"No URLs found in input file: {input_path}")
    if args.max_urls is not None and args.max_urls > 0:
        urls = urls[: args.max_urls]
        _flush_progress_line()
        print(f"Quick test mode: processing first {len(urls)} URLs")

    cached_results_by_url = (
        fetch_recent_results(
            db_path=history_db_path,
            urls=urls,
            max_age_hours=resume_hours,
        )
        if not args.dry_run
        else {}
    )
    reports, checked_count, cached_count = await analyzer.analyze(
        urls,
        cached_results_by_url=cached_results_by_url,
        progress_callback=_progress,
    )
    total = len(reports)
    valid = len([item for item in reports if item.check.ok])
    broken = total - valid

    ai_merged_path: Path | None = None
    ai_merged_updated = 0
    ai_merged_matched = 0
    if args.ai_merge_exported_json:
        merge_target_path = Path(args.ai_merge_exported_json)
        ai_merged_path = merge_target_path
        classifications_by_url = {
            report.url: report.classification
            for report in reports
            if report.classification is not None
        }
        if args.dry_run:
            print("[AI merge] skipped in dry run")
        else:
            ai_merged_updated, ai_merged_matched = merge_ai_into_firefox_export_by_url(
                input_path=merge_target_path,
                output_path=merge_target_path,
                classifications_by_url=classifications_by_url,
            )

    standard_csv_path: Path | None = None
    if not args.dry_run:
        upsert_results(history_db_path, [report.check for report in reports])
        paths = export_reports(reports, output_dir=output_dir)
    else:
        paths = {"valid": "(skipped in dry run)", "broken": "(skipped in dry run)", "full_report": "(skipped in dry run)"}
    if args.summary_csv and not args.dry_run:
        standard_csv_path = export_report_summary_csv(reports, Path(args.summary_csv))

    if not args.dry_run:
        finished_ts = time.time()
        insert_run_log(
            history_db_path,
            started_ts=started_ts,
            finished_ts=finished_ts,
            input_path=str(input_path),
            mode="standard",
            total=total,
            valid=valid,
            broken=broken,
            cached=cached_count,
            checked=checked_count,
        )
    _flush_progress_line()
    print(f"Total URLs (after dedupe mode): {total}")
    print(f"Valid: {valid} | Broken: {broken}")
    print(f"Check: {valid} + {broken} = {total}")
    print(f"Fresh checks: {checked_count} | Cached: {cached_count}")
    print(f"Valid JSON: {paths['valid']}")
    print(f"Broken JSON: {paths['broken']}")
    print(f"Full report: {paths['full_report']}")
    if ai_merged_path is not None:
        if args.dry_run:
            print(f"AI merged JSON: (skipped in dry run) target={ai_merged_path}")
        else:
            print(f"AI merged JSON: {ai_merged_path}")
            print(f"AI merged nodes: {ai_merged_updated} | Matched nodes: {ai_merged_matched}")
    if standard_csv_path is not None:
        print(f"Summary CSV: {standard_csv_path}")
    print(f"History DB: {history_db_path}" if not args.dry_run else "History DB: (skipped in dry run)")
    return 0


def main() -> None:
    # Ensure progress/status logs are visible immediately in buffered terminals.
    try:
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    except AttributeError:
        pass
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()
    log_path = _setup_run_log(Path(args.output_dir))
    print(f"[log] writing run events to: {log_path}")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
