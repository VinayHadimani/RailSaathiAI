"""Endpoint workflow tests for POST /api/v1/test/segment-availability.

Runs the full FastAPI stack (validation, service, normalizer) against a
mocked RailCore transport, covering the required test cases:

- successful schedule retrieval + valid station sequence
- both segments available / one segment unavailable / RAC / waitlist
- invalid station sequence / station not on route
- provider failure (500), timeout, rate limit -> PROVIDER_ERROR
  (never interpreted as unavailable)
- missing API key configuration
- request schema validation
"""

from __future__ import annotations

import httpx

from app.providers.base import InternalAvailabilityStatus

from tests.conftest import (
    MOCK_TRAIN,
    avail_entry,
    availability_payload,
    error_envelope,
    make_handler,
    request_body,
    tomorrow_ist,
    usage_headers,
)


def _post(test_client, **overrides):
    return test_client.post("/api/v1/test/segment-availability", json=request_body(**overrides))


# ------------------------------------------------------------ happy paths


def test_both_segments_available(make_test_client):
    """Tests 1-4: schedule retrieval, valid sequence, both segments available."""
    test_client = make_test_client(make_handler())
    response = _post(test_client)
    assert response.status_code == 200

    body = response.json()
    assert body["success"] is True
    assert body["status"] == "COMPLETED_BOTH_CONFIRMED"
    assert body["both_segments_confirmed_available"] is True

    # Train info and full schedule are returned.
    assert body["train"]["number"] == MOCK_TRAIN
    assert body["train"]["total_stops"] == 7
    assert [s["station_code"] for s in body["schedule"]] == ["MYS", "KRNR", "HAS", "ASK", "UBL", "DWR", "BGM"]

    # Schedule validation: same train, ordered, contiguous.
    validation = body["schedule_validation"]
    assert validation["same_train"] is True
    assert validation["stations_on_route"] is True
    assert validation["first_segment_valid"] is True
    assert validation["second_segment_valid"] is True
    assert validation["first_segment_precedes_second"] is True
    assert validation["segments_contiguous"] is True
    assert validation["first_segment_sequence"] == {"from_sequence": 1, "to_sequence": 4}
    assert validation["second_segment_sequence"] == {"from_sequence": 4, "to_sequence": 6}

    # Both segments: confirmed availability with provider data preserved.
    assert len(body["segments"]) == 2
    for segment in body["segments"]:
        assert segment["status"] == "CONFIRMED_AVAILABLE"
        assert segment["provider_status"] == "AVAILABLE"
        assert segment["checked_at"]
        assert segment["last_updated_at"]
        assert segment["provider_raw"]["success"] is True
    assert body["segments"][0]["from_station"] == "MYS"
    assert body["segments"][0]["to_station"] == "ASK"
    assert body["segments"][1]["from_station"] == "ASK"
    assert body["segments"][1]["to_station"] == "DWR"
    assert body["segments"][1]["fare"] == 185

    # Usage: 1 schedule + 2 availability calls; 3 credits.
    assert body["api_usage"]["requests_sent"] == 3
    assert body["api_usage"]["credits_cost"] == 3
    assert body["availability_checked_at"]
    assert body["notes"]  # disclaimers present

    # Credential hygiene: the API key never appears anywhere in the response.
    assert "rk_live" not in response.text


def test_station_codes_are_case_insensitive(make_test_client):
    test_client = make_test_client(make_handler())
    response = _post(test_client, from_station_1="mys", to_station_1="ask", from_station_2="ask", to_station_2="dwr")
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_non_contiguous_ordered_segments_are_checked(make_test_client):
    """Segment 2 may start later than segment 1 ends (e.g. MYS->ASK then UBL->DWR)."""
    test_client = make_test_client(make_handler())
    response = _post(test_client, from_station_2="UBL")
    body = response.json()
    assert response.status_code == 200
    assert body["schedule_validation"]["segments_contiguous"] is False
    assert body["schedule_validation"]["first_segment_precedes_second"] is True
    assert body["segments"][1]["from_station"] == "UBL"
    assert body["both_segments_confirmed_available"] is True


# ------------------------------------------------- non-confirmed outcomes


