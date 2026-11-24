"""Gemini LLM client implementation using Google GenAI SDK."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from pydantic import SecretStr

from ...core.key_pool import APIKeyPool, mask_key
from .base import LLMClient
from .exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)


class GeminiLLMClient(LLMClient):
    """Google Gemini LLM client using official google-genai SDK.

    Maps provider SDK exceptions to provider-neutral LLM exceptions,
    protects API credentials from logging or leaking, and handles
    both synchronous and asynchronous generation.
    """

    DEFAULT_MODEL = "gemini-2.5-flash"
    DEFAULT_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        api_key: SecretStr | str | None = None,
        *,
        api_keys: Sequence[SecretStr | str] | None = None,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._timeout_seconds = float(timeout_seconds)

        if client is not None:
            self._client = client
            self._key_pool = APIKeyPool(["injected-client"], name="Gemini")
            if api_key is not None:
                key_value = api_key.get_secret_value() if isinstance(api_key, SecretStr) else str(api_key).strip()
                self._api_key = SecretStr(key_value)
            else:
                self._api_key = SecretStr("injected-client")
            self._clients = {}
            return

        if api_key is not None and not api_keys:
            raw_val = api_key.get_secret_value() if isinstance(api_key, SecretStr) else str(api_key)
            if not raw_val.strip():
                raise LLMAuthenticationError(
                    "Gemini API key is empty.",
                    provider="gemini",
                )

        all_keys: list[str] = []
        if api_keys:
            for k in api_keys:
                v = k.get_secret_value() if isinstance(k, SecretStr) else str(k).strip()
                if v:
                    all_keys.append(v)
        if api_key is not None:
            v = api_key.get_secret_value() if isinstance(api_key, SecretStr) else str(api_key).strip()
            if v:
                for part in v.split(","):
                    part_clean = part.strip()
                    if part_clean and part_clean not in all_keys:
                        all_keys.append(part_clean)

        self._key_pool = APIKeyPool(all_keys, name="Gemini")
        if not self._key_pool.is_configured:
            raise LLMAuthenticationError(
                "Gemini API key is not configured. Set GEMINI_API_KEY environment variable.",
                provider="gemini",
            )

        self._api_key = SecretStr(self._key_pool.keys[0])
        self._client = None
        self._clients: dict[str, Any] = {}

    @property
    def key_pool(self) -> APIKeyPool:
        return self._key_pool

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model

    def __repr__(self) -> str:
        return f"<GeminiLLMClient model={self._model} keys={self._key_pool.total_count} timeout={self._timeout_seconds}s>"

    def __str__(self) -> str:
        return f"GeminiLLMClient(model='{self._model}', keys={self._key_pool.total_count})"

    def _get_client(self, key: str | None = None) -> Any:
        """Lazily initialize the google.genai.Client for the specified key."""
        if self._client is not None:
            return self._client

        active_key = key or (self._key_pool.keys[0] if self._key_pool.keys else None)
        if not active_key:
            raise LLMAuthenticationError("No Gemini API key available.", provider="gemini")

        if active_key in self._clients:
            return self._clients[active_key]

        try:
            from google import genai
            from google.genai import types

            timeout_ms = int(self._timeout_seconds * 1000)
            client = genai.Client(
                api_key=active_key,
                http_options=types.HttpOptions(api_version="v1beta", timeout=timeout_ms),
            )
            self._clients[active_key] = client
            return client
        except Exception as exc:
            logger.error("Failed to initialize Google GenAI client for key %s: %s", mask_key(active_key), type(exc).__name__)
            raise LLMProviderError(
                f"Failed to initialize Gemini client: {exc}",
                provider="gemini",
                raw_error=exc,
            ) from exc

    def _build_config(
        self,
        system_instruction: str | None,
        temperature: float | None,
        max_output_tokens: int | None,
    ) -> Any:
        from google.genai import types

        timeout_ms = int(self._timeout_seconds * 1000)
        config_args: dict[str, Any] = {
            "http_options": types.HttpOptions(api_version="v1beta", timeout=timeout_ms),
        }
        if system_instruction is not None:
            config_args["system_instruction"] = system_instruction
        if temperature is not None:
            config_args["temperature"] = temperature
        if max_output_tokens is not None:
            config_args["max_output_tokens"] = max_output_tokens

        return types.GenerateContentConfig(**config_args)

    def _validate_response_text(self, response: Any) -> str:
        """Validate and extract text from GenerateContentResponse."""
        if response is None:
            raise LLMInvalidResponseError(
                "Gemini returned None response.",
                provider="gemini",
            )

        text = getattr(response, "text", None)
        if text is None or not str(text).strip():
            raise LLMInvalidResponseError(
                "Gemini returned an empty response (text is blank or safety filter triggered).",
                provider="gemini",
            )

        return str(text).strip()

    def _map_exception(self, exc: Exception, current_key: str | None = None) -> LLMError:
        """Translate Google GenAI SDK and HTTP exceptions to LLM exceptions."""
        if isinstance(exc, LLMError):
            return exc

        exc_name = type(exc).__name__
        exc_str = str(exc)

        # Sanitize any accidental appearance of any API key in the error string
        keys_to_redact = list(self._key_pool.keys)
        if current_key:
            keys_to_redact.append(current_key)
        if hasattr(self, "_api_key"):
            keys_to_redact.append(self._api_key.get_secret_value())

        for k in keys_to_redact:
            if k and k != "injected-client" and k in exc_str:
                exc_str = exc_str.replace(k, "[REDACTED]")

        # Check for google.genai.errors.APIError and subclasses
        status_code: int | None = None
        if hasattr(exc, "code"):
            try:
                status_code = int(getattr(exc, "code"))
            except (ValueError, TypeError):
                status_code = None

        if status_code in (401, 403) or "unauthenticated" in exc_str.lower() or "api_key" in exc_str.lower():
            return LLMAuthenticationError(
                f"Gemini authentication failed: {exc_str}",
                provider="gemini",
                status_code=status_code or 401,
                raw_error=exc,
            )

        if (
            status_code in (429, 503)
            or "resource_exhausted" in exc_str.lower()
            or "quota" in exc_str.lower()
            or "high demand" in exc_str.lower()
        ):
            return LLMRateLimitError(
                f"Gemini rate limit or quota exceeded: {exc_str}",
                provider="gemini",
                status_code=status_code or 429,
                raw_error=exc,
            )

        if (
            status_code in (408, 504)
            or "timeout" in exc_name.lower()
            or "timed out" in exc_str.lower()
            or isinstance(exc, TimeoutError)
        ):
            return LLMTimeoutError(
                f"Gemini request timed out after {self._timeout_seconds}s: {exc_str}",
                provider="gemini",
                status_code=status_code or 504,
                raw_error=exc,
            )

        if status_code and status_code >= 500:
            return LLMProviderError(
                f"Gemini upstream server error ({status_code}): {exc_str}",
                provider="gemini",
                status_code=status_code,
                raw_error=exc,
            )

        return LLMProviderError(
            f"Gemini request failed: {exc_name}: {exc_str}",
            provider="gemini",
            status_code=status_code,
            raw_error=exc,
        )

    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        candidates = self._key_pool.get_candidates()
        if not candidates:
            raise LLMAuthenticationError("Gemini API key is not configured.", provider="gemini")

        models_to_try = [self._model]
        if "3.1-flash-lite" not in self._model:
            models_to_try.append("gemini-3.1-flash-lite")

        last_exc: LLMError | None = None
        for key_idx, current_key in enumerate(candidates):
            client = self._get_client(current_key)
            config = self._build_config(
                system_instruction=system_instruction,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )

            for model_name in models_to_try:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config,
                    )
                    text = self._validate_response_text(response)
                    self._key_pool.advance()
                    return text
                except Exception as exc:
                    exc_str = str(exc).lower()
                    if "404" in exc_str and "no longer available" in exc_str and model_name != models_to_try[-1]:
                        logger.info(
                            "Model %s not available for key %s, falling back to %s...",
                            model_name,
                            mask_key(current_key),
                            models_to_try[-1],
                        )
                        continue

                    mapped_exc = self._map_exception(exc, current_key)
                    if isinstance(mapped_exc, LLMRateLimitError):
                        self._key_pool.mark_rate_limited(current_key)
                        if key_idx + 1 < len(candidates):
                            logger.warning(
                                "Gemini key %s rate-limited / high demand. Failing over to next key (%d remaining)...",
                                mask_key(current_key),
                                len(candidates) - (key_idx + 1),
                            )
                            last_exc = mapped_exc
                            break
                    logger.warning("Gemini generation failed: %s", mapped_exc)
                    raise mapped_exc from exc

        if last_exc:
            raise last_exc
        raise LLMProviderError("Gemini request failed: no responses returned", provider="gemini")

    async def generate_text_async(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        candidates = self._key_pool.get_candidates()
        if not candidates:
            raise LLMAuthenticationError("Gemini API key is not configured.", provider="gemini")

        models_to_try = [self._model]
        if "3.1-flash-lite" not in self._model:
            models_to_try.append("gemini-3.1-flash-lite")

        last_exc: LLMError | None = None
        for key_idx, current_key in enumerate(candidates):
            client = self._get_client(current_key)
            config = self._build_config(
                system_instruction=system_instruction,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )

            for model_name in models_to_try:
                try:
                    response = await client.aio.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config,
                    )
                    text = self._validate_response_text(response)
                    self._key_pool.advance()
                    return text
                except Exception as exc:
                    exc_str = str(exc).lower()
                    if "404" in exc_str and "no longer available" in exc_str and model_name != models_to_try[-1]:
                        logger.info(
                            "Model %s not available for key %s, falling back to %s...",
                            model_name,
                            mask_key(current_key),
                            models_to_try[-1],
                        )
                        continue

                    mapped_exc = self._map_exception(exc, current_key)
                    if isinstance(mapped_exc, LLMRateLimitError):
                        self._key_pool.mark_rate_limited(current_key)
                        if key_idx + 1 < len(candidates):
                            logger.warning(
                                "Gemini key %s rate-limited / high demand. Failing over to next key (%d remaining)...",
                                mask_key(current_key),
                                len(candidates) - (key_idx + 1),
                            )
                            last_exc = mapped_exc
                            break
                    logger.warning("Gemini async generation failed: %s", mapped_exc)
                    raise mapped_exc from exc

        if last_exc:
            raise last_exc
        raise LLMProviderError("Gemini async request failed: no responses returned", provider="gemini")
