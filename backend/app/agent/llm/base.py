"""Abstract base class for provider-independent LLM clients."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMClient(ABC):
    """Abstract provider-independent LLM client interface.

    Any LLM provider (Gemini, Groq, local models, or test mocks) must
    implement this interface, allowing higher-level agent orchestration
    code to remain completely decoupled from specific vendor SDKs.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the LLM provider (e.g. 'gemini', 'mock')."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Name of the specific model in use (e.g. 'gemini-3.5-flash-lite')."""

    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        """Synchronously generate text from prompt.

        Args:
            prompt: User/agent prompt text.
            system_instruction: Optional system-level guidance.
            temperature: Sampling temperature (0.0 to 2.0).
            max_output_tokens: Maximum tokens in response.

        Returns:
            The generated string response.

        Raises:
            LLMAuthenticationError: Missing or invalid API credentials.
            LLMRateLimitError: Provider rate limit or quota exceeded.
            LLMTimeoutError: Request exceeded time budget.
            LLMProviderError: Upstream provider failure (5xx or connection error).
            LLMInvalidResponseError: Response was empty, safety-filtered, or malformed.
        """

    @abstractmethod
    async def generate_text_async(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        """Asynchronously generate text from prompt.

        Args:
            prompt: User/agent prompt text.
            system_instruction: Optional system-level guidance.
            temperature: Sampling temperature (0.0 to 2.0).
            max_output_tokens: Maximum tokens in response.

        Returns:
            The generated string response.

        Raises:
            LLMAuthenticationError: Missing or invalid API credentials.
            LLMRateLimitError: Provider rate limit or quota exceeded.
            LLMTimeoutError: Request exceeded time budget.
            LLMProviderError: Upstream provider failure (5xx or connection error).
            LLMInvalidResponseError: Response was empty, safety-filtered, or malformed.
        """
