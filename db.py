from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


JOB_STATUSES = {"pending", "processing", "done", "failed"}


@dataclass(frozen=True)
class Job:
    id: int
    url: str
    status: str
    retries: int


class JobQueue:
    """Small SQLite-backed queue with atomic single-job claims."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()
        schema_sql = row["sql"] if row is not None else None
        if schema_sql and "url TEXT UNIQUE NOT NULL" in schema_sql:
            self._migrate_jobs_table()
        else:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'done', 'failed')),
                    retries INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    http_status INTEGER,
                    title TEXT,
                    description TEXT,
                    keywords TEXT,
                    comments TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_status_retries_id
            ON jobs(status, retries, id)
            """
        )

    def _migrate_jobs_table(self) -> None:
        self.conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
        self.conn.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'done', 'failed')),
                retries INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                http_status INTEGER,
                title TEXT,
                description TEXT,
                keywords TEXT,
                comments TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO jobs (
                id, url, status, retries, last_error, http_status,
                title, description, keywords, comments, created_at, updated_at
            )
            SELECT
                id, url, status, retries, last_error, http_status,
                title, description, keywords, comments, created_at, updated_at
            FROM jobs_old
            ORDER BY id
            """
        )
        self.conn.execute("DROP TABLE jobs_old")

    def reset(self) -> None:
        self.conn.execute("DELETE FROM jobs")

    def recover_processing_jobs(self) -> int:
        cursor = self.conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                last_error = COALESCE(last_error, 'Recovered from interrupted processing state'),
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing'
            """
        )
        return cursor.rowcount

    def insert_urls(self, urls: Iterable[str]) -> int:
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT INTO jobs (url, status)
            VALUES (?, 'pending')
            """,
            [(url,) for url in urls],
        )
        return self.conn.total_changes - before

    def claim_next(self, retry_limit: int) -> Job | None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                """
                SELECT id, url, status, retries
                FROM jobs
                WHERE status = 'pending'
                   OR (status = 'failed' AND retries < ?)
                ORDER BY id
                LIMIT 1
                """,
                (retry_limit,),
            ).fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None

            self.conn.execute(
                """
                UPDATE jobs
                SET status = 'processing',
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (row["id"],),
            )
            self.conn.execute("COMMIT")
            return Job(id=row["id"], url=row["url"], status=row["status"], retries=row["retries"])
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def mark_done(
        self,
        job_id: int,
        *,
        http_status: int | None,
        title: str,
        description: str,
        keywords: str,
        comments: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE jobs
            SET status = 'done',
                last_error = NULL,
                http_status = ?,
                title = ?,
                description = ?,
                keywords = ?,
                comments = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (http_status, title, description, keywords, comments, job_id),
        )

    def mark_failed(
        self,
        job_id: int,
        *,
        error: str,
        http_status: int | None = None,
        title: str = "",
        description: str = "",
        keywords: str = "",
        comments: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                retries = retries + 1,
                last_error = ?,
                http_status = ?,
                title = COALESCE(NULLIF(?, ''), title),
                description = COALESCE(NULLIF(?, ''), description),
                keywords = COALESCE(NULLIF(?, ''), keywords),
                comments = COALESCE(NULLIF(?, ''), comments),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error[:4000], http_status, title, description, keywords, comments, job_id),
        )

    def count_eligible(self, retry_limit: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM jobs
            WHERE status = 'pending'
               OR (status = 'failed' AND retries < ?)
            """,
            (retry_limit,),
        ).fetchone()
        return int(row["total"])

    def counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    def fetch_failed_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM jobs WHERE status = 'failed' ORDER BY id"))

    def fetch_all_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM jobs ORDER BY id"))

    def fetch_ai_candidates(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = """
            SELECT id, url, title, description
            FROM jobs
            WHERE status = 'done'
              AND (comments IS NULL OR TRIM(comments) = '')
            ORDER BY id
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return list(self.conn.execute(sql, params))

    def update_comments(self, job_id: int, comments: str) -> None:
        self.conn.execute(
            """
            UPDATE jobs
            SET comments = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (comments, job_id),
        )
