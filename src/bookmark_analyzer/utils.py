from __future__ import annotations

from urllib.parse import urlparse


def normalize_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        return value
    if not value.startswith(("http://", "https://")):
        return f"https://{value}"
    return value


def extract_domain(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host
