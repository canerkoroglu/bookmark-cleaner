from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import BookmarkReport


def export_reports(reports: list[BookmarkReport], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    valid = [item.to_dict() for item in reports if item.check.ok]
    broken = [item.to_dict() for item in reports if not item.check.ok]
    all_items = [item.to_dict() for item in reports]

    valid_path = output_dir / "valid_bookmarks.json"
    broken_path = output_dir / "broken_bookmarks.json"
    report_path = output_dir / "bookmark_report.json"

    valid_path.write_text(json.dumps(valid, indent=2, ensure_ascii=False), encoding="utf-8")
    broken_path.write_text(json.dumps(broken, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(json.dumps(all_items, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"valid": valid_path, "broken": broken_path, "full_report": report_path}


def export_report_summary_csv(reports: list[BookmarkReport], csv_path: Path) -> Path:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "url",
                "domain",
                "ok",
                "status_code",
                "via",
                "response_time_ms",
                "deduplicated",
                "duplicate_count",
                "error",
            ]
        )
        for report in reports:
            writer.writerow(
                [
                    report.url,
                    report.domain,
                    report.check.ok,
                    report.check.status_code,
                    report.check.via,
                    report.check.response_time_ms,
                    report.deduplicated,
                    len(report.duplicate_urls),
                    report.check.error or "",
                ]
            )
    return csv_path


def export_job_summary_csv(
    rows: list[dict[str, str | int | bool | None]],
    csv_path: Path,
) -> Path:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "job_id",
                "url",
                "ok",
                "status_code",
                "via",
                "response_time_ms",
                "error",
                "source",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.get("job_id"),
                    row.get("url"),
                    row.get("ok"),
                    row.get("status_code"),
                    row.get("via"),
                    row.get("response_time_ms"),
                    row.get("error") or "",
                    row.get("source"),
                ]
            )
    return csv_path
