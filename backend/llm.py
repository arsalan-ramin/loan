"""Server-only Anthropic Messages API transport, configuration and safe errors."""

import logging
import os
import time
import uuid

import anthropic

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
MAX_OUTPUT_TOKENS = 8192
LOGGER = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Safe, actionable error; never includes provider bodies, credentials, or prompts."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class AnthropicLLM:
    def __init__(self):
        self.model = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.base_url = os.getenv("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        try:
            self.max_retries = max(0, min(5, int(os.getenv("ANTHROPIC_MAX_RETRIES", "2"))))
        except ValueError:
            self.max_retries = 2

    def complete(self, system: str, prompt: str) -> str:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        attempts = 0
        retry_exhausted = False
        response_chars = 0
        stop_category = "not_received"
        token_counts: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}
        LOGGER.info("llm_invocation_started request_id=%s", request_id)

        def log_terminal(event: str, category: str, http_status: int | None = None) -> None:
            # Callers supply only fixed categories/statuses, never provider details.
            LOGGER.log(
                logging.WARNING if event == "failed" else logging.INFO,
                "llm_invocation_%s request_id=%s duration_ms=%.3f category=%s "
                "http_status=%s response_chars=%d stop_category=%s input_tokens=%s output_tokens=%s "
                "attempts=%d retry_exhausted=%s",
                event, request_id, (time.perf_counter() - started) * 1000,
                category, http_status, response_chars, stop_category,
                token_counts["input_tokens"], token_counts["output_tokens"],
                attempts, retry_exhausted,
            )

        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            log_terminal("failed", "not_configured", 503)
            raise LLMError("Anthropic is not configured. Set ANTHROPIC_API_KEY in the backend environment and restart the API.", 503)
        try:
            # Endpoint overrides are backend deployment configuration only.
            # Only this loop owns retries; SDK retries would multiply the budget.
            with anthropic.Anthropic(api_key=key, base_url=self.base_url, timeout=60.0, max_retries=0) as client:
                while True:
                    attempts += 1
                    try:
                        response = client.messages.create(
                            model=self.model, max_tokens=MAX_OUTPUT_TOKENS, system=system,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        break
                    except (anthropic.APIConnectionError, anthropic.APIStatusError) as error:
                        if isinstance(error, anthropic.APITimeoutError):
                            category = "timeout"
                        elif isinstance(error, anthropic.APIConnectionError):
                            category = "connection"
                        elif isinstance(error, anthropic.RateLimitError):
                            category = "rate_limit"
                        elif (not isinstance(error, (anthropic.AuthenticationError,
                                                    anthropic.PermissionDeniedError, anthropic.NotFoundError))
                              and (error.status_code in (408, 409) or error.status_code >= 500)):
                            category = "api_status"
                        else:
                            raise
                        if attempts > self.max_retries:
                            retry_exhausted = True
                            raise
                        delay = min(0.5 * (2 ** (attempts - 1)), 4.0)
                        if isinstance(error, anthropic.APIStatusError):
                            # Optional numeric Retry-After seconds: ignore malformed,
                            # negative and nonfinite values; never log raw headers.
                            try:
                                retry_after = float(error.response.headers.get("retry-after", ""))
                            except (TypeError, ValueError, OverflowError):
                                pass
                            else:
                                if 0 <= retry_after < float("inf"):
                                    delay = min(retry_after, 30.0)
                        LOGGER.warning(
                            "llm_invocation_retry request_id=%s attempt=%d category=%s delay_seconds=%.3f",
                            request_id, attempts, category, delay,
                        )
                        time.sleep(delay)
        except anthropic.AuthenticationError:
            log_terminal("failed", "authentication", 503)
            raise LLMError("Anthropic authentication failed. Check the backend ANTHROPIC_API_KEY.", 503) from None
        except (anthropic.PermissionDeniedError, anthropic.NotFoundError):
            log_terminal("failed", "model_unavailable", 503)
            raise LLMError(f"Anthropic model {self.model} is unavailable to this account. Check model access and ANTHROPIC_MODEL.", 503) from None
        except anthropic.RateLimitError:
            log_terminal("failed", "rate_limit", 429)
            raise LLMError("Anthropic rate limit reached. Retry shortly or check your account limits.", 429) from None
        except anthropic.APITimeoutError:
            log_terminal("failed", "timeout", 504)
            raise LLMError("Anthropic timed out. Please retry.", 504) from None
        except anthropic.APIConnectionError:
            log_terminal("failed", "connection", 503)
            raise LLMError("Unable to connect to Anthropic. Check the backend network connection.", 503) from None
        except anthropic.APIStatusError:
            log_terminal("failed", "api_status", 502)
            raise LLMError("Anthropic rejected the request. Check model configuration, billing, and service availability.") from None
        text = "\n\n".join(block.text for block in response.content if block.type == "text").strip()
        response_chars = len(text)
        stop_category = (response.stop_reason if response.stop_reason in
                         ("end_turn", "stop_sequence", "max_tokens") else "other")
        usage = getattr(response, "usage", None)
        for name in token_counts:
            value = getattr(usage, name, None)
            # bools, strings, mocks and other provider values are not token counts.
            if type(value) is int and value >= 0:
                token_counts[name] = value
        if response.stop_reason == "max_tokens":
            log_terminal("failed", "truncation", 502)
            raise LLMError(f"Claude reached the {MAX_OUTPUT_TOKENS:,}-token output limit before completing its answer. "
                           "Incomplete commentary was discarded; deterministic results are unchanged. "
                           "Reprocessing may incur another Anthropic charge; retrying is not guaranteed to resolve truncation.")
        if response.stop_reason not in ("end_turn", "stop_sequence"):
            log_terminal("failed", "abnormal_stop", 502)
            raise LLMError("Anthropic stopped before a normal completion. Commentary was discarded; "
                           "deterministic results are unchanged. Check model availability before retrying.")
        if not text:
            log_terminal("failed", "empty_response", 502)
            raise LLMError("Anthropic returned no text in its completed response. Deterministic results are unchanged. "
                           "Check model availability before retrying; another request may incur charges.")
        log_terminal("completed", "success")
        return text