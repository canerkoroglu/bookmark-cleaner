from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(slots=True)
class CategoryRules:
    domain: dict[str, str]
    contains: dict[str, str]

    def category_for_url(self, url: str) -> str | None:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]

        if domain in self.domain:
            return self.domain[domain]

        lowered_url = url.lower()
        for needle, category in self.contains.items():
            if needle and needle in lowered_url:
                return category
        return None


def load_category_rules(path: str | None) -> CategoryRules | None:
    if not path:
        return None

    file_path = Path(path)
    if not file_path.exists():
        raise ValueError(f"Category rules file does not exist: {file_path}")

    payload = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Category rules file must be a JSON object.")

    domain_map_raw = payload.get("domain", {})
    contains_map_raw = payload.get("contains", {})
    if not isinstance(domain_map_raw, dict) or not isinstance(contains_map_raw, dict):
        raise ValueError("Category rules file must define 'domain' and 'contains' objects.")

    domain_map = {str(k).strip().lower(): str(v).strip().lower() for k, v in domain_map_raw.items() if str(k).strip()}
    contains_map = {str(k).strip().lower(): str(v).strip().lower() for k, v in contains_map_raw.items() if str(k).strip()}
    return CategoryRules(domain=domain_map, contains=contains_map)

