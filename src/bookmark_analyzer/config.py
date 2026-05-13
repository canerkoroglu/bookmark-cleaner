from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class Settings:
    """Runtime settings for the bookmark analyzer."""

    openai_api_key: str | None
    openai_model: str
    ai_provider: str
    gemini_api_key: str | None
    gemini_model: str
    gemini_requests_per_minute: int
    gemini_tokens_per_minute: int
    gemini_requests_per_day: int
    gemini_batch_target_size: int
    ai_live_logs: bool
    ai_confidence_threshold: float
    category_rules_file: str | None
    request_timeout_seconds: float
    browser_timeout_ms: int
    concurrency: int
    user_agent: str
    max_content_chars: int
    retry_attempts: int
    retry_backoff_seconds: float
    domain_min_interval_seconds: float
    resume_hours: float
    history_db_path: str

    @classmethod
    def from_env(cls, concurrency_override: int | None = None) -> "Settings":
        concurrency = concurrency_override or int(os.getenv("BA_CONCURRENCY", "20"))
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            ai_provider=os.getenv("AI_PROVIDER", "openai").strip().lower(),
            gemini_api_key=(
                os.getenv("GEMINI_API_KEY")
                or os.getenv("GOOGLE_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            ),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            gemini_requests_per_minute=int(os.getenv("BA_GEMINI_RPM", "5")),
            gemini_tokens_per_minute=int(os.getenv("BA_GEMINI_TPM", "250000")),
            gemini_requests_per_day=int(os.getenv("BA_GEMINI_RPD", "20")),
            gemini_batch_target_size=int(os.getenv("BA_GEMINI_BATCH_TARGET_SIZE", "150")),
            ai_live_logs=os.getenv("BA_AI_LIVE_LOGS", "0").strip().lower() in {"1", "true", "yes", "on"},
            ai_confidence_threshold=float(os.getenv("BA_AI_CONFIDENCE_THRESHOLD", "0")),
            category_rules_file=os.getenv("BA_CATEGORY_RULES_FILE"),
            request_timeout_seconds=float(os.getenv("BA_REQUEST_TIMEOUT_SECONDS", "15")),
            browser_timeout_ms=int(os.getenv("BA_BROWSER_TIMEOUT_MS", "20000")),
            concurrency=concurrency,
            user_agent=os.getenv(
                "BA_USER_AGENT",
                "BookmarkAnalyzer/1.0 (+https://local.dev/bookmark-analyzer)",
            ),
            max_content_chars=int(os.getenv("BA_MAX_CONTENT_CHARS", "5000")),
            retry_attempts=int(os.getenv("BA_RETRY_ATTEMPTS", "2")),
            retry_backoff_seconds=float(os.getenv("BA_RETRY_BACKOFF_SECONDS", "0.7")),
            domain_min_interval_seconds=float(os.getenv("BA_DOMAIN_MIN_INTERVAL_SECONDS", "0.2")),
            resume_hours=float(os.getenv("BA_RESUME_HOURS", "0")),
            history_db_path=os.getenv("BA_HISTORY_DB_PATH", ".bookmark_analyzer.db"),
        )
