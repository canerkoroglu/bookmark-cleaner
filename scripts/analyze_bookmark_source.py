from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

SOURCE = Path("examples/ck-bookmark.json")


def normalize_domain(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def normalize_folder_path(folder_path: str) -> str:
    lowered = folder_path.lower()
    if lowered.startswith("toolbar / "):
        return folder_path[len("toolbar / ") :]
    if lowered == "toolbar":
        return ""
    return folder_path


def walk(node: object, folders: list[str], leaf_items: list[tuple[list[str], str, str]], folder_counts: Counter[str]) -> None:
    if isinstance(node, dict):
        node_type = node.get("type")
        title = str(node.get("title") or "").strip()

        if node_type == "text/x-moz-place-container":
            next_folders = folders + ([title] if title else [])
            children = node.get("children")
            if isinstance(children, list):
                for child in children:
                    walk(child, next_folders, leaf_items, folder_counts)
            return

        if node_type == "text/x-moz-place" and isinstance(node.get("uri"), str):
            url = node["uri"].strip()
            if url:
                leaf_items.append((folders, title, url))
                if folders:
                    folder_counts[" / ".join(folders)] += 1
            return

        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, folders, leaf_items, folder_counts)

    elif isinstance(node, list):
        for item in node:
            walk(item, folders, leaf_items, folder_counts)


def classify_bookmark(folders: list[str], title: str, url: str, category_rules: dict[str, list[str]]) -> str:
    text = " ".join(folders + [title, normalize_domain(url)]).lower()
    for name, patterns in category_rules.items():
        if any(re.search(pattern, text) for pattern in patterns):
            return name
    return "other"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze bookmark source and emit category summary + CSV report.")
    parser.add_argument("--input", default=str(SOURCE), help="Bookmark JSON input file path.")
    parser.add_argument(
        "--csv-output",
        default="output/bookmark_source_category_report.csv",
        help="CSV path for per-bookmark category report.",
    )
    parser.add_argument(
        "--report-output",
        default="output/bookmark_source_category_report.md",
        help="Markdown report path for human-friendly analysis summary.",
    )
    return parser.parse_args()


def build_markdown_report(
    total_leaf_items: int,
    unique_domains: int,
    category_counts: Counter[str],
    domain_counts: Counter[str],
    folder_counts: Counter[str],
    top_n: int = 15,
) -> str:
    def _list_block(counter: Counter[str]) -> str:
        lines = []
        for name, count in counter.most_common(top_n):
            pct = (count / total_leaf_items * 100.0) if total_leaf_items else 0.0
            lines.append(f"- {name}: {count} ({pct:.1f}%)")
        return "\n".join(lines) if lines else "- none"

    return (
        "# Bookmark Source Analysis Report\n\n"
        "## Overview\n"
        f"- Total bookmark leaves: {total_leaf_items}\n"
        f"- Unique domains: {unique_domains}\n"
        f"- Category coverage: {len(category_counts)} categories\n\n"
        "## Category Distribution\n"
        f"{_list_block(category_counts)}\n\n"
        "## Top Domains\n"
        f"{_list_block(domain_counts)}\n\n"
        "## Top Folder Paths\n"
        f"{_list_block(folder_counts)}\n"
    )


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    csv_output_path = Path(args.csv_output)
    report_output_path = Path(args.report_output)
    data = json.loads(input_path.read_text(encoding="utf-8"))

    leaf_items: list[tuple[list[str], str, str]] = []
    folder_counts: Counter[str] = Counter()
    walk(data, [], leaf_items, folder_counts)

    domain_counts = Counter(normalize_domain(url) for _, _, url in leaf_items if normalize_domain(url))

    category_rules: dict[str, list[str]] = {
        "developer": [r"github", r"stack", r"dev", r"api", r"docs?", r"program", r"code", r"linux", r"kubernetes", r"docker", r"python", r"node"],
        "news": [r"news", r"times", r"post", r"journal", r"reuters", r"bloomberg", r"cnn", r"bbc", r"hacker news"],
        "video": [r"youtube", r"vimeo", r"netflix", r"twitch"],
        "social": [r"x\\.com", r"twitter", r"reddit", r"facebook", r"instagram", r"linkedin", r"discord"],
        "shopping": [r"amazon", r"ebay", r"aliexpress", r"shop", r"store"],
        "tools": [r"figma", r"notion", r"canva", r"drive\\.google", r"calendar", r"jira", r"trello"],
        "reference": [r"wikipedia", r"mdn", r"stackoverflow", r"archive", r"dictionary"],
    }

    category_counts: Counter[str] = Counter()
    rows: list[dict[str, str]] = []
    normalized_folder_counts: Counter[str] = Counter()
    for folders, title, url in leaf_items:
        category = classify_bookmark(folders, title, url, category_rules)
        category_counts[category] += 1
        normalized_folder_path = normalize_folder_path(" / ".join(folders))
        if normalized_folder_path:
            normalized_folder_counts[normalized_folder_path] += 1
        rows.append(
            {
                "folder_path": normalized_folder_path,
                "title": title,
                "url": url,
                "domain": normalize_domain(url),
                "category": category,
            }
        )

    csv_output_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["folder_path", "title", "url", "domain", "category"])
        writer.writeheader()
        writer.writerows(rows)

    report_output_path.parent.mkdir(parents=True, exist_ok=True)
    report_output_path.write_text(
        build_markdown_report(
            total_leaf_items=len(leaf_items),
            unique_domains=len(domain_counts),
            category_counts=category_counts,
            domain_counts=domain_counts,
            folder_counts=normalized_folder_counts,
        ),
        encoding="utf-8",
    )

    print(f"TOTAL_BOOKMARK_LEAVES={len(leaf_items)}")
    print(f"UNIQUE_DOMAINS={len(domain_counts)}")
    print("TOP_DOMAINS=" + "; ".join(f"{domain}:{count}" for domain, count in domain_counts.most_common(15)))
    print("TOP_FOLDERS=" + "; ".join(f"{folder}:{count}" for folder, count in normalized_folder_counts.most_common(15)))
    print("CATEGORY_ESTIMATE=" + "; ".join(f"{name}:{count}" for name, count in category_counts.most_common()))
    print(f"CSV_REPORT={csv_output_path}")
    print(f"MARKDOWN_REPORT={report_output_path}")


if __name__ == "__main__":
    main()

