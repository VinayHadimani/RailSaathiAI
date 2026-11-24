"""Provider abstractions: internal availability statuses and error classes.

The journey-discovery engine (later phases) will depend on these
abstractions rather than on any concrete provider, so additional railway
providers can be added without touching the search algorithm
(spec sections 4 and 24-11).
"""

from __future__ import annotations

from enum import Enum


class InternalAvailabilityStatus(str, Enum):
    """Internal availability statuses (spec sections 7, 22).

    Mapping rules:
    - Only an explicit provider "AVAILABLE" becomes CONFIRMED_AVAILABLE.
    - RAC / WAITLIST / REGRET / NOT_AVAILABLE / TRAIN_CANCELLED /
      TRAIN_DEPARTED map to their non-confirmed counterparts.
    - Anything unknown or ambiguous maps to UNKNOWN - never confirmed.
    - PROVIDER_ERROR is reserved for API failures and is never evidence
      of unavailability.
    """

    CONFIRMED_AVAILABLE = "CONFIRMED_AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    WAITLIST = "WAITLIST"
    RAC = "RAC"
    UNKNOWN = "UNKNOWN"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class RailwayProviderError(Exception):
    """Base class for railway provider failures.

    Attributes:
        code: stable machine-readable error code.
        category: error family (AUTH, RATE_LIMIT, TIMEOUT, ...).
        retryable: whether a retry with backoff could succeed.
        http_status: provider HTTP status, if a response was received.
        request_id: provider request id (for support), if present.
        details: extra context (never contains credentials).
    """

    category: str = "PROVIDER"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
        retryable: bool | None = None,
        http_status: int | None = None,
        request_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        # Subclasses declare a class-level default; an explicit argument
        # always wins.
        self.retryable = type(self).retryable if retryable is None else retryable
        self.http_status = http_status
        self.request_id = request_id
        self.details = details or {}

    def to_dict(self) -> dict:
        """JSON-safe representation for API responses and logs."""
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "request_id": self.request_id,
            "details": self.details,
        }


class ProviderAuthError(RailwayProviderError):
    """401/403 - invalid or missing API key. Never retried."""

    category = "AUTH"


class ProviderCreditsError(RailwayProviderError):
    """402 - API credits exhausted. Never retried."""

    category = "BILLING"


class ProviderRateLimitError(RailwayProviderError):
    """429 - rate limit exceeded. Retryable (honours Retry-After)."""

    category = "RATE_LIMIT"
    retryable = True


class ProviderTimeoutError(RailwayProviderError):
    """Request timed out. Retryable."""

    category = "TIMEOUT"
    retryable = True


class ProviderTransportError(RailwayProviderError):
    """Network-level failure (connection reset, DNS, ...). Retryable."""

    category = "TRANSPORT"
    retryable = True


class ProviderServerError(RailwayProviderError):
    """5xx from the provider. Retryable with backoff."""

    category = "SERVER"
    retryable = True


class ProviderClientError(RailwayProviderError):
    """4xx (other than auth/credits/rate limit) - bad request or not found."""

    category = "CLIENT"


class ProviderUnexpectedResponseError(RailwayProviderError):
    """2xx response whose body is not the documented shape.

    Treated as a provider protocol failure - never as availability.
    """

    category = "PROTOCOL"


class ProviderNotConfiguredError(RailwayProviderError):
    """The provider credentials are missing from the environment."""

    category = "CONFIG"