def test_one_segment_unavailable(make_test_client):
    """Test: one segment REGRET -> the journey is not reported as available."""
    handler = make_handler(
        availability_responses={("ASK", "DWR"): avail_entry(status="REGRET", text="REGRET", fare=185)}
    )
    test_client = make_test_client(handler)
    body = _post(test_client).json()

    assert body["success"] is False
    assert body["status"] == "COMPLETED_NOT_CONFIRMED"
    assert body["both_segments_confirmed_available"] is False
    assert body["segments"][0]["status"] == "CONFIRMED_AVAILABLE"
    assert body["segments"][1]["status"] == "NOT_AVAILABLE"
    assert body["segments"][1]["provider_status"] == "REGRET"


def test_rac_segment_is_not_confirmed(make_test_client):
    handler = make_handler(
        availability_responses={("ASK", "DWR"): avail_entry(status="RAC", text="RAC 25/RAC 24", rac_count=25)}
    )
    test_client = make_test_client(handler)
    body = _post(test_client).json()
    assert body["segments"][1]["status"] == "RAC"
    assert body["both_segments_confirmed_available"] is False


def test_waitlist_segment_is_not_confirmed(make_test_client):
    handler = make_handler(
        availability_responses={("MYS", "ASK"): avail_entry(status="WAITLIST", text="GNWL21/WL8", waitlist_count=8)}
    )
    test_client = make_test_client(handler)
    body = _post(test_client).json()
    assert body["segments"][0]["status"] == "WAITLIST"
    assert body["both_segments_confirmed_available"] is False


def test_unknown_provider_status_is_not_confirmed(make_test_client):
    handler = make_handler(
        availability_responses={("MYS", "ASK"): avail_entry(status="SOMETHING_NEW", text="SOMETHING_NEW")}
    )
    test_client = make_test_client(handler)
    body = _post(test_client).json()
    assert body["segments"][0]["status"] == "UNKNOWN"
    assert body["both_segments_confirmed_available"] is False


def test_class_not_offered_is_unknown_with_error(make_test_client):
    handler = make_handler(
        availability_responses={
            ("MYS", "ASK"): [avail_entry(class_code="2S", status="AVAILABLE", text="AVAILABLE-9")]
        }
    )
    test_client = make_test_client(handler)
    body = _post(test_client).json()
    assert body["segments"][0]["status"] == "UNKNOWN"
    assert body["errors"][0]["code"] == "CLASS_NOT_OFFERED"
    assert body["success"] is False


# ------------------------------------------------- schedule validation


def test_invalid_station_sequence_reversed(make_test_client):
    """Test: invalid station sequence (segment 1 in reverse direction)."""
    test_client = make_test_client(make_handler())
    body = _post(test_client, from_station_1="ASK", to_station_1="MYS").json()

    assert body["status"] == "SCHEDULE_INVALID"
    assert body["success"] is False
    assert body["schedule_validation"]["first_segment_valid"] is False
    # Availability checks are skipped to conserve API credits.
    assert body["segments"] == []
    assert body["api_usage"]["requests_sent"] == 1


def test_station_not_on_route(make_test_client):
    test_client = make_test_client(make_handler())
    body = _post(test_client, to_station_2="ZZZZ").json()

    assert body["status"] == "SCHEDULE_INVALID"
    assert body["schedule_validation"]["stations_on_route"] is False
    assert "ZZZZ" in body["schedule_validation"]["missing_stations"]
    assert body["api_usage"]["requests_sent"] == 1


def test_second_segment_starting_before_first_ends(make_test_client):
    """Segment 2 must not overlap segment 1 (must start at/after its end)."""
    test_client = make_test_client(make_handler())
    body = _post(test_client, from_station_2="MYS", to_station_2="UBL").json()

    assert body["status"] == "SCHEDULE_INVALID"
    assert body["schedule_validation"]["first_segment_precedes_second"] is False
    assert body["api_usage"]["requests_sent"] == 1


# ------------------------------------------------------- provider errors


