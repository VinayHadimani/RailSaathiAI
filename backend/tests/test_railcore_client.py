"""RailCoreClient unit tests (all provider responses are mocked).

Covers: schedule retrieval and normalization, availability status mapping,
error handling (401, 402, 429, timeouts, 5xx, malformed responses),
retry/backoff behaviour, usage-header tracking, credential hygiene and
HMAC signing.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import (
    InternalAvailabilityStatus,
    ProviderAuthError,
    ProviderCreditsError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderUnexpectedResponseError,
)
from app.providers.railcore import RailCoreClient

from tests.conftest import (
    MOCK_TRAIN,
    avail_entry,
    availability_payload,
    error_envelope,
    make_client,
    make_handler,
    mock_schedule_payload,
    usage_headers,
)


# --------------------------------------------------------------- schedule


def test_schedule_retrieval_and_normalization():
    """Test 1: successful schedule retrieval (mocked provider)."""
    client = make_client(make_handler())
    result = client.get_train_schedule(MOCK_TRAIN)

    schedule = result.data
    assert schedule.train_number == MOCK_TRAIN
    assert schedule.train_name == "Mock Overnight Express"
    assert schedule.source_station_code == "MYS"
    assert schedule.destination_station_code == "BGM"
    assert len(schedule.stops) == 7

    ask = schedule.stop_by_code("ASK")
    assert ask is not None and ask.sequence == 4
    assert ask.station_name == "Arsikere Junction"
    assert ask.arrival_time == "01:25"

    # Overnight support: origin is day 1, later stations are day 2.
    assert schedule.stop_by_code("MYS").day == 1
    assert schedule.stop_by_code("DWR").day == 2
    # Origin has no arrival, destination has no departure.
    assert schedule.stop_by_code("MYS").arrival_time is None
    assert schedule.stop_by_code("BGM").departure_time is None
    # Raw provider payload preserved for feasibility analysis.
    assert result.raw["success"] is True
    assert result.request_id == "req_test_sched"


def test_schedule_unknown_train_returns_client_error():
    def schedule_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=error_envelope("TRAIN_NOT_FOUND", "train not found", "CLIENT"))

    client = make_client(make_handler(schedule_responder=schedule_404))
    with pytest.raises(Exception) as excinfo:
        client.get_train_schedule("99998")
    assert excinfo.value.code == "TRAIN_NOT_FOUND"
    assert excinfo.value.http_status == 404


# ---------------------------------------------------------- availability


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("AVAILABLE", InternalAvailabilityStatus.CONFIRMED_AVAILABLE),
        ("RAC", InternalAvailabilityStatus.RAC),
        ("WAITLIST", InternalAvailabilityStatus.WAITLIST),
        ("REGRET", InternalAvailabilityStatus.NOT_AVAILABLE),
        ("NOT_AVAILABLE", InternalAvailabilityStatus.NOT_AVAILABLE),
        ("TRAIN_CANCELLED", InternalAvailabilityStatus.NOT_AVAILABLE),
        ("TRAIN_DEPARTED", InternalAvailabilityStatus.NOT_AVAILABLE),
        ("UNKNOWN", InternalAvailabilityStatus.UNKNOWN),
    ],
)
def test_availability_status_mapping(provider_status, expected):
    """Explicit mapping: only AVAILABLE is confirmed; unknown is never confirmed."""
    handler = make_handler(
        availability_responses={("MYS", "ASK"): avail_entry(status=provider_status, text=f"{provider_status}-x")}
    )
    client = make_client(handler)
    result = client.get_seat_availability(
        train_number=MOCK_TRAIN, from_station="MYS", to_station="ASK",
        journey_date="2026-11-24", travel_class="SL", quota="GN",
    )
    assert result.data.status == expected
    assert result.data.requested_class.provider_status == provider_status


def test_availability_confirmed_carries_counts_and_fare():
    handler = make_handler(
        availability_responses={
            ("MYS", "ASK"): avail_entry(status="AVAILABLE", text="AVAILABLE-120", available_count=120, fare=255, total_fare=255)
        }
    )
    client = make_client(handler)
    result = client.get_seat_availability(
        train_number=MOCK_TRAIN, from_station="MYS", to_station="ASK",
        journey_date="2026-11-24", travel_class="SL", quota="GN",
    )
    requested = result.data.requested_class
    assert result.data.status == InternalAvailabilityStatus.CONFIRMED_AVAILABLE
    assert requested.available_count == 120
    assert requested.availability_text == "AVAILABLE-120"
    assert requested.fare == 255
    assert requested.last_updated_at == "2026-11-23T08:15:46.547+05:30"


def test_availability_requested_class_not_offered():
    """A class missing from the response is UNKNOWN, never confirmed."""
    handler = make_handler(
        availability_responses={
            ("MYS", "ASK"): [avail_entry(class_code="2S", status="AVAILABLE")]
        }
    )
    client = make_client(handler)
    result = client.get_seat_availability(
        train_number=MOCK_TRAIN, from_station="MYS", to_station="ASK",
        journey_date="2026-11-24", travel_class="SL", quota="GN",
    )
    assert result.data.status == InternalAvailabilityStatus.UNKNOWN
    assert result.data.class_not_offered is True
    assert result.data.requested_class is None


# ------------------------------------------------------- provider errors


def test_invalid_api_key_401():
    """Test: invalid API key -> auth error, never retried."""
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=error_envelope("INVALID_API_KEY", "Unauthorized", "AUTH"))

    client = make_client(make_handler(schedule_responder=unauthorized))
    with pytest.raises(ProviderAuthError) as excinfo:
        client.get_train_schedule(MOCK_TRAIN)
    assert excinfo.value.code == "INVALID_API_KEY"
    assert client.stats["requests_sent"] == 1  # permanent error: no retry


def test_credits_exhausted_402():
    def exhausted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json=error_envelope("CREDITS_EXHAUSTED", "no credits", "BILLING"))

    client = make_client(make_handler(schedule_responder=exhausted))
    with pytest.raises(ProviderCreditsError):
        client.get_train_schedule(MOCK_TRAIN)


def test_rate_limit_without_retry():
    """429 with max_retries=0 -> ProviderRateLimitError, not availability."""
    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json=error_envelope("RATE_LIMITED", "rate limit exceeded", "RATE_LIMIT", retryable=True),
            headers={"retry-after": "120"},
        )

    client = make_client(make_handler(schedule_responder=limited))
    with pytest.raises(ProviderRateLimitError) as excinfo:
        client.get_train_schedule(MOCK_TRAIN)
    assert excinfo.value.details["retry_after_seconds"] == 120
    assert client.stats["requests_sent"] == 1


def test_rate_limit_large_retry_after_is_not_retried_inline():
    """A 429 with a long Retry-After fails fast instead of blocking."""
    calls = {"count": 0}

    def limited(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            429,
            json=error_envelope("RATE_LIMITED", "rate limit exceeded", "RATE_LIMIT", retryable=True),
            headers={"retry-after": "120"},
        )

    client = make_client(make_handler(schedule_responder=limited), max_retries=3)
    with pytest.raises(ProviderRateLimitError):
        client.get_train_schedule(MOCK_TRAIN)
    assert calls["count"] == 1  # did not sleep 120s / did not retry


def test_rate_limit_retry_then_success():
    """A retryable 429 with Retry-After: 0 is retried and succeeds."""
    calls = {"count": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                429,
                json=error_envelope("RATE_LIMITED", "rate limit exceeded", "RATE_LIMIT", retryable=True),
                headers={"retry-after": "0"},
            )
        return httpx.Response(200, json=mock_schedule_payload(), headers=usage_headers("req_test_retry"))

    client = make_client(make_handler(schedule_responder=flaky), max_retries=2)
    result = client.get_train_schedule(MOCK_TRAIN)
    assert result.data.train_number == MOCK_TRAIN
    assert calls["count"] == 2
    assert client.stats["successful_requests"] == 1


def test_timeout():
    """Test: API timeout -> ProviderTimeoutError (never an availability result)."""

    def timing_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    client = make_client(make_handler(schedule_responder=timing_out))
    with pytest.raises(ProviderTimeoutError):
        client.get_train_schedule(MOCK_TRAIN)


def test_server_error_retry_then_success():
    """5xx is retryable with backoff; second attempt succeeds."""
    calls = {"count": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, json=error_envelope("UPSTREAM_UNAVAILABLE", "upstream down", "SERVER", retryable=True))
        return httpx.Response(200, json=mock_schedule_payload(), headers=usage_headers("req_test_retry"))

    client = make_client(make_handler(schedule_responder=flaky), max_retries=2)
    result = client.get_train_schedule(MOCK_TRAIN)
    assert result.data.train_number == MOCK_TRAIN
    assert calls["count"] == 2
    assert client.stats["failed_requests"] == 1


def test_non_json_response():
    """Test: unexpected provider response - non-JSON body with HTTP 200."""

    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error page</html>", headers={"content-type": "application/json"})

    client = make_client(make_handler(schedule_responder=not_json))
    with pytest.raises(ProviderUnexpectedResponseError) as excinfo:
        client.get_train_schedule(MOCK_TRAIN)
    assert excinfo.value.code == "INVALID_JSON"


def test_2xx_with_success_false_envelope():
    """Defensive: 2xx whose envelope says success=false is a provider error."""

    def weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=error_envelope("TRAIN_NOT_FOUND", "hidden error", "CLIENT"))

    client = make_client(make_handler(schedule_responder=weird))
    with pytest.raises(ProviderUnexpectedResponseError):
        client.get_train_schedule(MOCK_TRAIN)


def test_availability_missing_classes_array():
    """Malformed availability payload -> provider error, not availability."""

    def malformed(query: dict) -> httpx.Response:
        return httpx.Response(
            200,
            json=availability_payload(query["from"], query["to"], drop_classes_key=True),
            headers=usage_headers("req_test_malformed"),
        )

    handler = make_handler(availability_responses={("MYS", "ASK"): malformed})
    client = make_client(handler)
    with pytest.raises(ProviderUnexpectedResponseError):
        client.get_seat_availability(
            train_number=MOCK_TRAIN, from_station="MYS", to_station="ASK",
            journey_date="2026-11-24", travel_class="SL", quota="GN",
        )


def test_missing_api_key_configuration():
    """No API key configured -> clear configuration error."""
    client = make_client(make_handler(), api_key=None)
    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        client.get_train_schedule(MOCK_TRAIN)
    assert ".env" in excinfo.value.message


# --------------------------------------------------- usage + credentials


def test_usage_headers_tracked():
    client = make_client(make_handler())
    client.get_seat_availability(
        train_number=MOCK_TRAIN, from_station="MYS", to_station="ASK",
        journey_date="2026-11-24", travel_class="SL", quota="GN",
    )
    assert client.stats["requests_sent"] == 1
    assert client.stats["successful_requests"] == 1
    assert client.credits_cost_total == 1
    assert client.credits_remaining == 498
    assert client.rate_limit_remaining == 9
    assert client.rate_limit_day_remaining == 97


def test_same_query_params_sent_to_provider():
    """Both segment requests use the same train/date/class/quota (only stations differ)."""
    seen: list[dict] = []

    def spy_handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json=availability_payload(
                dict(request.url.params)["from"], dict(request.url.params)["to"], [avail_entry()]
            ),
            headers=usage_headers("req_test_spy"),
        )

    handler = make_handler(schedule_responder=lambda req: httpx.Response(200, json=mock_schedule_payload(), headers=usage_headers("req_test_sched")))
    # Route availability calls through the spy by using it for every pair.
    client = make_client(lambda request: spy_handler(request) if request.url.path.endswith("/availability/seats") else handler(request))
    for pair in (("MYS", "ASK"), ("ASK", "DWR")):
        client.get_seat_availability(
            train_number=MOCK_TRAIN, from_station=pair[0], to_station=pair[1],
            journey_date="2026-11-24", travel_class="SL", quota="GN",
        )
    assert len(seen) == 2
    for params in seen:
        assert params["train_number"] == MOCK_TRAIN
        assert params["date"] == "2026-11-24"
        assert params["class"] == "SL"
        assert params["quota"] == "GN"
    assert seen[0]["from"] == "MYS" and seen[0]["to"] == "ASK"
    assert seen[1]["from"] == "ASK" and seen[1]["to"] == "DWR"


def test_hmac_signing_headers_added_when_secret_configured():
    captured: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured.update(
            {
                "signature": request.headers.get("x-railcore-signature"),
                "timestamp": request.headers.get("x-railcore-timestamp"),
                "nonce": request.headers.get("x-railcore-nonce"),
                "url": str(request.url),
            }
        )
        return httpx.Response(200, json=mock_schedule_payload(), headers=usage_headers("req_test_hmac"))

    client = make_client(make_handler(schedule_responder=capture), hmac_secret="test-signing-secret")
    client.get_train_schedule(MOCK_TRAIN)
    assert captured["signature"]
    assert captured["timestamp"]
    assert len(captured["nonce"]) == 32
    assert "/v1/trains/99999/schedule" in captured["url"]


def test_provider_response_carries_raw_payload_and_metadata():
    client = make_client(make_handler())
    result = client.get_seat_availability(
        train_number=MOCK_TRAIN, from_station="MYS", to_station="ASK",
        journey_date="2026-11-24", travel_class="SL", quota="GN",
    )
    assert result.raw["data"]["from_station_code"] == "MYS"
    assert result.credits_cost == 1
    assert result.http_status == 200
    assert result.checked_at  # UTC timestamp of when we checked


# ------------------------------------------------------------ batch checks


def test_batch_availability_checks_both_segments_in_one_request():
    """Both same-train segments can be checked in a single POST batch."""
    client = make_client(make_handler())
    batch = client.get_batch_availability(
        [
            {"train_number": MOCK_TRAIN, "from": "MYS", "to": "ASK", "date": "2026-11-24", "classes": ["SL"], "quota": "GN"},
            {"train_number": MOCK_TRAIN, "from": "ASK", "to": "DWR", "date": "2026-11-24", "classes": ["SL"], "quota": "GN"},
        ]
    )
    assert client.stats["requests_sent"] == 1  # one HTTP call
    assert len(batch.items) == 2
    assert batch.failed_items == []
    assert batch.items[0].from_station == "MYS"
    assert batch.items[0].requested_class.status == InternalAvailabilityStatus.CONFIRMED_AVAILABLE
    assert batch.items[1].from_station == "ASK"
    assert batch.items[1].to_station == "DWR"
    assert batch.request_id == "req_test_batch"


def test_batch_failed_items_are_not_availability():
    """A per-item failure (e.g. BOOKING_NOT_OPEN) carries no availability claim."""
    client = make_client(make_handler())
    batch = client.get_batch_availability(
        [
            {"train_number": MOCK_TRAIN, "from": "FAIL", "to": "ITEM", "date": "2026-11-24", "classes": ["SL"], "quota": "GN"},
        ]
    )
    assert batch.items == []
    assert batch.failed_items[0]["error"] == "BOOKING_NOT_OPEN"


def test_batch_size_validation():
    client = make_client(make_handler())
    with pytest.raises(ValueError):
        client.get_batch_availability([])
