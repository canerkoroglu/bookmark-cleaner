from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from env_loader import load_env_file  # noqa: E402


def main() -> int:
    load_env_file(ROOT_DIR / ".env")

    api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    retry_attempts = env_int("BA_RETRY_ATTEMPTS", 2)
    retry_backoff_seconds = env_float("BA_RETRY_BACKOFF_SECONDS", 0.7)
    request_timeout_seconds = env_float("BA_REQUEST_TIMEOUT_SECONDS", 30.0)
    if not api_key:
        print("FAIL: GEMINI_API_KEY is not set in the environment or .env")
        return 1

    prompt = 'Answer with one word only: What is the capital of Turkey?'
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    total_attempts = retry_attempts + 1
    payload: dict | None = None
    for attempt in range(total_attempts):
        try:
            with urllib.request.urlopen(request, timeout=request_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
                break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code not in {429, 500, 502, 503, 504} or attempt == total_attempts - 1:
                print(f"FAIL: Gemini API returned HTTP {exc.code}: {detail}")
                return 1
            wait_seconds = retry_backoff_seconds * (2**attempt)
            print(f"Retrying transient Gemini HTTP {exc.code} in {wait_seconds:.1f}s...")
            time.sleep(wait_seconds)
        except Exception as exc:  # noqa: BLE001
            if attempt == total_attempts - 1:
                print(f"FAIL: Gemini request failed: {type(exc).__name__}: {exc}")
                return 1
            wait_seconds = retry_backoff_seconds * (2**attempt)
            print(f"Retrying Gemini request after {type(exc).__name__} in {wait_seconds:.1f}s...")
            time.sleep(wait_seconds)

    if payload is None:
        print("FAIL: Gemini request did not return a response")
        return 1

    answer = extract_text(payload)
    normalized = answer.strip().lower().strip(".!\"'")
    if "ankara" not in normalized:
        print(f"FAIL: expected answer to contain 'Ankara', got: {answer!r}")
        return 1

    print(f"OK: Gemini API key works. Model {model!r} answered: {answer.strip()!r}")
    return 0


def extract_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    return " ".join(
        str(part.get("text", "")).strip()
        for part in parts
        if isinstance(part, dict) and part.get("text")
    )


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


if __name__ == "__main__":
    raise SystemExit(main())
