from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from .models import AiClassification
from .rate_limit import ApiQuotaLimiter


CLASSIFIER_PROMPT = """
You classify bookmarks.
Return ONLY valid JSON with this exact schema:
{
  "category": "string",
  "tags": ["string"],
  "summary": "string",
  "confidence": 0.0
}

Allowed category values:
["news", "docs", "developer", "reference", "shopping", "social", "video", "tools", "other"]
Keep summary under 25 words.
Confidence must be between 0 and 1.
""".strip()

ALLOWED_CATEGORIES = {
    "news",
    "docs",
    "developer",
    "reference",
    "shopping",
    "social",
    "video",
    "tools",
    "other",
}


def _estimate_tokens(text: str) -> int:
    # Fast approximation for scheduling against token-per-minute quotas.
    return max(1, math.ceil(len(text) / 4))


def _normalize_classification(parsed: dict[str, object], model: str) -> AiClassification:
    category = str(parsed.get("category", "other")).strip().lower()
    if category not in ALLOWED_CATEGORIES:
        category = "other"

    tags_raw = parsed.get("tags", [])
    tags = [str(item).strip() for item in tags_raw] if isinstance(tags_raw, list) else []
    tags = [item for item in tags if item][:8]

    summary = str(parsed.get("summary", "")).strip()
    confidence_raw = parsed.get("confidence", 0.0)
    if isinstance(confidence_raw, (int, float, str)):
        try:
            confidence = float(confidence_raw)
        except ValueError:
            confidence = 0.0
    else:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return AiClassification(
        category=category,
        tags=tags,
        summary=summary,
        confidence=confidence,
        model=model,
    )


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return cleaned


class OpenAIClassifier:
    def __init__(self, api_key: str, model: str, max_content_chars: int = 5000) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "OpenAI package not installed. Install dependencies with: pip install -e ."
            ) from exc

        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model
        self._max_content_chars = max_content_chars

    async def classify(self, url: str, title_hint: str | None = None) -> AiClassification:
        title_hint_text = title_hint or ""
        user_input = (
            f"Classify this bookmark.\n"
            f"URL: {url}\n"
            f"Title hint: {title_hint_text[: self._max_content_chars]}"
        )

        response = await self._client.responses.create(
            model=self._model,
            input=f"{CLASSIFIER_PROMPT}\n\n{user_input}",
        )

        output = response.output_text.strip()
        parsed = json.loads(_strip_code_fence(output))
        return _normalize_classification(parsed, self._model)

    async def classify_many(
        self,
        urls: list[str],
        concurrency: int = 8,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, AiClassification]:
        semaphore = asyncio.Semaphore(concurrency)

        async def _run(url: str) -> tuple[str, AiClassification | None]:
            async with semaphore:
                try:
                    result = await self.classify(url)
                    return url, result
                except Exception:  # noqa: BLE001
                    return url, None

        tasks: list[asyncio.Task[tuple[str, AiClassification | None]]] = [
            asyncio.create_task(_run(url)) for url in urls
        ]
        pairs: list[tuple[str, AiClassification | None]] = []
        completed = 0
        total = len(tasks)
        for task in asyncio.as_completed(tasks):
            pairs.append(await task)
            completed += 1
            if progress_callback:
                progress_callback(completed, total)
        return {url: value for url, value in pairs if value is not None}


