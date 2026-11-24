"""Same-train combination search engine tests (Phase 2).

Covers POST /api/v1/journeys/same-train/search end-to-end (validation,
service, planner, normalizer) against a mocked RailCore transport, and
the engine service directly for budget scenarios:

- one intermediate station / multiple intermediate stations
- all segments confirmed
- one segment unavailable
- RAC and waitlist rejection
- invalid station order
- overnight journeys (per-segment boarding dates, day offsets)
- duplicate combinations and query deduplication
- API errors, rate limits and in-place item failures
- fare calculation (sum, missing fare, direct comparison)
- search budgets (requests, credits, enumeration cap, result cap)
- ranking preferences, candidate skip reasons, validation errors
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from app.core.config import get_settings
from app.providers.base import InternalAvailabilityStatus
from app.schemas.journey_search import SameTrainSearchRequest
from app.services.same_train_combination_service import SameTrainCombinationService
from app.services.search_budgets import SearchBudgets
from app.services.segment_query_planner import SegmentQueryPlanner

from tests.conftest import (
    ALL_DAYS,
    FARES_ALL_AVAILABLE,
    TRAIN_ALL_AVAILABLE,
    TRAIN_ALL_WAITLIST,
    TRAIN_NO_SL_CLASS,
    TRAIN_NOT_RUNNING,
    TRAIN_ONE_LEG_UNAVAILABLE,
    TRAIN_SINGLE_COMBO,
    WEEKDAY_CODES,
    all_available_map,
    avail_entry_for,
    error_envelope,
    journey_body,
    make_client,
    make_handler,
    route_trains_payload,
    schedule_payload_for,
    tomorrow_ist,
)

SEARCH_URL = "/api/v1/journeys/same-train/search"

# Every station pair inside the MYS..DWR window (6 stops -> 15 pairs).
ALL_PAIRS = list(FARES_ALL_AVAILABLE.keys())


@pytest.fixture(autouse=True)
def _clear_planner_caches():
    """Isolate process-wide schedule/discovery caches between tests."""
    SegmentQueryPlanner.clear_shared_caches()
    yield
    SegmentQueryPlanner.clear_shared_caches()


# ------------------------------------------------------------ scenario setup


def full_availability_map(
    train: str,
    *,
    default_status: str = "NOT_AVAILABLE",
    overrides: dict[tuple[str, str], dict] | None = None,
) -> dict:
    """Availability for every window pair; unspecified pairs get default_status."""
    overrides = overrides or {}
    mapping: dict = {}
    for pair in ALL_PAIRS:
        mapping[(train, pair[0], pair[1])] = overrides.get(pair) or avail_entry_for(
            pair, status=default_status
        )
    return mapping


def scenario_client(
    *,
    trains: list[str],
    availability: dict,
    schedules: dict[str, dict] | None = None,
    call_log: list | None = None,
    **handler_kwargs,
):
    """Build (railcore_client, handler) for a multi-train search scenario."""
    schedule_map = schedules or {t: schedule_payload_for(t) for t in trains}
    handler = make_handler(
        route_trains=route_trains_payload(trains),
        schedules=schedule_map,
        availability_by_train=availability,
        call_log=call_log,
        **handler_kwargs,
    )
    return make_client(handler)


def run_service(client, budgets: SearchBudgets | None = None, **body_overrides):
    """Run the engine service directly (budget injection without env changes)."""
    service = SameTrainCombinationService(client, budgets)
    request = SameTrainSearchRequest(**journey_body(**body_overrides))
    return service.run(request)


# =================================================================
# Required scenario 1: one intermediate station
# =================================================================


def test_one_intermediate_station(make_test_client):
    """Exactly one valid split point yields exactly one combination."""
    availability = full_availability_map(
        TRAIN_SINGLE_COMBO,
        overrides={
            ("MYS", "ASK"): avail_entry_for(("MYS", "ASK"), fare=165),
            ("ASK", "DWR"): avail_entry_for(("ASK", "DWR"), fare=250),
            ("MYS", "DWR"): avail_entry_for(("MYS", "DWR"), status="RAC", fare=400),
        },
    )
    client = scenario_client(trains=[TRAIN_SINGLE_COMBO], availability=availability)
    test_client = make_test_client(make_handler())  # placeholder, see below
    test_client.app.dependency_overrides.clear()

    body, status = run_service(client)
    assert status == 200
    assert body["status"] == "COMPLETED"
    assert body["success"] is True
    assert body["total_results"] == 1

    result = body["results"][0]
    assert result["segment_count"] == 2
    assert result["seat_changes"] == 1
    assert [s["from_station"]["code"] for s in result["segments"]] == ["MYS", "ASK"]
    assert [s["to_station"]["code"] for s in result["segments"]] == ["ASK", "DWR"]
    assert result["combination_id"] == f"{TRAIN_SINGLE_COMBO}-{tomorrow_ist()}-MYS-ASK-DWR"
    # The direct (single-segment) journey is NOT a "combination" result.
    assert all(r["segment_count"] >= 2 for r in body["results"])


# =================================================================
# Required scenario 2: multiple intermediate stations
# =================================================================


def test_multiple_intermediate_stations(make_test_client):
    """With every pair available, all 15 partitions (2..5 segments) are found."""
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, status = run_service(client)
    assert status == 200
    assert body["status"] == "COMPLETED"
    assert body["total_results"] == 15

    ids = {r["combination_id"] for r in body["results"]}
    assert len(ids) == 15  # no duplicates (also scenario 8)

    # Segment-count distribution: C(4,1)=4 two-segment, C(4,2)=6 three,
    # C(4,3)=4 four, C(4,4)=1 five.
    by_segments: dict[int, int] = {}
    for result in body["results"]:
        by_segments[result["segment_count"]] = by_segments.get(result["segment_count"], 0) + 1
    assert by_segments == {2: 4, 3: 6, 4: 4, 5: 1}

    # A 5-segment (4 intermediate stations) combination exists.
    five = next(r for r in body["results"] if r["segment_count"] == 5)
    assert [s["from_station"]["code"] for s in five["segments"]] == [
        "MYS", "KRNR", "HAS", "ASK", "UBL",
    ]

    # A specific 3-segment combination exists.
    assert f"{TRAIN_ALL_AVAILABLE}-{tomorrow_ist()}-MYS-ASK-UBL-DWR" in ids


# =================================================================
# Required scenario 3: all segments confirmed
# =================================================================


def test_all_segments_confirmed(make_test_client):
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, _ = run_service(client)
    assert body["success"] is True
    for result in body["results"]:
        assert result["all_segments_confirmed_available"] is True
        for segment in result["segments"]:
            assert segment["status"] == InternalAvailabilityStatus.CONFIRMED_AVAILABLE.value
            assert segment["provider_status"] == "AVAILABLE"
            assert segment["available_count"] > 0
            assert segment.get("error") is None  # exclude_none drops the key

    # Completeness: full search, nothing truncated.
    completeness = body["search_completeness"]
    assert completeness["complete"] is True
    assert completeness["incomplete_reasons"] == []
    assert completeness["combinations_found"] == 15


# =================================================================
# Required scenario 4: one segment unavailable
# =================================================================


def test_one_segment_unavailable_excludes_combinations():
    """ASK->DWR NOT AVAILABLE: combinations through it disappear, others stay."""
    availability = full_availability_map(
        TRAIN_ONE_LEG_UNAVAILABLE,
        overrides={
            ("MYS", "KRNR"): avail_entry_for(("MYS", "KRNR"), fare=60),
            ("MYS", "ASK"): avail_entry_for(("MYS", "ASK"), fare=165),
            ("MYS", "UBL"): avail_entry_for(("MYS", "UBL"), fare=340),
            ("MYS", "DWR"): avail_entry_for(("MYS", "DWR"), status="WAITLIST", fare=400),
            ("KRNR", "DWR"): avail_entry_for(("KRNR", "DWR"), fare=350),
            ("ASK", "DWR"): avail_entry_for(("ASK", "DWR"), status="NOT_AVAILABLE", fare=250),
            ("UBL", "DWR"): avail_entry_for(("UBL", "DWR"), fare=75),
        },
    )
    client = scenario_client(trains=[TRAIN_ONE_LEG_UNAVAILABLE], availability=availability)

    body, status = run_service(client)
    assert status == 200
    ids = {r["combination_id"] for r in body["results"]}

    # The unavailable leg disqualifies MYS->ASK->DWR and MYS->ASK->UBL->DWR.
    assert f"{TRAIN_ONE_LEG_UNAVAILABLE}-{tomorrow_ist()}-MYS-ASK-DWR" not in ids
    # Valid alternatives survive.
    assert f"{TRAIN_ONE_LEG_UNAVAILABLE}-{tomorrow_ist()}-MYS-KRNR-DWR" in ids
    assert f"{TRAIN_ONE_LEG_UNAVAILABLE}-{tomorrow_ist()}-MYS-UBL-DWR" in ids
    assert body["total_results"] == 2

    # The direct segment reference reflects the waitlisted direct ticket.
    direct = body["candidates"][0]["direct_segment"]
    assert direct["status"] == InternalAvailabilityStatus.WAITLIST.value
    assert direct["total_fare"] == 400


# =================================================================
# Required scenario 5: RAC and waitlist rejection
# =================================================================


def test_rac_and_waitlist_never_qualify():
    """A train where everything is RAC/waitlist yields nothing - correctly."""
    overrides = {
        ("MYS", "ASK"): avail_entry_for(("MYS", "ASK"), status="RAC"),
        ("MYS", "DWR"): avail_entry_for(("MYS", "DWR"), status="WAITLIST"),
        ("MYS", "UBL"): avail_entry_for(("MYS", "UBL"), status="WAITLIST"),
        ("KRNR", "DWR"): avail_entry_for(("KRNR", "DWR"), status="RAC"),
        ("UBL", "DWR"): avail_entry_for(("UBL", "DWR"), status="WAITLIST"),
        ("ASK", "DWR"): avail_entry_for(("ASK", "DWR"), status="RAC"),
    }
    availability = full_availability_map(TRAIN_ALL_WAITLIST, overrides=overrides)
    client = scenario_client(trains=[TRAIN_ALL_WAITLIST], availability=availability)

    body, status = run_service(client)
    assert status == 200
    assert body["status"] == "COMPLETED_NO_COMBINATIONS"
    assert body["success"] is False
    assert body["results"] == []
    assert body["total_results"] == 0

    # The search itself completed (budgets fine): unavailability is a fact,
    # not a budget artifact.
    assert body["search_completeness"]["complete"] is True
    report = body["candidates"][0]
    assert report["processed"] is True
    assert report["combinations_found"] == 0
    assert report["direct_segment"]["status"] == InternalAvailabilityStatus.WAITLIST.value


# =================================================================
# Required scenario 6: invalid station order / stations not on route
# =================================================================


def test_invalid_station_order_skips_train():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, status = run_service(client, from_station="DWR", to_station="MYS")
    assert status == 200
    assert body["status"] == "COMPLETED_NO_COMBINATIONS"
    assert body["results"] == []

    report = body["candidates"][0]
    assert report["skip_reason"] == "STATION_ORDER_INVALID"
    assert report["processed"] is True
    # No availability queries were wasted on an impossible direction.
    assert report["segments_checked"] == 0


def test_station_not_on_route_skips_train():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, _ = run_service(client, to_station="NDLS")
    report = body["candidates"][0]
    assert report["skip_reason"] == "STATIONS_NOT_ON_ROUTE"
    assert "NDLS" in report["skip_detail"]
    assert body["results"] == []


def test_request_validation_rejects_bad_input(make_test_client):
    test_client = make_test_client(make_handler())
    # Same station
    response = test_client.post(SEARCH_URL, json=journey_body(from_station="MYS", to_station="MYS"))
    assert response.status_code == 422
    # Bad class
    response = test_client.post(SEARCH_URL, json=journey_body(travel_class="XX"))
    assert response.status_code == 422
    # Past date
    response = test_client.post(
        SEARCH_URL, json=journey_body(journey_date=(date.today() - timedelta(days=1)).isoformat())
    )
    assert response.status_code == 422
    # Beyond booking window
    response = test_client.post(
        SEARCH_URL, json=journey_body(journey_date=(date.today() + timedelta(days=90)).isoformat())
    )
    assert response.status_code == 422
    # Bad ranking preference
    response = test_client.post(SEARCH_URL, json=journey_body(ranking_preference="PRETTIEST"))
    assert response.status_code == 422


# =================================================================
# Required scenario 7: overnight journeys
# =================================================================


def test_overnight_journey_dates_and_duration():
    """Day-2 boardings are queried with the advanced calendar date."""
    call_log: list = []
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(
        trains=[TRAIN_ALL_AVAILABLE], availability=availability, call_log=call_log
    )

    journey_date = tomorrow_ist()
    next_day = (date.fromisoformat(journey_date) + timedelta(days=1)).isoformat()

    body, _ = run_service(client)
    assert body["status"] == "COMPLETED"

    result = next(
        r for r in body["results"] if r["combination_id"].endswith("MYS-ASK-DWR")
    )
    assert result["overnight"] is True
    assert result["departure"]["time"] == "22:45"
    assert result["departure"]["day"] == 1
    assert result["arrival"]["time"] == "06:43"
    assert result["arrival"]["day"] == 2
    # 22:45 day 1 -> 06:43 day 2 = 478 minutes (NOT 468 from clock math).
    assert result["total_duration_minutes"] == 478

    # Per-segment boarding dates: MYS (day 1) -> journey date; ASK (day 2) -> +1.
    assert result["segments"][0]["boarding_query_date"] == journey_date
    assert result["segments"][0]["departure_day"] == 1
    assert result["segments"][1]["boarding_query_date"] == next_day
    assert result["segments"][1]["departure_day"] == 2
    assert result["train_run_origin_departure_date"] == journey_date

    # The provider actually received the advanced date for the ASK->DWR item.
    batch_items = [
        item
        for entry in call_log
        if entry["path"] == "/availability/seats/batch"
        for item in entry["items"]
    ]
    ask_dwr = next(i for i in batch_items if i["from"] == "ASK" and i["to"] == "DWR")
    assert ask_dwr["date"] == next_day
    assert ask_dwr["train_number"] == TRAIN_ALL_AVAILABLE
    assert ask_dwr["quota"] == "GN"
    assert ask_dwr["classes"] == ["SL"]

    # A day-1 boarding station keeps the requested date.
    krnr_has = next(i for i in batch_items if i["from"] == "KRNR" and i["to"] == "HAS")
    assert krnr_has["date"] == journey_date

    # All items belong to the same train run.
    assert {i["train_number"] for i in batch_items} == {TRAIN_ALL_AVAILABLE}


# =================================================================
# Required scenario 8: duplicate combinations / query dedup
# =================================================================


def test_no_duplicate_combinations_and_query_dedup():
    call_log: list = []
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(
        trains=[TRAIN_ALL_AVAILABLE], availability=availability, call_log=call_log
    )

    body, _ = run_service(client)
    ids = [r["combination_id"] for r in body["results"]]
    assert len(ids) == len(set(ids)) == 15

    # Every (train, from, to) pair was sent to the provider at most once,
    # although most pairs appear in many combinations (e.g. ASK->DWR is in
    # 8 of the 15 combinations).
    batch_items = [
        (i["train_number"], i["from"], i["to"])
        for entry in call_log
        if entry["path"] == "/availability/seats/batch"
        for i in entry["items"]
    ]
    assert len(batch_items) == len(set(batch_items)) == 15

    completeness = body["search_completeness"]
    assert completeness["distinct_availability_queries"] == 15
    assert completeness["segments_checked"] == 15
    assert completeness["deduplicated_queries"] == 0  # nothing was re-asked


def test_availability_query_dedup_across_bfs_rounds():
    """A memoized pair is never re-sent even when requested twice."""
    availability = full_availability_map(
        TRAIN_SINGLE_COMBO,
        overrides={
            ("MYS", "ASK"): avail_entry_for(("MYS", "ASK"), fare=165),
            ("ASK", "DWR"): avail_entry_for(("ASK", "DWR"), fare=250),
        },
    )
    # Reachable stop ASK discovered in round 1; round 2 expands it. The
    # (MYS, ASK) pair from round 1 must not be re-sent in round 2.
    client = scenario_client(trains=[TRAIN_SINGLE_COMBO], availability=availability)
    body, _ = run_service(client)
    assert body["total_results"] == 1
    assert body["search_completeness"]["distinct_availability_queries"] == 7


# =================================================================
# Required scenario 9: API errors and rate limits
# =================================================================


def test_discovery_provider_error_returns_502(make_test_client):
    handler = make_handler(
        route_trains_responder=lambda query: httpx.Response(
            500, json=error_envelope("UPSTREAM_UNAVAILABLE", "upstream down", "SERVER", retryable=True)
        )
    )
    test_client = make_test_client(handler)
    response = test_client.post(SEARCH_URL, json=journey_body())
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "PROVIDER_ERROR"
    assert body["results"] == []
    assert body["success"] is False
    assert body["errors"]


def test_no_trains_found_returns_clean_empty_result(make_test_client):
    test_client = make_test_client(make_handler())  # no route_trains -> 404
    response = test_client.post(SEARCH_URL, json=journey_body())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "NO_CANDIDATE_TRAINS"
    assert body["results"] == []
    assert body["success"] is False


def test_discovery_malformed_response_is_provider_error(make_test_client):
    malformed = {"success": True, "data": {"trains": "not-a-list"}, "meta": {}}
    handler = make_handler(route_trains=malformed)
    test_client = make_test_client(handler)
    response = test_client.post(SEARCH_URL, json=journey_body())
    assert response.status_code == 502
    assert response.json()["status"] == "PROVIDER_ERROR"


def test_rate_limit_mid_search_is_partial_provider_error():
    """A 429 on the second availability batch -> PARTIAL, never 'unavailable'.

    Round 1 (all MYS->X pairs) succeeds; round 2 (expansion) is rate
    limited. No complete combination exists from round-1 pairs alone, so
    results stay empty - and the response must say WHY.
    """
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    base_handler = make_handler(
        route_trains=route_trains_payload([TRAIN_ALL_AVAILABLE]),
        schedules={TRAIN_ALL_AVAILABLE: schedule_payload_for(TRAIN_ALL_AVAILABLE)},
        availability_by_train=availability,
    )
    batch_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/availability/seats/batch"):
            batch_calls["count"] += 1
            if batch_calls["count"] >= 2:
                return httpx.Response(
                    429,
                    json=error_envelope("RATE_LIMITED", "rate limit exceeded", "RATE_LIMIT", retryable=True),
                    headers={"retry-after": "0"},
                )
        return base_handler(request)

    client = make_client(handler)
    body, status = run_service(client)
    assert status == 200
    assert body["status"] == "PARTIAL_PROVIDER_ERROR"
    assert body["success"] is False
    assert body["results"] == []
    assert "PROVIDER_ERROR" in body["search_completeness"]["incomplete_reasons"]
    assert body["search_completeness"]["complete"] is False
    # The provider error is surfaced, never translated to NOT_AVAILABLE.
    assert any(e.get("code") == "RATE_LIMITED" for e in body["errors"])
    # Round-1 queries were answered (5 segments sent successfully); the
    # rate-limited round-2 attempt is recorded on the train report.
    assert body["search_completeness"]["segments_checked"] == 5
    assert body["candidates"][0]["segments_checked"] == 15  # 5 answered + 10 attempted


def test_schedule_fetch_failure_skips_train_as_unverified():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    handler = make_handler(
        route_trains=route_trains_payload([TRAIN_ALL_AVAILABLE]),
        schedule_responder=lambda request: httpx.Response(
            500, json=error_envelope("UPSTREAM_UNAVAILABLE", "upstream down", "SERVER", retryable=True)
        ),
        availability_by_train=availability,
    )
    client = make_client(handler)
    body, status = run_service(client)
    assert status == 200
    assert body["status"] == "PARTIAL_PROVIDER_ERROR"
    assert body["results"] == []
    report = body["candidates"][0]
    assert report["skip_reason"] == "SCHEDULE_FETCH_FAILED"
    assert "unverified, not unavailable" in report["skip_detail"]
    assert report["errors"]


def test_batch_item_failure_excludes_segment_without_unavailability_claim():
    """An in-place BOOKING_NOT_OPEN item is UNKNOWN, never unavailable."""
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(
        trains=[TRAIN_ALL_AVAILABLE],
        availability=availability,
        batch_item_failures={(TRAIN_ALL_AVAILABLE, "ASK", "DWR")},
    )
    body, _ = run_service(client)
    ids = {r["combination_id"] for r in body["results"]}
    assert f"{TRAIN_ALL_AVAILABLE}-{tomorrow_ist()}-MYS-ASK-DWR" not in ids
    # Combinations avoiding the failed segment still exist.
    assert f"{TRAIN_ALL_AVAILABLE}-{tomorrow_ist()}-MYS-KRNR-DWR" in ids
    # The failure is surfaced as an error, not silently swallowed.
    assert any(e.get("code") == "BOOKING_NOT_OPEN" for e in body["errors"])


# =================================================================
# Required scenario 10: fare calculation
# =================================================================


def test_fare_sum_and_direct_comparison():
    availability = full_availability_map(
        TRAIN_ONE_LEG_UNAVAILABLE,
        overrides={
            ("MYS", "KRNR"): avail_entry_for(("MYS", "KRNR"), fare=60),
            ("MYS", "ASK"): avail_entry_for(("MYS", "ASK"), fare=165),
            ("MYS", "UBL"): avail_entry_for(("MYS", "UBL"), fare=340),
            ("MYS", "DWR"): avail_entry_for(("MYS", "DWR"), status="WAITLIST", fare=400),
            ("KRNR", "DWR"): avail_entry_for(("KRNR", "DWR"), fare=350),
            ("UBL", "DWR"): avail_entry_for(("UBL", "DWR"), fare=75),
        },
    )
    client = scenario_client(trains=[TRAIN_ONE_LEG_UNAVAILABLE], availability=availability)

    body, _ = run_service(client)
    by_id = {r["combination_id"]: r for r in body["results"]}

    combo = by_id[f"{TRAIN_ONE_LEG_UNAVAILABLE}-{tomorrow_ist()}-MYS-KRNR-DWR"]
    assert combo["total_fare"] == 410  # 60 + 350
    assert combo["fares_complete"] is True
    assert combo["segments_missing_fare"] == []
    assert [s["total_fare"] for s in combo["segments"]] == [60, 350]

    # Direct through-route fare comparison (segmented - direct = +10).
    comparison = combo["fare_comparison"]
    assert comparison["direct_total_fare"] == 400
    assert comparison["direct_segment_status"] == InternalAvailabilityStatus.WAITLIST.value
    assert comparison["difference"] == 10
    assert comparison["difference_percent"] == 2.5
    assert "cost more" in comparison["note"]


def test_missing_segment_fare_is_reported_never_invented():
    overrides = {
        pair: avail_entry_for(pair, fare=fare)
        for pair, fare in FARES_ALL_AVAILABLE.items()
    }
    overrides[("ASK", "DWR")] = avail_entry_for(("ASK", "DWR"), no_fare=True)
    availability = full_availability_map(TRAIN_ALL_AVAILABLE, overrides=overrides)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, _ = run_service(client)
    combo = next(
        r for r in body["results"] if r["combination_id"].endswith("MYS-ASK-DWR")
    )
    assert combo.get("total_fare") is None  # exclude_none drops the key
    assert combo["fares_complete"] is False
    assert combo["segments_missing_fare"] == [2]  # segment 2 (ASK->DWR)
    # Availability is still confirmed; only the fare is unknown.
    assert all(
        s["status"] == InternalAvailabilityStatus.CONFIRMED_AVAILABLE.value
        for s in combo["segments"]
    )
    # Fare comparison cannot be computed without the segmented total.
    assert combo["fare_comparison"].get("difference") is None


# =================================================================
# Required scenario 11: search budgets
# =================================================================


def test_request_budget_exhaustion_is_reported_as_partial():
    """Discovery + schedule consume the whole request budget: partial search."""
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    budgets = SearchBudgets(max_api_requests=2, max_credits=50)
    body, status = run_service(client, budgets=budgets)
    assert status == 200
    assert body["status"] == "PARTIAL_BUDGET_EXHAUSTED"
    assert body["success"] is False
    assert body["results"] == []
    completeness = body["search_completeness"]
    assert completeness["complete"] is False
    assert "BUDGET_REQUESTS_EXHAUSTED" in completeness["incomplete_reasons"]
    assert completeness["budget"]["requests_used"] == 2
    assert completeness["budget"]["requests_max"] == 2
    assert completeness["segments_checked"] == 0
    # The message must say the search was NOT exhaustive.
    assert "NOT all possibilities" in body["message"] or "budget" in body["message"].lower()


def test_credit_budget_exhaustion_trims_batches():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    # Discovery(1) + schedule(1) = 2 credits; one more item fits.
    budgets = SearchBudgets(max_api_requests=40, max_credits=3)
    body, _ = run_service(client, budgets=budgets)
    assert body["status"] == "PARTIAL_BUDGET_EXHAUSTED"
    assert "BUDGET_CREDITS_EXHAUSTED" in body["search_completeness"]["incomplete_reasons"]
    assert body["search_completeness"]["budget"]["credits_used_estimate"] == 3
    assert body["search_completeness"]["segments_checked"] == 1


def test_enumeration_cap_is_reported():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    budgets = SearchBudgets(max_combinations_per_train=3)
    body, _ = run_service(client, budgets=budgets)
    assert body["total_results"] == 3
    assert body["results_truncated"] is True
    assert body["candidates"][0]["combinations_truncated"] is True
    # Fewest-segment combinations survive the cap (iterative deepening).
    assert all(r["segment_count"] == 2 for r in body["results"])
    # The search itself finished exploring availability.
    assert body["search_completeness"]["complete"] is True


def test_results_cap_is_reported():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    budgets = SearchBudgets(max_results=2)
    body, _ = run_service(client, budgets=budgets)
    assert body["total_results"] == 2
    assert body["results_truncated"] is True


def test_train_limit_is_reported():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    second = dict(availability)
    second.update(all_available_map("99909", FARES_ALL_AVAILABLE))
    client = scenario_client(
        trains=[TRAIN_ALL_AVAILABLE, "99909"], availability=second
    )

    budgets = SearchBudgets(max_trains=1)
    body, _ = run_service(client, budgets=budgets)
    assert body["status"] == "PARTIAL_BUDGET_EXHAUSTED"
    assert "TRAIN_LIMIT_REACHED" in body["search_completeness"]["incomplete_reasons"]
    skip_report = next(r for r in body["candidates"] if r["train_number"] == "99909")
    assert skip_report["skip_reason"] == "TRAIN_LIMIT_REACHED"
    assert skip_report["processed"] is False


def test_settings_budgets_are_env_configurable(make_test_client, monkeypatch):
    """The endpoint picks budgets up from environment settings."""
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    handler = make_handler(
        route_trains=route_trains_payload([TRAIN_ALL_AVAILABLE]),
        schedules={TRAIN_ALL_AVAILABLE: schedule_payload_for(TRAIN_ALL_AVAILABLE)},
        availability_by_train=availability,
    )
    test_client = make_test_client(handler)

    monkeypatch.setenv("SEARCH_MAX_API_REQUESTS", "2")
    get_settings.cache_clear()
    try:
        response = test_client.post(SEARCH_URL, json=journey_body())
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "PARTIAL_BUDGET_EXHAUSTED"
        assert body["search_completeness"]["budget"]["requests_max"] == 2
    finally:
        monkeypatch.delenv("SEARCH_MAX_API_REQUESTS", raising=False)
        get_settings.cache_clear()


# =================================================================
# Candidate filtering: class not offered / not running
# =================================================================


def test_class_not_offered_skips_train_without_queries():
    availability = all_available_map(TRAIN_NO_SL_CLASS, FARES_ALL_AVAILABLE)
    availability.update(all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE))
    # The ROUTE payload is what the class pre-filter sees: 99904 carries
    # only CC/2S, so it is skipped before any schedule/availability call.
    route_payload = route_trains_payload([TRAIN_NO_SL_CLASS, TRAIN_ALL_AVAILABLE])
    route_payload["data"]["trains"][0]["classes"] = ["CC", "2S"]
    schedules = {
        TRAIN_NO_SL_CLASS: schedule_payload_for(TRAIN_NO_SL_CLASS, classes=("CC", "2S")),
        TRAIN_ALL_AVAILABLE: schedule_payload_for(TRAIN_ALL_AVAILABLE),
    }
    call_log: list = []
    handler = make_handler(
        route_trains=route_payload,
        schedules=schedules,
        availability_by_train=availability,
        call_log=call_log,
    )
    client = make_client(handler)

    body, _ = run_service(client)
    report = next(r for r in body["candidates"] if r["train_number"] == TRAIN_NO_SL_CLASS)
    assert report["skip_reason"] == "CLASS_NOT_OFFERED"
    assert report["processed"] is False
    # No schedule fetch, no availability query for the skipped train.
    assert not any(
        e.get("train_number") == TRAIN_NO_SL_CLASS
        for e in call_log
        if e["path"] == "/trains/{n}/schedule"
    )
    assert not any(
        i.get("train_number") == TRAIN_NO_SL_CLASS
        for e in call_log
        if e["path"] == "/availability/seats/batch"
        for i in e["items"]
    )
    # The other train still produced results.
    assert body["total_results"] == 15


def test_not_running_on_date_skips_train():
    # The run covering tomorrow's departure from MYS departs the origin
    # tomorrow (src day 1); exclude that weekday from running_days.
    journey_day = date.fromisoformat(tomorrow_ist())
    excluded_weekday = WEEKDAY_CODES[journey_day.weekday()]
    running_days = [d for d in ALL_DAYS if d != excluded_weekday]

    availability = all_available_map(TRAIN_NOT_RUNNING, FARES_ALL_AVAILABLE)
    schedules = {TRAIN_NOT_RUNNING: schedule_payload_for(TRAIN_NOT_RUNNING, running_days=running_days)}
    client = scenario_client(
        trains=[TRAIN_NOT_RUNNING], availability=availability, schedules=schedules
    )

    body, _ = run_service(client)
    report = body["candidates"][0]
    assert report["skip_reason"] == "NOT_RUNNING_ON_DATE"
    assert excluded_weekday in report["skip_detail"]
    assert report["segments_checked"] == 0  # no availability queries wasted


# =================================================================
# Ranking preferences
# =================================================================


def test_ranking_fewest_changes_first():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, _ = run_service(client, ranking_preference="FEWEST_CHANGES")
    assert body["ranked_by"] == "FEWEST_CHANGES"
    segment_counts = [r["segment_count"] for r in body["results"]]
    assert segment_counts == sorted(segment_counts)
    assert segment_counts[0] == 2
    # Ties inside the same segment count break by fare (cheapest first).
    first_two = [r["total_fare"] for r in body["results"] if r["segment_count"] == 2]
    assert first_two == sorted(first_two)


def test_ranking_cheapest_first():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, _ = run_service(client, ranking_preference="CHEAPEST")
    fares = [r["total_fare"] for r in body["results"]]
    assert fares == sorted(fares)
    # Cheapest splits cost 410: MYS->KRNR->DWR (60+350) and
    # MYS->HAS->DWR (120+290) tie; both come first.
    assert body["results"][0]["total_fare"] == 410
    cheapest_ids = {r["combination_id"] for r in body["results"] if r["total_fare"] == 410}
    assert cheapest_ids == {
        f"{TRAIN_ALL_AVAILABLE}-{tomorrow_ist()}-MYS-KRNR-DWR",
        f"{TRAIN_ALL_AVAILABLE}-{tomorrow_ist()}-MYS-HAS-DWR",
    }


def test_ranking_fastest_is_non_decreasing():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, _ = run_service(client, ranking_preference="FASTEST")
    durations = [r["total_duration_minutes"] for r in body["results"]]
    assert durations == sorted(durations)
    assert durations[0] == 478


# =================================================================
# Cross-search caching, usage tracking, endpoint-level checks
# =================================================================


def test_schedule_and_discovery_cached_across_searches():
    """Second identical search reuses schedule + discovery, still re-checks availability."""
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    first, _ = run_service(client)
    second, _ = run_service(client)

    first_usage = first["search_completeness"]
    second_usage = second["search_completeness"]
    # First search: discovery + schedule fetched (cache miss).
    assert first_usage["discovery_cache_hits"] == 0
    assert first_usage["schedule_cache_hits"] == 0
    # Second search: both served from cache -> fewer provider requests.
    assert second_usage["discovery_cache_hits"] == 1
    assert second_usage["schedule_cache_hits"] == 1
    assert second["api_usage"]["requests_sent"] < first["api_usage"]["requests_sent"]
    # Availability is NEVER cached across searches (freshness).
    assert second_usage["segments_checked"] == first_usage["segments_checked"] == 15
    assert second["total_results"] == first["total_results"] == 15


def test_api_usage_and_credits_reported():
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    client = scenario_client(trains=[TRAIN_ALL_AVAILABLE], availability=availability)

    body, _ = run_service(client)
    usage = body["api_usage"]
    assert usage["provider"] == "railcore"
    # 1 discovery + 1 schedule + 2 batch calls = 4 HTTP requests.
    assert usage["requests_sent"] == 4
    assert usage["successful_requests"] == 4
    assert usage["failed_requests"] == 0
    # Estimated credits: 1 + 1 + 5 + 10 = 17 items/calls.
    assert body["search_completeness"]["budget"]["credits_used_estimate"] == 17
    assert body["availability_checked_at"]


def test_endpoint_happy_path_and_disclaimers(make_test_client):
    availability = all_available_map(TRAIN_ALL_AVAILABLE, FARES_ALL_AVAILABLE)
    handler = make_handler(
        route_trains=route_trains_payload([TRAIN_ALL_AVAILABLE]),
        schedules={TRAIN_ALL_AVAILABLE: schedule_payload_for(TRAIN_ALL_AVAILABLE)},
        availability_by_train=availability,
    )
    test_client = make_test_client(handler)

    response = test_client.post(SEARCH_URL, json=journey_body(ranking_preference="CHEAPEST"))
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "COMPLETED"
    assert body["success"] is True
    assert body["total_results"] == 15
    assert body["search"] == {
        "from_station": "MYS",
        "to_station": "DWR",
        "journey_date": tomorrow_ist(),
        "travel_class": "SL",
        "quota": "GN",
        "ranking_preference": "CHEAPEST",
    }

    # Disclaimers: never a booking, ticketing rules unverified.
    notes = " ".join(body["notes"]).lower()
    assert "not booked tickets" in notes
    assert "does not book tickets" in notes
    assert "must be verified" in notes
    assert "never interpreted as unavailability" in notes


def test_openapi_documents_the_endpoint(make_test_client):
    test_client = make_test_client(make_handler())
    schema = test_client.get("/openapi.json").json()
    path = schema["paths"]["/api/v1/journeys/same-train/search"]
    assert "post" in path
    assert path["post"]["summary"]
    description = path["post"]["description"]
    assert "same train" in description.lower()
    assert "never books tickets" in description.lower()
    # Request schema example present.
    example = path["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert example
