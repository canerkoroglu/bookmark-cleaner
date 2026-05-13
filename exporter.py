from __future__ import annotations

import csv
from pathlib import Path

from db import JobQueue


CSV_COLUMNS = ["site_url", "status", "title", "description", "keywords", "comments"]


def export_jobs_to_csv(queue: JobQueue, output_path: Path) -> None:
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


def _csv_status(job_status: str) -> str:
    if job_status == "done":
        return "working"
    if job_status == "failed":
        return "broken"
    return job_status
