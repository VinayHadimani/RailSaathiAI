"""Deterministic Mock LLM client for testing and offline development."""

from __future__ import annotations

import asyncio
from typing import Any

from .base import LLMClient
from .exceptions import LLMError


class MockLLMClient(LLMClient):
    """Deterministic, offline LLM client for tests and local development.

    Supports:
    - Pre-queued responses returned in order.
    - Pattern-based response mapping (matching substring in prompt).
    - Configured error simulation (raising specific LLMError instances).
    - Detailed call history inspection for test assertions.
    """

    def __init__(
        self,
        *,
        default_response: str = "Mock LLM response",
        responses: list[str] | None = None,
        pattern_responses: dict[str, str] | None = None,
        error_to_raise: Exception | None = None,
        errors: list[Exception] | None = None,
        model_name: str = "mock-model",
    ) -> None:
        self._default_response = default_response
        self._responses: list[str] = list(responses) if responses else []
        self._pattern_responses: dict[str, str] = dict(pattern_responses) if pattern_responses else {}
        self._error_to_raise = error_to_raise
        self._errors: list[Exception] = list(errors) if errors else []
        self._model_name = model_name
        self._history: list[dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def history(self) -> list[dict[str, Any]]:
        """List of all calls made to this client."""
        return self._history

    @property
    def call_count(self) -> int:
        return len(self._history)

    def queue_response(self, text: str) -> None:
        """Enqueue an additional response for subsequent calls."""
        self._responses.append(text)

    def queue_error(self, exc: Exception) -> None:
        """Enqueue an exception to be raised on subsequent calls."""
        self._errors.append(exc)

    def clear_history(self) -> None:
        """Clear recorded call history."""
        self._history.clear()

    def _resolve_response(
        self,
        prompt: str,
        system_instruction: str | None,
        temperature: float | None,
        max_output_tokens: int | None,
    ) -> str:
        call_record = {
            "prompt": prompt,
            "system_instruction": system_instruction,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        self._history.append(call_record)

        # Check queued errors
        if self._errors:
            raise self._errors.pop(0)
        if self._error_to_raise is not None:
            raise self._error_to_raise

        # Check queued responses
        if self._responses:
            return self._responses.pop(0)

        # Check pattern matches (case-insensitive)
        prompt_lower = prompt.lower()
        if "new user message:" in prompt_lower:
            user_msg_part = prompt_lower.split("new user message:")[-1].split("extract parameters as json:")[0]
            for pattern, response in self._pattern_responses.items():
                if pattern.lower() in user_msg_part:
                    return response

        for pattern, response in self._pattern_responses.items():
            if pattern.lower() in prompt_lower:
                return response

        return self._default_response

    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        return self._resolve_response(
            prompt=prompt,
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

    async def generate_text_async(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        await asyncio.sleep(0)  # yield control
        return self._resolve_response(
            prompt=prompt,
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
