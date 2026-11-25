"""LLM Client Factory for RailSaathi AI."""

from __future__ import annotations

import json
import logging
from typing import Any

from ...core.config import Settings, get_settings
from .base import LLMClient
from .demo import DemoLLMClient
from .exceptions import LLMError
from .gemini import GeminiLLMClient
from .mock import MockLLMClient

logger = logging.getLogger(__name__)

# Sensible pattern responses for mock provider during local development/demo
_DEFAULT_DEV_PATTERNS: dict[str, str] = {
    "mysore to dharwad day after tomorrow in 3a": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Mysore",
        "destination_raw": "Dharwad",
        "date_raw": "day after tomorrow",
        "travel_class": "3A",
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "mysuru to belagavi": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Mysuru",
        "destination_raw": "Belagavi",
        "date_raw": "tomorrow",
        "travel_class": None,
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "mysore to belagavi": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Mysore",
        "destination_raw": "Belagavi",
        "date_raw": "tomorrow",
        "travel_class": None,
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "bangalore to belagavi": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Bangalore",
        "destination_raw": "Belagavi",
        "date_raw": "tomorrow",
        "travel_class": None,
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "bengaluru to belagavi": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Bengaluru",
        "destination_raw": "Belagavi",
        "date_raw": "tomorrow",
        "travel_class": None,
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "bangalore to dharwad": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Bangalore",
        "destination_raw": "Dharwad",
        "date_raw": "tomorrow",
        "travel_class": None,
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "bengaluru to dharwad": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Bengaluru",
        "destination_raw": "Dharwad",
        "date_raw": "tomorrow",
        "travel_class": None,
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "mysore to dharwad": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Mysore",
        "destination_raw": "Dharwad",
        "date_raw": "tomorrow",
        "travel_class": None,
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "mysuru to dharwad": json.dumps({
        "intent": "JOURNEY_SEARCH",
        "origin_raw": "Mysuru",
        "destination_raw": "Dharwad",
        "date_raw": "tomorrow",
        "travel_class": None,
        "quota": None,
        "ranking_preference": None,
        "is_correction": False,
        "correction_target": None,
    }),
    "in 3a": json.dumps({
        "intent": "CORRECTION",
        "origin_raw": None,
        "destination_raw": None,
        "date_raw": None,
        "travel_class": "3A",
        "quota": None,
        "ranking_preference": None,
        "is_correction": True,
        "correction_target": "class",
    }),
    "cheapest": json.dumps({
        "intent": "CORRECTION",
        "origin_raw": None,
        "destination_raw": None,
        "date_raw": None,
        "travel_class": None,
        "quota": None,
        "ranking_preference": "CHEAPEST",
        "is_correction": True,
        "correction_target": "ranking_preference",
    }),
    "fastest": json.dumps({
        "intent": "CORRECTION",
        "origin_raw": None,
        "destination_raw": None,
        "date_raw": None,
        "travel_class": None,
        "quota": None,
        "ranking_preference": "FASTEST",
        "is_correction": True,
        "correction_target": "ranking_preference",
    }),
}


def get_llm_client(settings: Settings | None = None, **overrides: Any) -> LLMClient:
    """Factory creating an LLMClient instance based on settings or explicit overrides.

    Args:
        settings: Application settings instance (defaults to cached get_settings()).
        **overrides: Optional runtime overrides:
            provider: "gemini", "demo" or "mock"
            model / model_name: Model identifier string
            api_key: Provider API key
            timeout_seconds: Request timeout in seconds
            client: Pre-instantiated SDK client (useful for mocks/tests)
            responses / pattern_responses / error_to_raise: Mock client options

    Returns:
        Configured LLMClient instance. An unrecognized provider string never
        raises: it degrades to 'gemini' when credentials are configured, or
        to the offline 'demo' client otherwise, with a warning logged.

    Raises:
        ValueError: Only if provider is none of 'gemini', 'demo', 'mock' AND
            the credential fallback above could not resolve to a usable provider.
    """
    if settings is None:
        settings = get_settings()

    provider = str(overrides.get("provider") or settings.llm_provider).strip().lower()

    if provider not in ("gemini", "demo", "mock"):
        if settings.gemini_api_key_is_configured:
            logger.warning(
                "Unrecognized LLM_PROVIDER '%s'. Falling back to 'gemini' as Gemini credentials are configured.",
                provider,
            )
            provider = "gemini"
        else:
            logger.warning(
                "Unrecognized LLM_PROVIDER '%s'. Falling back to 'demo'.",
                provider,
            )
            provider = "demo"

    if provider == "mock":
        mock_kwargs: dict[str, Any] = {}
        if "default_response" in overrides:
            mock_kwargs["default_response"] = overrides["default_response"]
        if "responses" in overrides:
            mock_kwargs["responses"] = overrides["responses"]
        if "pattern_responses" in overrides:
            mock_kwargs["pattern_responses"] = overrides["pattern_responses"]
        elif "default_response" not in overrides and "responses" not in overrides:
            mock_kwargs["pattern_responses"] = _DEFAULT_DEV_PATTERNS
        if "error_to_raise" in overrides:
            mock_kwargs["error_to_raise"] = overrides["error_to_raise"]
        if "errors" in overrides:
            mock_kwargs["errors"] = overrides["errors"]
        model_name = overrides.get("model_name") or overrides.get("model") or "mock-model"
        mock_kwargs["model_name"] = model_name

        return MockLLMClient(**mock_kwargs)

    if provider == "demo":
        return DemoLLMClient(model_name=overrides.get("model_name") or overrides.get("model") or "demo-rules-v1")

    if provider == "gemini":
        gemini_kwargs: dict[str, Any] = {}
        api_key = overrides.get("api_key", settings.gemini_api_key)
        api_keys = overrides.get("api_keys", settings.gemini_api_keys_list)
        model = overrides.get("model", settings.llm_model)
        timeout_seconds = overrides.get("timeout_seconds", settings.llm_timeout_seconds)

        if "client" in overrides:
            gemini_kwargs["client"] = overrides["client"]
        if api_keys:
            gemini_kwargs["api_keys"] = api_keys

        return GeminiLLMClient(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            **gemini_kwargs,
        )

    raise ValueError(
        f"Unsupported LLM provider '{provider}'. Supported providers are: 'gemini', 'demo', 'mock'."
    )