def test_provider_error_on_one_segment(make_test_client):
    """Test: provider failure on one segment -> PROVIDER_ERROR, never 'unavailable'."""

    def failing(query: dict) -> httpx.Response:
        return httpx.Response(500, json=error_envelope("UPSTREAM_UNAVAILABLE", "upstream error", "SERVER", retryable=True))

    handler = make_handler(availability_responses={("ASK", "DWR"): failing})
    test_client = make_test_client(handler)
    body = _post(test_client).json()

    assert body["status"] == "PROVIDER_ERROR"
    assert body["success"] is False
    segment2 = body["segments"][1]
    assert segment2["status"] == "PROVIDER_ERROR"
    assert segment2["status"] != "NOT_AVAILABLE"  # an API error is not unavailability
    assert segment2["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert segment2["error"]["details"]["segment"] == "segment_2"
    # Segment 1 still reported independently.
    assert body["segments"][0]["status"] == "CONFIRMED_AVAILABLE"


def test_timeout_on_segment_is_provider_error(make_test_client):
    def timing_out(query: dict) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    handler = make_handler(availability_responses={("MYS", "ASK"): timing_out})
    test_client = make_test_client(handler)
    body = _post(test_client).json()
    assert body["status"] == "PROVIDER_ERROR"
    assert body["segments"][0]["status"] == "PROVIDER_ERROR"
    assert body["segments"][0]["error"]["category"] == "TIMEOUT"


def test_rate_limit_on_segment_is_provider_error(make_test_client):
    def limited(query: dict) -> httpx.Response:
        return httpx.Response(
            429,
            json=error_envelope("RATE_LIMITED", "rate limit exceeded", "RATE_LIMIT", retryable=True),
            headers={"retry-after": "120"},
        )

    handler = make_handler(availability_responses={("MYS", "ASK"): limited})
    test_client = make_test_client(handler)
    body = _post(test_client).json()
    assert body["status"] == "PROVIDER_ERROR"
    assert body["segments"][0]["error"]["category"] == "RATE_LIMIT"
    assert body["segments"][0]["status"] != "NOT_AVAILABLE"


def test_malformed_availability_payload_is_provider_error(make_test_client):
    """Test: unexpected provider response shape."""

    def malformed(query: dict) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"foo": "bar"}}, headers=usage_headers("req_test_bad"))

    handler = make_handler(availability_responses={("ASK", "DWR"): malformed})
    test_client = make_test_client(handler)
    body = _post(test_client).json()
    assert body["status"] == "PROVIDER_ERROR"
    assert body["segments"][1]["error"]["code"] == "MALFORMED_AVAILABILITY"


def test_schedule_provider_error_returns_502(make_test_client):
    def auth_failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=error_envelope("INVALID_API_KEY", "Unauthorized", "AUTH"))

    test_client = make_test_client(make_handler(schedule_responder=auth_failure))
    response = _post(test_client)
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "PROVIDER_ERROR"
    assert body["errors"][0]["category"] == "AUTH"
    assert body["segments"] == []


def test_missing_api_key_returns_503_with_helpful_message(make_test_client):
    test_client = make_test_client(make_handler(), api_key=None)
    response = _post(test_client)
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "CONFIG_ERROR"
    assert ".env" in body["message"]
    assert body["errors"][0]["category"] == "CONFIG"
    # No availability claims whatsoever.
    assert body["segments"] == []
    assert body["both_segments_confirmed_available"] is False


# ------------------------------------------------- request validation


def test_invalid_travel_class_rejected(make_test_client):
    test_client = make_test_client(make_handler())
    response = _post(test_client, travel_class="XX")
    assert response.status_code == 422


def test_invalid_quota_rejected(make_test_client):
    test_client = make_test_client(make_handler())
    response = _post(test_client, quota="ZZ")
    assert response.status_code == 422


def test_past_journey_date_rejected(make_test_client):
    test_client = make_test_client(make_handler())
    response = _post(test_client, journey_date="2020-01-01")
    assert response.status_code == 422


def test_same_from_and_to_station_rejected(make_test_client):
    test_client = make_test_client(make_handler())
    response = _post(test_client, to_station_1="MYS")
    assert response.status_code == 422


def test_short_train_number_rejected(make_test_client):
    test_client = make_test_client(make_handler())
    response = _post(test_client, train_number="12")
    assert response.status_code == 422


def test_availability_checked_at_uses_latest_check(make_test_client):
    test_client = make_test_client(make_handler())
    body = _post(test_client).json()
    assert body["availability_checked_at"] >= tomorrow_ist()[:4]  # UTC ISO timestamp


# ------------------------------------------------------------ system routes


def test_health_endpoint_reports_configuration_state(make_test_client):
    test_client = make_test_client(make_handler())
    response = test_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["railcore_configured"] is True


def test_config_status_never_exposes_the_key(make_test_client):
    test_client = make_test_client(make_handler())
    response = test_client.get("/api/v1/config/status")
    body = response.json()
    assert body["api_key_configured"] is True
    assert "rk_live" not in response.text
    assert "test-key" not in response.text
