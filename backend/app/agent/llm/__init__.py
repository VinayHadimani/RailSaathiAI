"""RailSaathi AI - Provider-Independent LLM Client Package (Phase 6B.3)."""

from .base import LLMClient
from .exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from .factory import get_llm_client
from .gemini import GeminiLLMClient
from .mock import MockLLMClient

__all__ = [
    "LLMClient",
    "GeminiLLMClient",
    "MockLLMClient",
    "get_llm_client",
    "LLMError",
    "LLMAuthenticationError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMProviderError",
    "LLMInvalidResponseError",
]
