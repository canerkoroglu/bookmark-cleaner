from __future__ import annotations

import asyncio
import time
from collections import deque

from .utils import extract_domain


class DomainRateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval_seconds = max(0.0, min_interval_seconds)
        self._next_allowed_by_domain: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait_for_slot(self, url: str) -> None:
        if self._min_interval_seconds <= 0:
            return

        domain = extract_domain(url)
        if not domain:
            return

        async with self._lock:
            now = time.monotonic()
            next_allowed = self._next_allowed_by_domain.get(domain, now)
            wait_seconds = max(0.0, next_allowed - now)
            self._next_allowed_by_domain[domain] = max(now, next_allowed) + self._min_interval_seconds

        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)


class ApiQuotaLimiter:
    """Async limiter for API request/token quotas across minute/day windows."""

    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        requests_per_day: int,
    ) -> None:
        self._requests_per_minute = max(1, requests_per_minute)
        self._tokens_per_minute = max(1, tokens_per_minute)
        self._requests_per_day = max(1, requests_per_day)
        self._minute_request_events: deque[float] = deque()
        self._day_request_events: deque[float] = deque()
        self._minute_token_events: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    async def wait_for_capacity(self, estimated_tokens: int, requests: int = 1) -> None:
        token_budget = max(1, min(estimated_tokens, self._tokens_per_minute))
        if requests < 1:
            requests = 1

        while True:
            wait_seconds = 0.0
            async with self._lock:
                now = time.monotonic()
                self._prune(now)

                if len(self._day_request_events) + requests > self._requests_per_day:
                    raise RuntimeError(
                        "Daily Gemini request quota exhausted. "
                        f"Limit={self._requests_per_day} requests/day."
                    )

                minute_requests_ok = len(self._minute_request_events) + requests <= self._requests_per_minute
                minute_tokens_ok = self._minute_tokens_used() + token_budget <= self._tokens_per_minute

                if minute_requests_ok and minute_tokens_ok:
                    for _ in range(requests):
                        self._minute_request_events.append(now)
                        self._day_request_events.append(now)
                    self._minute_token_events.append((now, token_budget))
                    return

                req_wait = 0.0
                tok_wait = 0.0
                if not minute_requests_ok and self._minute_request_events:
                    req_wait = max(0.0, (self._minute_request_events[0] + 60.0) - now)
                if not minute_tokens_ok and self._minute_token_events:
                    tok_wait = max(0.0, (self._minute_token_events[0][0] + 60.0) - now)
                wait_seconds = max(req_wait, tok_wait, 0.05)

            await asyncio.sleep(wait_seconds)

    def _minute_tokens_used(self) -> int:
        return sum(tokens for _, tokens in self._minute_token_events)

    def _prune(self, now: float) -> None:
        minute_cutoff = now - 60.0
        day_cutoff = now - 86400.0

        while self._minute_request_events and self._minute_request_events[0] <= minute_cutoff:
            self._minute_request_events.popleft()
        while self._day_request_events and self._day_request_events[0] <= day_cutoff:
            self._day_request_events.popleft()
        while self._minute_token_events and self._minute_token_events[0][0] <= minute_cutoff:
            self._minute_token_events.popleft()

