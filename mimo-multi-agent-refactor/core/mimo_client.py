import time
import json
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Callable, Any

logger = logging.getLogger("mimo_client")

# ---------------------------------------------------------------------------
# TokenTracker — hooks into every request to record token consumption
# ---------------------------------------------------------------------------

class TokenTracker:
    """Per-session token usage ledger with optional hook callbacks."""

    def __init__(self, model: str = "mimo-v2.5-pro", daily_budget: int | None = None):
        self.model = model
        self.daily_budget = daily_budget
        self._lock = threading.Lock()
        self._daily_usage: dict[str, dict[str, int]] = {}  # date -> {prompt, completion, total}
        self._request_log: list[dict] = []
        self._hooks: list[Callable] = []

    # -- hook registration ---------------------------------------------------

    def on_track(self, fn: Callable) -> Callable:
        """Register a callback invoked after every token-tracking event.

        Signature: fn(date: str, prompt: int, completion: int, total: int) -> None
        """
        self._hooks.append(fn)
        return fn

    # -- core tracking -------------------------------------------------------

    def track(self, prompt_tokens: int, completion_tokens: int = 0) -> int:
        """Record token usage for the current date. Returns cumulative daily total."""
        date_key = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            day = self._daily_usage.setdefault(date_key, {"prompt": 0, "completion": 0, "total": 0})
            day["prompt"] += prompt_tokens
            day["completion"] += completion_tokens
            day["total"] += prompt_tokens + completion_tokens

            self._request_log.append({
                "ts": datetime.now().isoformat(),
                "prompt": prompt_tokens,
                "completion": completion_tokens,
            })

            total = day["total"]
        self._fire_hooks(date_key, prompt_tokens, completion_tokens, total)
        return total

    def _fire_hooks(self, date_key, prompt, completion, total):
        for hook in self._hooks:
            try:
                hook(date_key, prompt, completion, total)
            except Exception:
                logger.exception("TokenTracker hook raised")

    # -- query helpers -------------------------------------------------------

    def daily_usage(self, date_key: str | None = None) -> dict:
        """Return usage dict for *date_key* (defaults to today)."""
        if date_key is None:
            date_key = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            return dict(self._daily_usage.get(date_key, {"prompt": 0, "completion": 0, "total": 0}))

    @property
    def total_requests(self) -> int:
        with self._lock:
            return len(self._request_log)

    def budget_remaining(self) -> int | None:
        if self.daily_budget is None:
            return None
        return max(0, self.daily_budget - self.daily_usage()["total"])


# ---------------------------------------------------------------------------
# Pre-built hook: structured daily-usage logger
# ---------------------------------------------------------------------------

def daily_usage_logger(log: logging.Logger | None = None) -> Callable:
    """Factory: returns a TokenTracker hook that logs daily usage at WARNING if
    we cross 80 % of a configured budget, and at INFO on every request."""
    log = log or logger

    def hook(date_key: str, prompt: int, completion: int, total: int) -> None:
        pct = ""
        if hasattr(hook, "_budget") and hook._budget:
            pct = f" ({total / hook._budget * 100:.1f}% of budget)"
        log.info(
            "[%s] +%d/%d prompt/completion → %s daily total%s",
            date_key, prompt, completion, f"{total:,}", pct,
        )
        if hasattr(hook, "_budget") and hook._budget and total >= hook._budget * 0.8:
            log.warning("TokenTracker: daily budget at %.0f%% — %s / %s tokens used",
                        total / hook._budget * 100, f"{total:,}", f"{hook._budget:,}")

    hook._budget = None  # set by caller
    return hook


# ---------------------------------------------------------------------------
# Retry helpers for high-concurrency scenarios
# ---------------------------------------------------------------------------

def _backoff_delay(attempt: int, base: float = 2.0, max_delay: float = 120.0, jitter: float = 0.3) -> float:
    """Exponential backoff with jitter, capped at *max_delay* seconds."""
    delay = min(base ** attempt, max_delay)
    delay *= 1 + (jitter * (__import__("random").random() * 2 - 1))
    return delay


ASYNC_RETRYABLE = (
    asyncio.TimeoutError,
    ConnectionError,
    TimeoutError,
    OSError,
)

# ---------------------------------------------------------------------------
# Xiaomi MiMo V2.5 Pro Client
# ---------------------------------------------------------------------------

