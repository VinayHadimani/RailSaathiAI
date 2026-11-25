"""Unit tests for APIKeyPool and multi-key rate-limit failover across providers."""

import time
from unittest.mock import MagicMock
import httpx
import pytest

from app.core.key_pool import APIKeyPool, mask_key
from app.providers.base import ProviderRateLimitError
from app.providers.railcore import RailCoreClient
from app.agent.llm.gemini import GeminiLLMClient
from app.agent.llm.exceptions import LLMRateLimitError


# ---------------------------------------------------------------------------
# APIKeyPool unit tests
# ---------------------------------------------------------------------------


def test_key_pool_parsing():
    pool1 = APIKeyPool("key1, key2, key3")
    assert pool1.keys == ["key1", "key2", "key3"]
    assert pool1.total_count == 3
    assert pool1.is_configured is True

    # Deduplication and whitespace trimming
    pool2 = APIKeyPool([" keyA ", "keyB", "keyA", ""])
    assert pool2.keys == ["keyA", "keyB"]
    assert pool2.total_count == 2

    # Empty pool
    pool3 = APIKeyPool(None)
    assert pool3.keys == []
    assert pool3.total_count == 0
    assert pool3.is_configured is False


def test_mask_key():
    assert mask_key("rk_live_example1234567890") == "rk_liv...7890"
    assert mask_key("AQ.Ab8_example1234567890") == "AQ.Ab8...7890"
    assert mask_key("short") == "[REDACTED]"
    assert mask_key("") == "[EMPTY]"
    assert mask_key(None) == "[EMPTY]"


def test_key_pool_round_robin():
    pool = APIKeyPool(["k1", "k2", "k3"])

    c1 = pool.get_candidates()
    assert c1[0] == "k1"
    pool.advance()

    c2 = pool.get_candidates()
    assert c2[0] == "k2"
    pool.advance()

    c3 = pool.get_candidates()
    assert c3[0] == "k3"
    pool.advance()

    c4 = pool.get_candidates()
    assert c4[0] == "k1"


def test_key_pool_rate_limiting_and_recovery():
    pool = APIKeyPool(["k1", "k2", "k3"], default_cooldown_seconds=0.1)

    # Mark k1 as rate limited
    pool.mark_rate_limited("k1", cooldown_seconds=0.2)
    assert pool.available_count() == 2

    # Candidates should not include k1 while in cooldown
    c = pool.get_candidates()
    assert "k1" not in c
    assert c == ["k2", "k3"]

    # Sleep past cooldown
    time.sleep(0.25)
    assert pool.available_count() == 3
    c_after = pool.get_candidates()
    assert "k1" in c_after


def test_key_pool_all_in_cooldown_fallback():
    pool = APIKeyPool(["k1", "k2"], default_cooldown_seconds=10.0)
    pool.mark_rate_limited("k1", cooldown_seconds=5.0)
    pool.mark_rate_limited("k2", cooldown_seconds=15.0)

    assert pool.available_count() == 0
    # When all in cooldown, returns keys ordered by earliest expiration (k1 before k2)
    c = pool.get_candidates()
    assert c == ["k1", "k2"]


# ---------------------------------------------------------------------------
# RailCoreClient multi-key failover tests
# ---------------------------------------------------------------------------


def test_railcore_multi_key_failover():
    """Verify that when Key 1 receives HTTP 429, RailCoreClient immediately rotates to Key 2."""
    seen_keys: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("X-RailCore-Key")
        seen_keys.append(key)
        if key == "key-primary":
            return httpx.Response(
                429,
                json={"success": False, "error": {"code": "RATE_LIMITED", "message": "10 req/min exceeded"}},
                headers={"Retry-After": "30"},
            )
        # Second key succeeds
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "train_number": "12345",
                    "train_name": "Test Express",
                    "source_station_code": "MYS",
                    "destination_station_code": "SBC",
                    "stops": [],
                },
                "meta": {},
            },
        )

    client = RailCoreClient(
        api_keys=["key-primary", "key-secondary"],
        base_url="https://mock.railcore.tech/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)),
    )

    resp = client.get_train_schedule("12345")
    assert resp.data.train_number == "12345"
    # Key 1 was tried, got 429, then Key 2 was tried and succeeded!
    assert seen_keys == ["key-primary", "key-secondary"]


def test_railcore_all_keys_exhausted():
    """Verify that if all keys in the pool hit 429, ProviderRateLimitError is raised."""
    seen_keys: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("X-RailCore-Key")
        seen_keys.append(key)
        return httpx.Response(
            429,
            json={"success": False, "error": {"code": "RATE_LIMITED", "message": "Quota exceeded"}},
        )

    client = RailCoreClient(
        api_keys=["k1", "k2"],
        base_url="https://mock.railcore.tech/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)),
    )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        client.get_train_schedule("12345")

    assert exc_info.value.code == "RATE_LIMITED"
    assert seen_keys == ["k1", "k2"]


# ---------------------------------------------------------------------------
# GeminiLLMClient multi-key failover tests
# ---------------------------------------------------------------------------


