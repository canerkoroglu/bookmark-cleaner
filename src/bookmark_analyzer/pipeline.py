from __future__ import annotations

from collections.abc import Callable

from .ai_classifier import GeminiClassifier, OpenAIClassifier
from .category_rules import CategoryRules, load_category_rules
from .browser_checker import check_url_jobs_with_playwright, check_urls_with_playwright
from .config import Settings
from .dedupe import deduplicate_by_domain
from .http_checker import check_url_jobs_async, check_urls_async
from .models import AiClassification, BookmarkReport, HttpCheckResult
from .utils import extract_domain


class BookmarkAnalyzer:
    def __init__(
        self,
        settings: Settings,
        enable_playwright_fallback: bool = True,
        enable_ai_classification: bool = True,
        deduplicate_by_domain: bool = True,
    ) -> None:
        self.settings = settings
        self.enable_playwright_fallback = enable_playwright_fallback
        self.enable_ai_classification = enable_ai_classification
        self.deduplicate_by_domain = deduplicate_by_domain

    async def analyze(
        self,
        urls: list[str],
        cached_results_by_url: dict[str, HttpCheckResult] | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> tuple[list[BookmarkReport], int, int]:
        category_rules = load_category_rules(self.settings.category_rules_file)
        min_confidence = max(0.0, min(1.0, self.settings.ai_confidence_threshold))
        cached_results_by_url = cached_results_by_url or {}
        dedupe = deduplicate_by_domain(urls) if self.deduplicate_by_domain else None
        check_targets = dedupe.unique_urls if dedupe is not None else list(dict.fromkeys(urls))
        cached_targets = {url for url in check_targets if url in cached_results_by_url}
        pending_targets = [url for url in check_targets if url not in cached_targets]
        http_results: dict[str, HttpCheckResult] = {
            url: cached_results_by_url[url] for url in cached_targets
        }

        if pending_targets:
            fresh_results = await check_urls_async(
                urls=pending_targets,
                timeout_seconds=self.settings.request_timeout_seconds,
                concurrency=self.settings.concurrency,
                user_agent=self.settings.user_agent,
                retry_attempts=self.settings.retry_attempts,
                retry_backoff_seconds=self.settings.retry_backoff_seconds,
                domain_min_interval_seconds=self.settings.domain_min_interval_seconds,
                progress_callback=(
                    (lambda done, total: progress_callback("clean-http", done, total))
                    if progress_callback
                    else None
                ),
            )
            http_results.update(fresh_results)

        fallback_candidates = [
            url
            for url, result in http_results.items()
            if not result.ok and url in pending_targets
        ]

        if self.enable_playwright_fallback and fallback_candidates:
            try:
                browser_results = await check_urls_with_playwright(
                    urls=fallback_candidates,
                    timeout_ms=self.settings.browser_timeout_ms,
                    concurrency=max(2, min(self.settings.concurrency, 8)),
                    user_agent=self.settings.user_agent,
                    retry_attempts=max(1, self.settings.retry_attempts // 2),
                    retry_backoff_seconds=self.settings.retry_backoff_seconds,
                    domain_min_interval_seconds=self.settings.domain_min_interval_seconds,
                    progress_callback=(
                        (lambda done, total: progress_callback("clean-browser-fallback", done, total))
                        if progress_callback
                        else None
                    ),
                )
                for url, browser_result in browser_results.items():
                    if browser_result.ok or http_results[url].status_code is None:
                        http_results[url] = browser_result
            except Exception:  # noqa: BLE001
                # Keep HTTP results when browser fallback cannot be started.
                pass

        classifications = {}
        if self.enable_ai_classification:
            classifier = None
            if self.settings.ai_provider == "gemini" and self.settings.gemini_api_key:
                classifier = GeminiClassifier(
                    api_key=self.settings.gemini_api_key,
                    model=self.settings.gemini_model,
                    max_content_chars=self.settings.max_content_chars,
                    timeout_seconds=self.settings.request_timeout_seconds,
                    requests_per_minute=self.settings.gemini_requests_per_minute,
                    tokens_per_minute=self.settings.gemini_tokens_per_minute,
                    requests_per_day=self.settings.gemini_requests_per_day,
                    batch_target_size=self.settings.gemini_batch_target_size,
                    enable_live_logs=self.settings.ai_live_logs,
                    retry_attempts=self.settings.retry_attempts,
                    retry_backoff_seconds=self.settings.retry_backoff_seconds,
                )
            elif self.settings.openai_api_key:
                classifier = OpenAIClassifier(
                    api_key=self.settings.openai_api_key,
                    model=self.settings.openai_model,
                    max_content_chars=self.settings.max_content_chars,
                )

            if classifier is not None:
                try:
                    classifications = await classifier.classify_many(
                        urls=pending_targets,
                        concurrency=max(2, min(self.settings.concurrency, 10)),
                        progress_callback=(
                            (lambda done, total: progress_callback("ai-classification", done, total))
                            if progress_callback
                            else None
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    if self.settings.ai_live_logs:
                        print(f"[ai] classification skipped due to error: {exc}", flush=True)

        reports: list[BookmarkReport] = []
        for canonical in check_targets:
            duplicates = dedupe.duplicates_by_canonical.get(canonical, []) if dedupe else []
            check = http_results.get(canonical)
            if check is None:
                check = HttpCheckResult(
                    url=canonical,
                    ok=False,
                    status_code=None,
                    final_url=None,
                    error="No check result generated.",
                )

            reports.append(
                BookmarkReport(
                    url=canonical,
                    domain=extract_domain(canonical),
                    deduplicated=bool(duplicates),
                    duplicate_urls=duplicates,
                    check=check,
                    classification=self._apply_ai_post_processing(
                        url=canonical,
                        classification=classifications.get(canonical),
                        min_confidence=min_confidence,
                        category_rules=category_rules,
                    ),
                )
            )

        return reports, len(pending_targets), len(cached_targets)

    def _apply_ai_post_processing(
        self,
        url: str,
        classification: AiClassification | None,
        min_confidence: float,
        category_rules: CategoryRules | None,
    ) -> AiClassification | None:
        if classification is None:
            rule_category = category_rules.category_for_url(url) if category_rules else None
            if rule_category:
                resolved_category = str(rule_category)
                return AiClassification(
                    category=resolved_category,
                    tags=[],
                    summary="",
                    confidence=1.0,
                    model="rules",
                )
            return None

        if classification.confidence < min_confidence:
            return None

        rule_category = category_rules.category_for_url(url) if category_rules else None
        if rule_category and rule_category != classification.category:
            resolved_category = str(rule_category)
            return AiClassification(
                category=resolved_category,
                tags=classification.tags,
                summary=classification.summary,
                confidence=classification.confidence,
                model=f"{classification.model}+rules",
            )
        return classification

    async def analyze_url_jobs(
        self,
        jobs: list[tuple[str, str]],
        cached_results_by_url: dict[str, HttpCheckResult] | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> tuple[dict[str, HttpCheckResult], int, int]:
        cached_results_by_url = cached_results_by_url or {}
        pending_jobs = [(job_id, url) for job_id, url in jobs if url not in cached_results_by_url]
        cached_jobs = [(job_id, url) for job_id, url in jobs if url in cached_results_by_url]
        http_results: dict[str, HttpCheckResult] = {
            job_id: cached_results_by_url[url] for job_id, url in cached_jobs
        }

        if pending_jobs:
            fresh_results = await check_url_jobs_async(
                jobs=pending_jobs,
                timeout_seconds=self.settings.request_timeout_seconds,
                concurrency=self.settings.concurrency,
                user_agent=self.settings.user_agent,
                retry_attempts=self.settings.retry_attempts,
                retry_backoff_seconds=self.settings.retry_backoff_seconds,
                domain_min_interval_seconds=self.settings.domain_min_interval_seconds,
                progress_callback=(
                    (lambda done, total: progress_callback("clean-http", done, total))
                    if progress_callback
                    else None
                ),
            )
            http_results.update(fresh_results)

        fallback_jobs = [
            (job_id, url)
            for (job_id, url) in pending_jobs
            if (
                job_id in http_results
                and not http_results[job_id].ok
            )
        ]

        if self.enable_playwright_fallback and fallback_jobs:
            try:
                browser_results = await check_url_jobs_with_playwright(
                    jobs=fallback_jobs,
                    timeout_ms=self.settings.browser_timeout_ms,
                    concurrency=max(2, min(self.settings.concurrency, 8)),
                    user_agent=self.settings.user_agent,
                    retry_attempts=max(1, self.settings.retry_attempts // 2),
                    retry_backoff_seconds=self.settings.retry_backoff_seconds,
                    domain_min_interval_seconds=self.settings.domain_min_interval_seconds,
                    progress_callback=(
                        (lambda done, total: progress_callback("clean-browser-fallback", done, total))
                        if progress_callback
                        else None
                    ),
                )
                for job_id, browser_result in browser_results.items():
                    if (
                        job_id in http_results
                        and (browser_result.ok or http_results[job_id].status_code is None)
                    ):
                        http_results[job_id] = browser_result
            except Exception:  # noqa: BLE001
                pass

        return http_results, len(pending_jobs), len(cached_jobs)
