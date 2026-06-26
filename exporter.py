from __future__ import annotations

import asyncio
import csv
from pathlib import Path
from typing import Optional


CSV_COLUMNS = ["site_url", "status", "title", "description", "keywords", "comments"]


def _csv_status(job_status: str) -> str:
    if job_status == "done":
        return "working"
    if job_status == "failed":
        return "broken"
    return job_status


def export_jobs_to_csv(queue, output_path: Path) -> None:
    """Write a complete CSV from the database (overwrites file)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = queue.fetch_all_rows()
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "site_url": row["url"],
                    "status": _csv_status(row["status"]),
                    "title": row["title"] or "",
                    "description": row["description"] or "",
                    "keywords": row["keywords"] or "",
                    "comments": row["comments"] or "",
                }
            )


class CsvIncrementalExporter:
    """Append rows to the CSV file incrementally in an async-safe way.

    Note: this produces a running snapshot. The final export_jobs_to_csv call
    still writes a complete CSV (overwriting the file) at the end of the run.
    """

    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        # Ensure header exists
        existed = self.output_path.exists()
        mode = "a" if existed else "w"
        with self.output_path.open(mode, encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if not existed:
                writer.writeheader()

    async def append_row(
        self,
        *,
        site_url: str,
        status: str,
        title: Optional[str] = "",
        description: Optional[str] = "",
        keywords: Optional[str] = "",
        comments: Optional[str] = "",
    ) -> None:
        async with self._lock:
            with self.output_path.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writerow(
                    {
                        "site_url": site_url,
                        "status": _csv_status(status),
                        "title": title or "",
                        "description": description or "",
                        "keywords": keywords or "",
                        "comments": comments or "",
                    }
                )
