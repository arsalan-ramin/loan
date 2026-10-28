"""Server-only Anthropic Messages API transport, configuration and safe errors."""

import os

import anthropic

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
MAX_OUTPUT_TOKENS = 8192


class LLMError(RuntimeError):
    """Safe, actionable error; never includes provider bodies, credentials, or prompts."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class AnthropicLLM:
    def __init__(self):
        self.model = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.base_url = os.getenv("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL

    def complete(self, system: str, prompt: str) -> str:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise LLMError("Anthropic is not configured. Set ANTHROPIC_API_KEY in the backend environment and restart the API.", 503)
        try:
            # Endpoint overrides are backend deployment configuration only.
            with anthropic.Anthropic(api_key=key, base_url=self.base_url, timeout=60.0, max_retries=1) as client:
                response = client.messages.create(
                    model=self.model, max_tokens=MAX_OUTPUT_TOKENS, system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
        except anthropic.AuthenticationError:
            raise LLMError("Anthropic authentication failed. Check the backend ANTHROPIC_API_KEY.", 503) from None
        except (anthropic.PermissionDeniedError, anthropic.NotFoundError):
            raise LLMError(f"Anthropic model {self.model} is unavailable to this account. Check model access and ANTHROPIC_MODEL.", 503) from None
        except anthropic.RateLimitError:
            raise LLMError("Anthropic rate limit reached. Retry shortly or check your account limits.", 429) from None
        except anthropic.APITimeoutError:
            raise LLMError("Anthropic timed out. Please retry.", 504) from None
        except anthropic.APIConnectionError:
            raise LLMError("Unable to connect to Anthropic. Check the backend network connection.", 503) from None
        except anthropic.APIStatusError:
            raise LLMError("Anthropic rejected the request. Check model configuration, billing, and service availability.") from None
        text = "\n\n".join(block.text for block in response.content if block.type == "text").strip()
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude reached the {MAX_OUTPUT_TOKENS:,}-token output limit before completing its answer. "
                           "Incomplete commentary was discarded; deterministic results are unchanged. "
                           "Reprocessing may incur another Anthropic charge; retrying is not guaranteed to resolve truncation.")
        if response.stop_reason not in ("end_turn", "stop_sequence"):
            raise LLMError("Anthropic stopped before a normal completion. Commentary was discarded; "
                           "deterministic results are unchanged. Check model availability before retrying.")
        if not text:
            raise LLMError("Anthropic returned no text in its completed response. Deterministic results are unchanged. "
                           "Check model availability before retrying; another request may incur charges.")
        return text