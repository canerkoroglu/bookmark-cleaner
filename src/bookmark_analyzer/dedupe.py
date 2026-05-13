from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class DedupeResult:
    unique_urls: list[str]
    duplicates_by_canonical: dict[str, list[str]]
    canonical_by_url: dict[str, str]


def deduplicate_by_domain(urls: list[str]) -> DedupeResult:
    # Backward-compatible function name; dedupe now uses exact URL identity.
    seen: dict[str, str] = {}
    unique_urls: list[str] = []
    duplicates_by_canonical: dict[str, list[str]] = {}
    canonical_by_url: dict[str, str] = {}

    for url in urls:
        canonical = seen.get(url)
        if canonical is None:
            seen[url] = url
            unique_urls.append(url)
            duplicates_by_canonical[url] = []
            canonical_by_url[url] = url
            continue

        duplicates_by_canonical[canonical].append(url)
        canonical_by_url[url] = canonical

    return DedupeResult(
        unique_urls=unique_urls,
        duplicates_by_canonical=duplicates_by_canonical,
        canonical_by_url=canonical_by_url,
    )
