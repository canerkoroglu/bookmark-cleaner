from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .models import HttpCheckResult


def init_history_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS url_checks (
                url TEXT PRIMARY KEY,
                last_checked_ts REAL NOT NULL,
                ok INTEGER NOT NULL,
                status_code INTEGER,
                final_url TEXT,
                error TEXT,
                via TEXT,
                response_time_ms INTEGER,
                check_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_ts REAL NOT NULL,
                finished_ts REAL NOT NULL,
                input_path TEXT NOT NULL,
                mode TEXT NOT NULL,
                total INTEGER NOT NULL,
                valid INTEGER NOT NULL,
                broken INTEGER NOT NULL,
                cached INTEGER NOT NULL,
                checked INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def fetch_recent_results(
    db_path: Path,
    urls: list[str],
    max_age_hours: float,
) -> dict[str, HttpCheckResult]:
    if max_age_hours <= 0 or not urls or not db_path.exists():
        return {}

    cutoff_ts = time.time() - (max_age_hours * 3600)
    unique_urls = list(dict.fromkeys(urls))
    results: dict[str, HttpCheckResult] = {}

    with sqlite3.connect(db_path) as conn:
        for i in range(0, len(unique_urls), 500):
            chunk = unique_urls[i : i + 500]
            placeholders = ",".join(["?"] * len(chunk))
            rows = conn.execute(
                f"""
                SELECT url, ok, status_code, final_url, error, via, response_time_ms
                FROM url_checks
                WHERE url IN ({placeholders}) AND last_checked_ts >= ?
                """,
                [*chunk, cutoff_ts],
            ).fetchall()
            for url, ok, status_code, final_url, error, via, response_time_ms in rows:
                results[url] = HttpCheckResult(
                    url=url,
                    ok=bool(ok),
                    status_code=status_code,
                    final_url=final_url,
                    error=error,
                    via=via or "history-cache",
                    response_time_ms=response_time_ms,
                )

    return results


def upsert_results(db_path: Path, results: list[HttpCheckResult]) -> None:
    if not results:
        return
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO url_checks (
                url, last_checked_ts, ok, status_code, final_url, error, via, response_time_ms, check_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(url) DO UPDATE SET
                last_checked_ts=excluded.last_checked_ts,
                ok=excluded.ok,
                status_code=excluded.status_code,
                final_url=excluded.final_url,
                error=excluded.error,
                via=excluded.via,
                response_time_ms=excluded.response_time_ms,
                check_count=url_checks.check_count + 1
            """,
            [
                (
                    item.url,
                    now,
                    1 if item.ok else 0,
                    item.status_code,
                    item.final_url,
                    item.error,
                    item.via,
                    item.response_time_ms,
                )
                for item in results
            ],
        )
        conn.commit()


def insert_run_log(
    db_path: Path,
    *,
    started_ts: float,
    finished_ts: float,
    input_path: str,
    mode: str,
    total: int,
    valid: int,
    broken: int,
    cached: int,
    checked: int,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                started_ts, finished_ts, input_path, mode, total, valid, broken, cached, checked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_ts,
                finished_ts,
                input_path,
                mode,
                total,
                valid,
                broken,
                cached,
                checked,
            ),
        )
        conn.commit()
