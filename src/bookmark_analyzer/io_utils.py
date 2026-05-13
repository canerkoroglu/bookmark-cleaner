from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import AiClassification, PageMetadata
from .utils import extract_domain
from .utils import normalize_url

STATUS_FOUND = "#200_found"
STATUS_NOT_FOUND = "#404_notfound"


def _node_identifier(node: dict[str, Any], path: str) -> str:
    guid = node.get("guid")
    if isinstance(guid, str) and guid:
        return f"guid:{guid}:{path}"
    node_id = node.get("id")
    if node_id is not None:
        return f"id:{node_id}:{path}"
    return f"path:{path}"


def _extract_urls_from_nested_json(node: Any) -> list[str]:
    urls: list[str] = []

    if isinstance(node, dict):
        # Firefox export leaf nodes use `uri`; some formats use `url`.
        candidate = node.get("uri") or node.get("url")
        if isinstance(candidate, str):
            cleaned = normalize_url(candidate)
            if cleaned:
                urls.append(cleaned)

        # Traverse nested objects/lists (including `children`) for all exporter formats.
        for value in node.values():
            if isinstance(value, (dict, list)):
                urls.extend(_extract_urls_from_nested_json(value))

    elif isinstance(node, list):
        for item in node:
            urls.extend(_extract_urls_from_nested_json(item))

    return urls


def load_json_data(input_path: Path) -> Any:
    raw = input_path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return json.loads(raw)


def is_firefox_bookmark_export(data: Any) -> bool:
    return isinstance(data, dict) and data.get("type") == "text/x-moz-place-container"


def _extract_firefox_bookmark_nodes(node: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if isinstance(node.get("uri"), str):
            results.append(node)
        for value in node.values():
            if isinstance(value, (dict, list)):
                results.extend(_extract_firefox_bookmark_nodes(value))
    elif isinstance(node, list):
        for item in node:
            results.extend(_extract_firefox_bookmark_nodes(item))
    return results


def extract_firefox_bookmark_jobs(data: Any) -> list[tuple[str, str]]:
    """Return per-bookmark jobs while walking children hierarchy."""
    jobs: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            uri = node.get("uri")
            if node_type == "text/x-moz-place" and isinstance(uri, str):
                normalized = normalize_url(uri)
                if normalized:
                    jobs.append((_node_identifier(node, path), normalized))

            children = node.get("children")
            if isinstance(children, list):
                for idx, child in enumerate(children):
                    walk(child, f"{path}.{idx}")
            return

        if isinstance(node, list):
            for idx, child in enumerate(node):
                walk(child, f"{path}.{idx}")

    walk(data, "root")
    return jobs


def annotate_firefox_export_status(
    input_path: Path,
    output_path: Path | None = None,
    status_by_node_id: dict[str, str] | None = None,
    metadata_by_node_id: dict[str, PageMetadata] | None = None,
    status_by_url: dict[str, str] | None = None,
    status_by_domain: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    data = load_json_data(input_path)
    if data is None or not is_firefox_bookmark_export(data):
        return (0, 0, 0)

    updated = 0
    matched = 0
    metadata_updates = 0

    def walk(node: Any, path: str) -> None:
        nonlocal updated, matched, metadata_updates

        if isinstance(node, dict):
            node_type = node.get("type")
            uri = node.get("uri")
            if node_type == "text/x-moz-place" and isinstance(uri, str):
                normalized = normalize_url(uri)
                node_key = _node_identifier(node, path)
                keyword = None
                if status_by_node_id:
                    keyword = status_by_node_id.get(node_key)
                if keyword is None and status_by_url:
                    keyword = status_by_url.get(normalized)
                if keyword is None and status_by_domain:
                    domain = extract_domain(normalized)
                    keyword = status_by_domain.get(domain)

                if keyword is not None:
                    matched += 1
                    meta = metadata_by_node_id.get(node_key) if metadata_by_node_id else None

                    # Firefox timestamp convention is microseconds since epoch.
                    node["lastModified"] = int(time.time() * 1_000_000)

                    if meta:
                        changed_meta = False
                        if meta.description:
                            if node.get("description") != meta.description:
                                node["description"] = meta.description
                                changed_meta = True
                        if meta.keywords:
                            keyword_value = ", ".join(meta.keywords)
                            if node.get("keyword") != keyword_value:
                                node["keyword"] = keyword_value
                                changed_meta = True
                        if changed_meta:
                            updated += 1
                            metadata_updates += 1

            children = node.get("children")
            if isinstance(children, list):
                for idx, child in enumerate(children):
                    walk(child, f"{path}.{idx}")
            return

        if isinstance(node, list):
            for idx, child in enumerate(node):
                walk(child, f"{path}.{idx}")

    walk(data, "root")

    target_path = output_path or input_path
    target_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
    return updated, matched, metadata_updates


def merge_ai_into_firefox_export_by_url(
    input_path: Path,
    classifications_by_url: dict[str, AiClassification],
    output_path: Path | None = None,
) -> tuple[int, int]:
    data = load_json_data(input_path)
    if data is None or not is_firefox_bookmark_export(data):
        return (0, 0)

    updated = 0
    matched = 0

    def walk(node: Any) -> None:
        nonlocal updated, matched
        if isinstance(node, dict):
            node_type = node.get("type")
            uri = node.get("uri")
            if node_type == "text/x-moz-place" and isinstance(uri, str):
                normalized = normalize_url(uri)
                classification = classifications_by_url.get(normalized)
                if classification is not None:
                    matched += 1
                    changed = False

                    summary = classification.summary.strip()
                    if summary and node.get("description") != summary:
                        node["description"] = summary
                        changed = True

                    existing_keywords_raw = str(node.get("keyword", "")).strip()
                    existing_keywords = [k.strip() for k in existing_keywords_raw.split(",") if k.strip()]
                    merged_keywords: list[str] = []
                    seen: set[str] = set()
                    for keyword in existing_keywords + classification.tags:
                        lowered = keyword.lower()
                        if lowered in seen:
                            continue
                        seen.add(lowered)
                        merged_keywords.append(keyword)
                    if merged_keywords:
                        merged_value = ", ".join(merged_keywords)
                        if node.get("keyword") != merged_value:
                            node["keyword"] = merged_value
                            changed = True

                    if changed:
                        node["lastModified"] = int(time.time() * 1_000_000)
                        updated += 1

            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
            return

        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    target_path = output_path or input_path
    target_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
    return updated, matched


def load_urls(input_path: Path) -> list[str]:
    raw = input_path.read_text(encoding="utf-8").strip()
    if not raw:
        return []

    if input_path.suffix.lower() == ".json":
        data = json.loads(raw)
        if isinstance(data, list):
            if all(isinstance(item, str) for item in data):
                values = [str(item) for item in data]
            else:
                values = _extract_urls_from_nested_json(data)
        elif isinstance(data, dict) and isinstance(data.get("urls"), list):
            values = [str(item) for item in data["urls"]]
        elif isinstance(data, dict):
            values = _extract_urls_from_nested_json(data)
        else:
            raise ValueError(
                "Unsupported JSON format. Use a URL list, {'urls': [...]}, or nested bookmark JSON."
            )
    else:
        values = [line for line in raw.splitlines() if line.strip()]

    cleaned = [normalize_url(value) for value in values if normalize_url(value)]
    # Preserve order while removing exact duplicates.
    return list(dict.fromkeys(cleaned))
