"""Unit tests for the RailSaathi AI LLM Client layer (Phase 6B.3).

All tests are completely deterministic and offline:
- MockLLMClient operates purely in-memory.
- GeminiLLMClient tests mock the Google GenAI SDK responses; ZERO external
  network calls or real API credits are consumed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from app.agent.llm import (
    DemoLLMClient,
    GeminiLLMClient,
    LLMAuthenticationError,
    LLMError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    MockLLMClient,
    get_llm_client,
)
from app.core.config import Settings


# ============================================================================
# 1. MockLLMClient Tests
# ============================================================================

def test_mock_client_default_response():
    client = MockLLMClient(default_response="Default answer")
    assert client.provider_name == "mock"
    assert client.model_name == "mock-model"

    result = client.generate_text("Hello there")
    assert result == "Default answer"
    assert client.call_count == 1
    assert client.history[0]["prompt"] == "Hello there"


@pytest.mark.asyncio
async def test_mock_client_async_response():
    client = MockLLMClient(default_response="Async answer")
    result = await client.generate_text_async("Query", system_instruction="Be helpful")
    assert result == "Async answer"
    assert client.call_count == 1
    assert client.history[0]["system_instruction"] == "Be helpful"


def test_mock_client_queued_responses():
    client = MockLLMClient(responses=["First", "Second"])
    assert client.generate_text("Prompt 1") == "First"
    assert client.generate_text("Prompt 2") == "Second"
    # Exhausted queue falls back to default
    assert client.generate_text("Prompt 3") == "Mock LLM response"
    assert client.call_count == 3


def test_mock_client_pattern_responses():
    client = MockLLMClient(
        pattern_responses={
            "delhi": "NDLS stations detected",
            "mumbai": "CSMT stations detected",
        },
        default_response="General query",
    )
    assert client.generate_text("I want to go to Delhi") == "NDLS stations detected"
    assert client.generate_text("Book ticket to Mumbai") == "CSMT stations detected"
    assert client.generate_text("What is PNR status?") == "General query"


def test_mock_client_error_simulation():
    client = MockLLMClient(error_to_raise=LLMRateLimitError("Simulated 429", status_code=429))
    with pytest.raises(LLMRateLimitError) as exc_info:
        client.generate_text("Any prompt")
    assert exc_info.value.status_code == 429

    # Test queued error
    queued_client = MockLLMClient()
    queued_client.queue_error(LLMTimeoutError("Simulated timeout"))
    queued_client.queue_response("Success after retry")

    with pytest.raises(LLMTimeoutError):
        queued_client.generate_text("Call 1")

    assert queued_client.generate_text("Call 2") == "Success after retry"


def test_mock_client_clear_history():
    client = MockLLMClient()
    client.generate_text("Hello")
    assert client.call_count == 1
    client.clear_history()
    assert client.call_count == 0


# ============================================================================
# 2. GeminiLLMClient Initialization & Safety Tests
# ============================================================================

def test_gemini_client_missing_key_raises_auth_error():
    with pytest.raises(LLMAuthenticationError) as exc_info:
        GeminiLLMClient(api_key=None)
    assert "not configured" in str(exc_info.value).lower()
    assert exc_info.value.provider == "gemini"


def test_gemini_client_empty_key_raises_auth_error():
    with pytest.raises(LLMAuthenticationError) as exc_info:
        GeminiLLMClient(api_key="")
    assert "empty" in str(exc_info.value).lower()

    with pytest.raises(LLMAuthenticationError):
        GeminiLLMClient(api_key="   ")

    with pytest.raises(LLMAuthenticationError):
        GeminiLLMClient(api_key=SecretStr(""))


def test_gemini_client_repr_and_str_mask_key():
    client = GeminiLLMClient(api_key=SecretStr("AIzaSyVerySecretKey123"), model="gemini-3.5-flash-lite")
    repr_str = repr(client)
    str_str = str(client)

    assert "AIzaSy" not in repr_str
    assert "VerySecretKey123" not in repr_str
    assert "AIzaSy" not in str_str
    assert "gemini-3.5-flash-lite" in repr_str
    assert client.provider_name == "gemini"
    assert client.model_name == "gemini-3.5-flash-lite"


# ============================================================================
# 3. GeminiLLMClient Generation Tests (Mocked SDK)
# ============================================================================

def test_gemini_client_sync_generation_success():
    mock_sdk_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Here is your train route from NDLS to BCT."
    mock_sdk_client.models.generate_content.return_value = mock_response

    client = GeminiLLMClient(client=mock_sdk_client, model="gemini-3.5-flash-lite")

    result = client.generate_text(
        prompt="Find trains from Delhi to Mumbai",
        system_instruction="You are RailSaathi AI assistant.",
        temperature=0.2,
        max_output_tokens=500,
    )

    assert result == "Here is your train route from NDLS to BCT."
    mock_sdk_client.models.generate_content.assert_called_once()
    call_kwargs = mock_sdk_client.models.generate_content.call_args.kwargs

    assert call_kwargs["model"] == "gemini-3.5-flash-lite"
    assert call_kwargs["contents"] == "Find trains from Delhi to Mumbai"
    config = call_kwargs["config"]
    assert config.system_instruction == "You are RailSaathi AI assistant."
    assert config.temperature == 0.2
    assert config.max_output_tokens == 500
    assert config.http_options.timeout == 30000  # 30.0s -> 30000ms


@pytest.mark.asyncio
async def test_gemini_client_async_generation_success():
    mock_sdk_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Async response from Gemini"
    mock_sdk_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    client = GeminiLLMClient(client=mock_sdk_client, model="gemini-3.5-flash-lite", timeout_seconds=12.5)

    result = await client.generate_text_async(
        prompt="Async query",
        temperature=0.5,
    )

    assert result == "Async response from Gemini"
    mock_sdk_client.aio.models.generate_content.assert_awaited_once()
    call_kwargs = mock_sdk_client.aio.models.generate_content.call_args.kwargs
    assert call_kwargs["config"].http_options.timeout == 12500  # 12.5s -> 12500ms


def test_gemini_client_lazy_initialization_passes_timeout_to_sdk():
    with patch("google.genai.Client") as mock_genai_constructor:
        client = GeminiLLMClient(
            api_key=SecretStr("AIzaSyDummyKeyForInitTest"),
            model="gemini-3.5-flash-lite",
            timeout_seconds=20.0,
        )
        sdk_client = client._get_client()
        mock_genai_constructor.assert_called_once()
        call_kwargs = mock_genai_constructor.call_args.kwargs
        assert call_kwargs["api_key"] == "AIzaSyDummyKeyForInitTest"
        assert call_kwargs["http_options"].timeout == 20000  # 20.0s -> 20000ms


def test_gemini_client_redacts_api_key_from_exceptions():
    raw_key = "AIzaSySecretKeyThatShouldNeverLeak"
    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.side_effect = RuntimeError(
        f"Request failed at url https://generativelanguage.googleapis.com?key={raw_key}"
    )

    client = GeminiLLMClient(api_key=SecretStr(raw_key), client=mock_sdk_client)

    with pytest.raises(LLMProviderError) as exc_info:
        client.generate_text("Prompt")

    err_msg = str(exc_info.value)
    assert raw_key not in err_msg
    assert "[REDACTED]" in err_msg


def test_gemini_client_empty_response_raises_invalid_response_error():
    mock_sdk_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""  # Empty string (e.g. safety filter)
    mock_sdk_client.models.generate_content.return_value = mock_response

    client = GeminiLLMClient(client=mock_sdk_client)

    with pytest.raises(LLMInvalidResponseError) as exc_info:
        client.generate_text("Prompt")
    assert "empty" in str(exc_info.value).lower()
    assert exc_info.value.provider == "gemini"


def test_gemini_client_none_response_raises_invalid_response_error():
    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.return_value = None

    client = GeminiLLMClient(client=mock_sdk_client)

    with pytest.raises(LLMInvalidResponseError):
        client.generate_text("Prompt")


# ============================================================================
# 4. GeminiLLMClient Exception Mapping Tests
# ============================================================================

def test_gemini_client_maps_auth_error_401():
    class DummyAPIError(Exception):
        code = 401

    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.side_effect = DummyAPIError("Invalid API key")

    client = GeminiLLMClient(client=mock_sdk_client)

    with pytest.raises(LLMAuthenticationError) as exc_info:
        client.generate_text("Prompt")
    assert exc_info.value.status_code == 401
    assert exc_info.value.provider == "gemini"


def test_gemini_client_maps_auth_error_403():
    class DummyAPIError(Exception):
        code = 403

    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.side_effect = DummyAPIError("Forbidden / unauthenticated")

    client = GeminiLLMClient(client=mock_sdk_client)

    with pytest.raises(LLMAuthenticationError) as exc_info:
        client.generate_text("Prompt")
    assert exc_info.value.status_code == 403


def test_gemini_client_maps_rate_limit_429():
    class DummyRateLimitError(Exception):
        code = 429

    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.side_effect = DummyRateLimitError("RESOURCE_EXHAUSTED quota exceeded")

    client = GeminiLLMClient(client=mock_sdk_client)

    with pytest.raises(LLMRateLimitError) as exc_info:
        client.generate_text("Prompt")
    assert exc_info.value.status_code == 429
    assert exc_info.value.provider == "gemini"


def test_gemini_client_maps_timeout_error():
    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.side_effect = TimeoutError("Connection timed out")

    client = GeminiLLMClient(client=mock_sdk_client, timeout_seconds=15.0)

    with pytest.raises(LLMTimeoutError) as exc_info:
        client.generate_text("Prompt")
    assert "timed out" in str(exc_info.value).lower()
    assert exc_info.value.provider == "gemini"


def test_gemini_client_maps_server_error_500():
    class DummyServerError(Exception):
        code = 500

    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.side_effect = DummyServerError("Backend unavailable")

    client = GeminiLLMClient(client=mock_sdk_client)

    with pytest.raises(LLMProviderError) as exc_info:
        client.generate_text("Prompt")
    assert exc_info.value.status_code == 500
    assert exc_info.value.provider == "gemini"


def test_gemini_client_maps_503_overload_to_rate_limit_for_key_failover():
    """503 / overloaded / high-demand responses must map to LLMRateLimitError
    (not LLMProviderError) so the key pool benches the key and fails over to
    the next candidate instead of surfacing a hard error to the user."""

    class DummyOverloaded(Exception):
        code = 503

    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.side_effect = DummyOverloaded("Model overloaded, high demand")

    client = GeminiLLMClient(client=mock_sdk_client)

    with pytest.raises(LLMRateLimitError) as exc_info:
        client.generate_text("Prompt")
    assert exc_info.value.status_code == 503
    assert exc_info.value.provider == "gemini"


def test_gemini_client_maps_unexpected_exception():
    mock_sdk_client = MagicMock()
    mock_sdk_client.models.generate_content.side_effect = RuntimeError("Something unexpected")

    client = GeminiLLMClient(client=mock_sdk_client)

    with pytest.raises(LLMProviderError) as exc_info:
        client.generate_text("Prompt")
    assert "RuntimeError" in str(exc_info.value)


# ============================================================================
# 5. Factory Tests (get_llm_client)
# ============================================================================

def test_factory_creates_mock_client():
    client = get_llm_client(
        provider="mock",
        default_response="Hello Mock",
        model_name="test-mock",
    )
    assert isinstance(client, MockLLMClient)
    assert client.provider_name == "mock"
    assert client.model_name == "test-mock"
    assert client.generate_text("ping") == "Hello Mock"


def test_factory_creates_gemini_client_with_injected_client():
    mock_sdk = MagicMock()
    client = get_llm_client(
        provider="gemini",
        client=mock_sdk,
        model="gemini-3.5-flash-lite",
        timeout_seconds=20.0,
    )
    assert isinstance(client, GeminiLLMClient)
    assert client.provider_name == "gemini"
    assert client.model_name == "gemini-3.5-flash-lite"


def test_factory_creates_gemini_client_from_settings():
    settings = Settings(
        llm_provider="gemini",
        llm_model="gemini-3.5-flash-lite",
        gemini_api_key=SecretStr("test-key-12345"),
        llm_timeout_seconds=25.0,
    )
    client = get_llm_client(settings=settings)
    assert isinstance(client, GeminiLLMClient)
    assert client.provider_name == "gemini"
    assert client.model_name == "gemini-3.5-flash-lite"


def test_factory_gemini_without_api_key_raises_auth_error_not_fallback():
    settings = Settings(
        llm_provider="gemini",
        gemini_api_key=None,
    )
    with pytest.raises(LLMAuthenticationError) as exc_info:
        get_llm_client(settings=settings)
    assert "not configured" in str(exc_info.value).lower()


def test_factory_mock_never_requires_api_key():
    settings = Settings(
        llm_provider="mock",
        gemini_api_key=None,
    )
    client = get_llm_client(settings=settings)
    assert isinstance(client, MockLLMClient)
    assert client.provider_name == "mock"
    assert client.generate_text("Test query") == "Mock LLM response"


def test_factory_unsupported_provider_falls_back_to_demo_when_unconfigured():
    # Hermetic: explicit credential-less settings so the assertion cannot
    # depend on the developer's local backend/.env (which may hold real keys).
    # Contract: an unknown provider string NEVER hard-crashes the app - with
    # no credentials configured it degrades to the offline demo client
    # (deterministic rule-based extraction, still fully functional offline).
    settings = Settings(
        llm_provider="unsupported_ai",
        gemini_api_key=None,
    )
    client = get_llm_client(settings=settings, provider="unsupported_ai")
    assert isinstance(client, DemoLLMClient)
    assert client.provider_name == "demo"


def test_factory_unsupported_provider_falls_back_to_gemini_when_configured():
    # Credentials present -> the factory must never hard-crash on an unknown
    # provider string; it degrades to the configured Gemini pool instead.
    settings = Settings(
        llm_provider="unsupported_ai",
        gemini_api_key=SecretStr("fake-key-123456789"),
    )
    client = get_llm_client(settings=settings, provider="unsupported_ai")
    assert isinstance(client, GeminiLLMClient)
    assert client.provider_name == "gemini"