class MiMoClient:
    """Async HTTP client for the Xiaomi MiMo V2.5 Pro LLM endpoint.

    Designed for 100k+ token payloads from agents like PlannerAgent.  Timeouts are
    set generously (default 300 s connect, 600 s read) to accommodate massive
    context windows.  A built-in ``TokenTracker`` records per-request and daily
    usage, and every call uses exponential-backoff retry for high-concurrency
    resilience.
    """

    # -- endpoint constants --------------------------------------------------

    DEFAULT_BASE_URL = "https://api.xiaomi.com/v1"
    DEFAULT_MODEL = "mimo-v2.5-pro"
    DEFAULT_MAX_TOKENS = 100_000  # MiMo V2.5 Pro supports large contexts

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        connect_timeout: float = 300.0,
        read_timeout: float = 600.0,
        max_retries: int = 5,
        daily_token_budget: int | None = None,
        token_tracker: TokenTracker | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        self.api_key = api_key
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model = model or self.DEFAULT_MODEL
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries

        # Token tracking
        self.token_tracker = token_tracker or TokenTracker(
            model=self.model, daily_budget=daily_token_budget
        )
        if daily_token_budget is not None and self.token_tracker.daily_budget is None:
            self.token_tracker.daily_budget = daily_token_budget

        # Wire the daily-usage logger hook
        _log_hook = daily_usage_logger(logger)
        _log_hook._budget = daily_token_budget
        self.token_tracker.on_track(_log_hook)

        self.extra_headers = extra_headers or {}

    # -- public API ----------------------------------------------------------

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        top_p: float = 0.95,
        stop: list[str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict:
        """Send a chat-completion request and return the parsed JSON response.

        Automatically retries on transient errors with exponential backoff.
        Token usage is recorded through ``self.token_tracker``.
        """
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.DEFAULT_MAX_TOKENS,
            "top_p": top_p,
        }
        if stop:
            payload["stop"] = stop
        if extra_body:
            payload.update(extra_body)

        headers = self._build_headers()
        response = await self._request_with_retry("POST", url, headers, payload)
        return self._process_response(response)

    # -- internal: HTTP ------------------------------------------------------

    async def _request_with_retry(
        self, method: str, url: str, headers: dict, payload: dict | None
    ) -> dict:
        import aiohttp

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(
                    connect=self.connect_timeout,
                    sock_read=self.read_timeout,
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(
                        method, url, headers=headers, json=payload
                    ) as resp:
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After", "5")
                            wait = float(retry_after)
                            logger.warning("Rate-limited (429). Waiting %.1fs", wait)
                            await asyncio.sleep(wait)
                            continue

                        body = await resp.text()
                        if resp.status >= 500:
                            raise _ServerError(resp.status, body)

                        if resp.status >= 400:
                            raise _ClientError(resp.status, body)

                        return json.loads(body)

            except (asyncio.TimeoutError, _ServerError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                delay = _backoff_delay(attempt + 1)
                logger.warning(
                    "Request attempt %d/%d failed (%s). Retrying in %.1fs ...",
                    attempt + 1, self.max_retries, exc, delay,
                )
                await asyncio.sleep(delay)

        raise _RetryExhausted(
            f"All {self.max_retries + 1} attempts failed. Last error: {last_exc}"
        ) from last_exc

    def _build_headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self.extra_headers)
        return h

    def _process_response(self, raw: dict) -> dict:
        usage = raw.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        daily_total = self.token_tracker.track(prompt_tokens, completion_tokens)
        raw["_daily_total_tokens"] = daily_total
        return raw

    # -- convenience sync wrapper --------------------------------------------

    def chat_completion_sync(self, *args: Any, **kwargs: Any) -> dict:
        """Synchronous alias for environments without a running event loop."""
        return asyncio.run(self.chat_completion(*args, **kwargs))


# ---------------------------------------------------------------------------
# Internal exception hierarchy
# ---------------------------------------------------------------------------

class _MiMoError(Exception):
    """Base for all MiMo client errors."""


class _RetryExhausted(_MiMoError):
    """Raised after every retry attempt has been consumed."""


class _ServerError(_MiMoError):
    """5xx from the upstream API."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Server error {status}: {body[:300]}")


class _ClientError(_MiMoError):
    """Non-retryable 4xx (except 429 which is handled separately)."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Client error {status}: {body[:300]}")
