"""Provider-independent exceptions for the RailSaathi AI LLM client layer."""

from __future__ import annotations


class LLMError(Exception):
    """Base exception for all LLM client errors."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        raw_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.raw_error = raw_error

    def __str__(self) -> str:
        parts = [self.message]
        if self.provider:
            parts.append(f"[provider={self.provider}]")
        if self.status_code:
            parts.append(f"[status_code={self.status_code}]")
        return " ".join(parts)


class LLMAuthenticationError(LLMError):
    """Raised when API credentials are missing, invalid, expired, or forbidden (HTTP 401/403)."""


class LLMRateLimitError(LLMError):
    """Raised when LLM quota or rate limit is exceeded (HTTP 429)."""


class LLMTimeoutError(LLMError):
    """Raised when an LLM request times out."""


class LLMProviderError(LLMError):
    """Raised when the upstream LLM service fails (HTTP 5xx, connection error, etc.)."""


class LLMInvalidResponseError(LLMError):
    """Raised when the provider returns an empty, safety-blocked, or malformed response."""
