from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp


LOGGER = logging.getLogger(__name__)


class GeminiQuotaExceeded(RuntimeError):
    pass


@dataclass
class GeminiClient:
    api_key: str | None
    model: str = "gemini-2.5-flash"
    timeout_seconds: float = 30.0
    requests_per_minute: int = 6
    tokens_per_minute: int = 0
    requests_per_day: int = 0
    retry_attempts: int = 2
    retry_backoff_seconds: float = 0.7
    _rate_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _next_request_at: float = field(default=0.0, init=False, repr=False)
    _requests_used_today: int = field(default=0, init=False, repr=False)
    _token_window_started_at: float = field(default=0.0, init=False, repr=False)
    _tokens_used_this_minute: int = field(default=0, init=False, repr=False)

    @property
    def request_interval_seconds(self) -> float:
        return 60.0 / max(1, self.requests_per_minute)

    async def summarize(self, *, url: str, title: str, description: str) -> str | None:
        if not self.api_key:
            LOGGER.warning("GEMINI_API_KEY is not set; leaving AI comments blank")
            return None

        prompt = (
            "This is a webpage URL. Based on its content, explain in 1-2 sentences "
            "what this page is used for.\n\n"
            f"URL: {url}\n"
            f"Title: {title or 'N/A'}\n"
            f"Description: {description or 'N/A'}\n\n"
            'If unclear, respond exactly: "Unknown purpose"'
        )
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        estimated_tokens = max(1, len(prompt) // 4)

        total_attempts = max(1, self.retry_attempts + 1)
        for attempt in range(total_attempts):
            try:
                await self._wait_for_quota(estimated_tokens=estimated_tokens)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(endpoint, json=payload) as response:
                        text = await response.text()
                        if response.status >= 500 or response.status == 429:
                            raise RuntimeError(f"Gemini transient HTTP {response.status}: {text[:300]}")
                        if response.status >= 400:
                            raise RuntimeError(f"Gemini HTTP {response.status}: {text[:300]}")
                        data = await response.json()
                        summary = _extract_text(data)
                        return summary or "Unknown purpose"
            except GeminiQuotaExceeded as exc:
                LOGGER.warning("Gemini quota limit reached for %s: %s", url, exc)
                return None
            except Exception as exc:  # noqa: BLE001
                if attempt == total_attempts - 1:
                    LOGGER.warning("Gemini summary failed for %s: %s: %s", url, type(exc).__name__, exc)
                    return None
                backoff = self.retry_backoff_seconds * (2**attempt)
                LOGGER.debug("Gemini retry %s/%s for %s in %.2fs", attempt + 1, self.retry_attempts, url, backoff)
                await asyncio.sleep(backoff)

        return "Unknown purpose"

    async def _wait_for_rate_limit(self) -> None:
        await self._wait_for_quota(estimated_tokens=1)

    async def _wait_for_quota(self, *, estimated_tokens: int) -> None:
        while True:
            async with self._rate_lock:
                now = time.monotonic()
                if self._token_window_started_at <= 0:
                    self._token_window_started_at = now
                if now - self._token_window_started_at >= 60:
                    self._token_window_started_at = now
                    self._tokens_used_this_minute = 0

                if self.requests_per_day > 0 and self._requests_used_today >= self.requests_per_day:
                    raise GeminiQuotaExceeded(f"BA_GEMINI_RPD={self.requests_per_day}")

                wait_seconds = max(0.0, self._next_request_at - now)
                if (
                    self.tokens_per_minute > 0
                    and self._tokens_used_this_minute + estimated_tokens > self.tokens_per_minute
                ):
                    wait_seconds = max(wait_seconds, 60 - (now - self._token_window_started_at))

                if wait_seconds <= 0:
                    self._next_request_at = now + self.request_interval_seconds
                    self._requests_used_today += 1
                    self._tokens_used_this_minute += estimated_tokens
                    return

            LOGGER.debug("Waiting %.2fs for Gemini quota limits", wait_seconds)
            await asyncio.sleep(wait_seconds)


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text_parts = [str(part.get("text", "")).strip() for part in parts if isinstance(part, dict)]
    return " ".join(part for part in text_parts if part).strip()
