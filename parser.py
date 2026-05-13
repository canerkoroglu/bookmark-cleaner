from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def extract_urls_from_firefox_json(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    urls: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            candidate = node.get("uri") or node.get("url")
            if isinstance(candidate, str) and _is_http_url(candidate):
                url = candidate.strip()
                if url not in seen:
                    seen.add(url)
                    urls.append(url)

            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
            return

        if isinstance(node, list):
            for child in node:
                walk(child)
            return

        if isinstance(node, str) and _is_http_url(node):
            url = node.strip()
            if url not in seen:
                seen.add(url)
                urls.append(url)

    walk(data)
    return urls