def test_gemini_multi_key_failover():
    """Verify that when Key 1 receives 429 / RESOURCE_EXHAUSTED, Gemini rotates to Key 2."""
    call_keys: list[str] = []

    class MockModelClient:
        def __init__(self, key: str):
            self.key = key

        def generate_content(self, model, contents, config):
            call_keys.append(self.key)
            if self.key == "gemini-key-1":
                err = Exception("Resource has been exhausted (e.g. check quota).")
                err.code = 429
                raise err

            resp = MagicMock()
            resp.text = '{"status": "OK", "answer": "resolved"}'
            return resp

    class MockGenAIClient:
        def __init__(self, key: str):
            self.models = MockModelClient(key)

    client = GeminiLLMClient(
        api_keys=["gemini-key-1", "gemini-key-2"],
        model="gemini-3.5-flash-lite",
    )
    # Inject mocked SDK clients mapped to the keys
    client._clients["gemini-key-1"] = MockGenAIClient("gemini-key-1")
    client._clients["gemini-key-2"] = MockGenAIClient("gemini-key-2")

    res = client.generate_text("test prompt")
    assert "resolved" in res
    assert call_keys == ["gemini-key-1", "gemini-key-2"]


def test_gemini_all_keys_rate_limited():
    """Verify that when all keys in Gemini pool hit 429, LLMRateLimitError is raised."""
    class MockModelClient:
        def generate_content(self, model, contents, config):
            err = Exception("Resource has been exhausted (quota exceeded).")
            err.code = 429
            raise err

    class MockGenAIClient:
        def __init__(self):
            self.models = MockModelClient()

    client = GeminiLLMClient(
        api_keys=["gk1", "gk2"],
        model="gemini-3.5-flash-lite",
    )
    client._clients["gk1"] = MockGenAIClient()
    client._clients["gk2"] = MockGenAIClient()

    with pytest.raises(LLMRateLimitError):
        client.generate_text("test prompt")


def test_key_pool_strips_surrounding_quotes():
    """Verify accidental surrounding quotes from env vars are stripped when parsing keys."""
    pool = APIKeyPool('"rk_live_one", \'rk_live_two\', "rk_live_one"', name="TestQuoted")
    assert pool.keys == ["rk_live_one", "rk_live_two"]


def test_railcore_auth_failover_on_invalid_first_key():
    """Verify RailCoreClient fails over to next key when first key returns 401 INVALID_API_KEY."""
    seen_keys: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("X-RailCore-Key")
        seen_keys.append(key)
        if key == "bad-key":
            return httpx.Response(
                401,
                json={"success": False, "error": {"code": "INVALID_API_KEY", "message": "Unauthorized"}},
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "train_number": "12345",
                    "train_name": "Test Express",
                    "source_station_code": "MYS",
                    "destination_station_code": "SBC",
                    "stops": [],
                },
                "meta": {},
            },
        )

    client = RailCoreClient(
        api_keys=["bad-key", "good-key"],
        base_url="https://mock.railcore.tech/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)),
    )

    resp = client.get_train_schedule("12345")
    assert resp.data.train_number == "12345"
    assert seen_keys == ["bad-key", "good-key"]


def test_railcore_all_keys_in_long_cooldown_fails_fast_without_hammering():
    """Verify subsequent calls fail fast when all keys already have >5s cooldown."""
    calls = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "45"},
            json={"success": False, "error": {"code": "RATE_LIMITED", "message": "429"}},
        )

    client = RailCoreClient(
        api_keys=["k1", "k2"],
        base_url="https://mock.railcore.tech/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)),
    )

    with pytest.raises(ProviderRateLimitError):
        client.get_train_schedule("12345")
    assert calls == 2

    # Second call while both keys are in 45s cooldown must NOT hit HTTP again
    with pytest.raises(ProviderRateLimitError):
        client.get_train_schedule("12345")
    assert calls == 2


def test_railcore_credits_exhausted_failover_and_long_bench():
    """Verify 402 CREDITS_EXHAUSTED benches the key and fails over to healthy keys."""
    seen_keys: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("X-RailCore-Key")
        seen_keys.append(key)
        if key == "empty-key":
            return httpx.Response(
                402,
                json={"success": False, "error": {"code": "CREDITS_EXHAUSTED", "message": "Credits exhausted"}},
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "train_number": "12345",
                    "train_name": "Test Express",
                    "source_station_code": "MYS",
                    "destination_station_code": "SBC",
                    "stops": [],
                },
                "meta": {},
            },
        )

    client = RailCoreClient(
        api_keys=["empty-key", "funded-key"],
        base_url="https://mock.railcore.tech/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)),
    )

    resp = client.get_train_schedule("12345")
    assert resp.data.train_number == "12345"
    assert seen_keys == ["empty-key", "funded-key"]

    # Subsequent call goes straight to funded-key because empty-key is benched
    client.get_train_schedule("12345")
    assert seen_keys == ["empty-key", "funded-key", "funded-key"]


def test_key_pool_wait_for_key_gap_enforces_minimum_spacing():
    """Verify wait_for_key_gap records usage and spaces consecutive calls on the same key."""
    pool = APIKeyPool(["k1", "k2"], name="TestSpacing")
    t0 = time.monotonic()
    pool.wait_for_key_gap("k1", 0.05)
    pool.wait_for_key_gap("k2", 0.05)  # different key -> no wait
    elapsed_different = time.monotonic() - t0
    assert elapsed_different < 0.04

    t1 = time.monotonic()
    pool.wait_for_key_gap("k1", 0.06)  # same key -> spaces cleanly
    elapsed_same = time.monotonic() - t1
    assert elapsed_same >= 0.03