class GeminiClassifier:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_content_chars: int = 5000,
        timeout_seconds: float = 30.0,
        requests_per_minute: int = 5,
        tokens_per_minute: int = 250_000,
        requests_per_day: int = 20,
        batch_target_size: int = 150,
        enable_live_logs: bool = False,
        retry_attempts: int = 2,
        retry_backoff_seconds: float = 0.7,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_content_chars = max_content_chars
        self._timeout_seconds = max(5.0, timeout_seconds)
        self._batch_target_size = max(1, batch_target_size)
        self._limiter = ApiQuotaLimiter(
            requests_per_minute=requests_per_minute,
            tokens_per_minute=tokens_per_minute,
            requests_per_day=requests_per_day,
        )
        self._tokens_per_minute = max(1, tokens_per_minute)
        self._requests_per_day = max(1, requests_per_day)
        self._enable_live_logs = enable_live_logs
        self._started_monotonic = time.monotonic()
        self._retry_attempts = max(0, retry_attempts)
        self._retry_backoff_seconds = max(0.1, retry_backoff_seconds)

    def _log(self, message: str) -> None:
        if not self._enable_live_logs:
            return
        elapsed = time.monotonic() - self._started_monotonic
        print(f"[gemini][+{elapsed:7.2f}s] {message}", flush=True)

    async def classify(self, url: str, title_hint: str | None = None) -> AiClassification:
        results = await self.classify_many([url], title_hints={url: title_hint} if title_hint else None)
        classification = results.get(url)
        if classification is None:
            raise RuntimeError(f"Gemini did not return a classification for URL: {url}")
        return classification

    async def classify_many(
        self,
        urls: list[str],
        concurrency: int = 1,
        title_hints: dict[str, str | None] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, AiClassification]:
        del concurrency  # Quotas are controlled centrally by the limiter.
        unique_urls = list(dict.fromkeys(urls))
        if not unique_urls:
            return {}

        if "live" in self._model.lower():
            raise RuntimeError(
                "Gemini Live models are not supported by this tool's REST generateContent path. "
                "Use a non-live model (for example, gemini-2.5-flash) for batch classification."
            )

        batches = self._plan_batches(unique_urls, title_hints or {})
        self._log(
            f"planning complete: urls={len(unique_urls)}, batches={len(batches)}, "
            f"target_batch_size={self._batch_target_size}, model={self._model}"
        )
        results: dict[str, AiClassification] = {}
        completed_urls = 0
        total_urls = len(unique_urls)
        timeout_ctor = getattr(aiohttp, "ClientTimeout")
        session_ctor = getattr(aiohttp, "ClientSession")
        timeout = timeout_ctor(total=self._timeout_seconds)
        async with session_ctor(timeout=timeout) as session:
            for batch_index, batch in enumerate(batches, start=1):
                estimated_tokens = self._estimate_batch_tokens(batch, title_hints or {})
                self._log(
                    f"batch {batch_index}/{len(batches)} queued: size={len(batch)}, "
                    f"estimated_tokens={estimated_tokens}"
                )
                try:
                    request_started = time.monotonic()
                    self._log(f"batch {batch_index}: sending request")
                    payload_text = await self._request_batch(
                        session,
                        batch,
                        title_hints or {},
                        estimated_tokens=estimated_tokens,
                    )
                    parsed = self._parse_batch_response(payload_text, batch)
                    results.update(parsed)
                    request_elapsed = time.monotonic() - request_started
                    self._log(
                        f"batch {batch_index}: completed in {request_elapsed:.2f}s, "
                        f"parsed={len(parsed)}/{len(batch)}, total_parsed={len(results)}"
                    )
                except RuntimeError as exc:
                    message = str(exc).lower()
                    if "quota" in message or "requests/day" in message:
                        self._log(f"batch {batch_index}: stopped ({exc})")
                        break
                    self._log(f"batch {batch_index}: runtime failure ({exc})")
                    completed_urls += len(batch)
                    if progress_callback:
                        progress_callback(min(completed_urls, total_urls), total_urls)
                    continue
                except Exception as exc:  # noqa: BLE001
                    self._log(f"batch {batch_index}: request failed ({type(exc).__name__}: {exc})")
                    completed_urls += len(batch)
                    if progress_callback:
                        progress_callback(min(completed_urls, total_urls), total_urls)
                    continue

                completed_urls += len(batch)
                if progress_callback:
                    progress_callback(min(completed_urls, total_urls), total_urls)

        self._log(f"classification done: classified={len(results)}/{len(unique_urls)}")
        return results

    def _plan_batches(self, urls: list[str], title_hints: dict[str, str | None]) -> list[list[str]]:
        required_batch_size = math.ceil(len(urls) / self._requests_per_day)
        target_size = max(self._batch_target_size, required_batch_size)
        initial_batches = [urls[idx : idx + target_size] for idx in range(0, len(urls), target_size)]

        token_soft_limit = int(self._tokens_per_minute * 0.75)
        planned: list[list[str]] = []
        for batch in initial_batches:
            queue = [batch]
            while queue:
                current = queue.pop(0)
                if len(current) <= 1:
                    planned.append(current)
                    continue
                if self._estimate_batch_tokens(current, title_hints) <= token_soft_limit:
                    planned.append(current)
                    continue
                mid = len(current) // 2
                queue.insert(0, current[mid:])
                queue.insert(0, current[:mid])
        return planned

    def _estimate_batch_tokens(self, batch: list[str], title_hints: dict[str, str | None]) -> int:
        lines: list[str] = []
        for index, url in enumerate(batch, start=1):
            hint = (title_hints.get(url) or "")[: self._max_content_chars]
            if hint:
                lines.append(f"{index}. URL: {url} | title_hint: {hint}")
            else:
                lines.append(f"{index}. URL: {url}")
        prompt = "\n".join(lines)
        # Budget includes prompt + expected structured JSON response size.
        return _estimate_tokens(CLASSIFIER_PROMPT) + _estimate_tokens(prompt) + (len(batch) * 40)

    async def _request_batch(
        self,
        session: Any,
        batch: list[str],
        title_hints: dict[str, str | None],
        estimated_tokens: int,
    ) -> str:
        formatted_urls: list[str] = []
        for index, url in enumerate(batch, start=1):
            hint = (title_hints.get(url) or "")[: self._max_content_chars]
            if hint:
                formatted_urls.append(f"{index}. URL: {url} | title_hint: {hint}")
            else:
                formatted_urls.append(f"{index}. URL: {url}")

        user_prompt = (
            "Classify every bookmark URL below. Return ONLY JSON with schema:\n"
            '{"results": [{"url": "string", "category": "string", "tags": ["string"], '
            '"summary": "string", "confidence": 0.0}]}\n'
            "Include one result item per URL and copy each URL exactly.\n\n"
            f"URLs:\n{chr(10).join(formatted_urls)}"
        )

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent"
            f"?key={self._api_key}"
        )
        body = {
            "systemInstruction": {"parts": [{"text": CLASSIFIER_PROMPT}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
            },
        }

        max_attempts = self._retry_attempts + 1
        data: dict[str, object] | None = None
        for attempt in range(1, max_attempts + 1):
            wait_start = time.monotonic()
            await self._limiter.wait_for_capacity(estimated_tokens=estimated_tokens)
            waited = time.monotonic() - wait_start
            if waited >= 0.1:
                self._log(f"waited {waited:.2f}s for quota slot before attempt {attempt}/{max_attempts}")
            try:
                async with session.post(url, json=body) as response:
                    response.raise_for_status()
                    data = await response.json()
                break
            except Exception as exc:  # noqa: BLE001
                status = int(getattr(exc, "status", 0) or 0)
                retryable = status == 429 or status == 503 or status >= 500
                retry_after_header = getattr(exc, "headers", {}).get("Retry-After") if hasattr(exc, "headers") else None
                if retryable and attempt < max_attempts:
                    if retry_after_header:
                        try:
                            sleep_seconds = max(0.1, float(str(retry_after_header)))
                        except ValueError:
                            sleep_seconds = self._retry_backoff_seconds * (2 ** (attempt - 1))
                    else:
                        sleep_seconds = self._retry_backoff_seconds * (2 ** (attempt - 1))
                    self._log(
                        f"transient status {status} on attempt {attempt}/{max_attempts}; "
                        f"retrying in {sleep_seconds:.2f}s"
                    )
                    await asyncio.sleep(sleep_seconds)
                    continue

                if status == 0 and attempt < max_attempts:
                    sleep_seconds = self._retry_backoff_seconds * (2 ** (attempt - 1))
                    self._log(
                        f"network error ({type(exc).__name__}) on attempt {attempt}/{max_attempts}; "
                        f"retrying in {sleep_seconds:.2f}s"
                    )
                    await asyncio.sleep(sleep_seconds)
                    continue

                raise

        if data is None:
            raise RuntimeError("Gemini request failed before receiving a response body.")

        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")
        first_candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        content = first_candidate.get("content", {}) if isinstance(first_candidate, dict) else {}
        parts = content.get("parts", []) if isinstance(content, dict) else []
        if not parts:
            raise RuntimeError("Gemini returned empty content parts.")
        first_part = parts[0] if isinstance(parts, list) and parts else {}
        return str(first_part.get("text", "") if isinstance(first_part, dict) else "").strip()

    def _parse_batch_response(
        self,
        response_text: str,
        requested_urls: list[str],
    ) -> dict[str, AiClassification]:
        requested = set(requested_urls)
        payload = json.loads(_strip_code_fence(response_text))
        items = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return {}

        parsed: dict[str, AiClassification] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url or url not in requested:
                continue
            parsed[url] = _normalize_classification(item, self._model)
        return parsed


