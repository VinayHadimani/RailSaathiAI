"""Unified journey orchestration engine tests (Phase 5).

Covers POST /api/v1/journeys/search and the orchestration service
directly, against a fully synthetic rail network (trains 998xx, mirroring
the MYS -> DWR product scenario with fictional schedules and configurable
availability). Every provider response is mocked; no test ever contacts
the real RailCore API and no credits are consumed.

Coverage (per the phase 5 brief):
- all three engines contributing results to one merged answer
- engine subset selection (and request validation of the engines field)
- phase 2 / 3 / 4 normalization (including phase 2 extras and derived
  absolute timestamps)
- overnight datetime conversion (phase 2 semantics) incl. cross-engine
  dedup on overnight boarding dates
- cross-engine deduplication by segment signature; different partitions
  remaining distinct
- confirmed availability enforcement + unverified candidate separation
  (WAITLIST / RAC / NOT_AVAILABLE / UNKNOWN / PROVIDER_ERROR)
- all four ranking preferences over merged sets + missing-fare ranking +
  the single deterministic tie-break
- shared query deduplication (one credit per physical availability item)
- shared request budget, credit budget, wall-clock deadline, budget
  exhaustion BEFORE API requests, counters never reset between engines
- one engine failing while another succeeds (provider failure AND an
  unexpected engine exception)
- API response validation, OpenAPI documentation, config/provider errors
- regression of the three existing engine endpoints
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import Settings
from app.schemas.unified_search import UnifiedSearchRequest
from app.services.search_budgets import UnifiedJourneyBudgets
from app.services.segment_query_planner import SegmentQueryPlanner
from app.services.unified_journey_service import UnifiedJourneyOrchestrationService
from app.services.same_train_combination_service import SameTrainCombinationService

from tests.conftest import (
    error_envelope,
    make_client,
    make_handler,
    tc_avail,
    tc_route_entry,
    tc_routes,
    tc_schedule,
    tc_stop,
    tomorrow_ist,
)

SEARCH_URL = "/api/v1/journeys/search"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
CONFIRMED = "CONFIRMED_AVAILABLE"


@pytest.fixture(autouse=True)
def _clear_planner_caches():
    """Isolate process-wide schedule/discovery caches between tests."""
    SegmentQueryPlanner.clear_shared_caches()
    yield
    SegmentQueryPlanner.clear_shared_caches()


# ================================================================ fixtures

UA = "99801"  # MYS 08:00 -> KRNR 09:10 -> ASK 10:30 -> SBC 12:30 -> UBL 15:00 -> DWR 17:30
UB = "99802"  # ASK 11:35 -> SBC 13:30 -> UBL 16:00 -> DWR 18:30
UC = "99803"  # SBC 14:05 -> UBL 16:30 -> DWR 19:00 (not reachable in the main network)
UO = "99804"  # MYS 22:45 d1 -> KRNR 23:15 d1 -> ASK 01:25 d2 -> UBL 06:10 d2 -> DWR 06:43 d2 (overnight)
MINI = "99805"  # MYS 08:00 -> XST 09:00 -> DWR 12:00 (minimal dedup network)
PART = "99806"  # MYS 08:00 -> AST 09:00 -> BST 10:30 -> DWR 12:00 (partition network)
TIE1 = "99811"  # MYS 08:00 -> XST 09:00 -> DWR 12:00 (identical twin trains)
TIE2 = "99812"


def _d(offset_days: int = 0) -> str:
    return (datetime.now(tz=IST).date() + timedelta(days=1 + offset_days)).isoformat()


def main_schedules() -> dict[str, dict]:
    return {
        UA: tc_schedule(UA, [
            tc_stop(1, "MYS", "Mysuru Junction", None, "08:00", dist=0),
            tc_stop(2, "KRNR", "Krishnarajanagar", "09:10", "09:15", dist=35, halt=5),
            tc_stop(3, "ASK", "Arsikere Junction", "10:30", "10:35", dist=160, halt=5),
            tc_stop(4, "SBC", "Bengaluru", "12:30", "12:35", dist=320, halt=5),
            tc_stop(5, "UBL", "Hubballi Junction", "15:00", "15:05", dist=410, halt=5),
            tc_stop(6, "DWR", "Dharwad", "17:30", None, dist=440),
        ]),
        UB: tc_schedule(UB, [
            tc_stop(1, "ASK", "Arsikere Junction", None, "11:35"),
            tc_stop(2, "SBC", "Bengaluru", "13:30", "13:35", dist=160, halt=5),
            tc_stop(3, "UBL", "Hubballi Junction", "16:00", "16:05", dist=250, halt=5),
            tc_stop(4, "DWR", "Dharwad", "18:30", None, dist=280),
        ]),
        UC: tc_schedule(UC, [
            tc_stop(1, "SBC", "Bengaluru", None, "14:05"),
            tc_stop(2, "UBL", "Hubballi Junction", "16:30", "16:35", dist=90, halt=5),
            tc_stop(3, "DWR", "Dharwad", "19:00", None, dist=120),
        ]),
    }


def overnight_schedules() -> dict[str, dict]:
    return {
        UO: tc_schedule(UO, [
            tc_stop(1, "MYS", "Mysuru Junction", None, "22:45", dist=0),
            tc_stop(2, "KRNR", "Krishnarajanagar", "23:15", "23:16", dist=34, halt=1),
            tc_stop(3, "ASK", "Arsikere Junction", "01:25", "01:27", dist=163, halt=2, day=2),
            tc_stop(4, "UBL", "Hubballi Junction", "06:10", "06:20", dist=408, halt=10, day=2),
            tc_stop(5, "DWR", "Dharwad", "06:43", None, dist=436, day=2),
        ]),
    }


def mini_schedules() -> dict[str, dict]:
    return {
        MINI: tc_schedule(MINI, [
            tc_stop(1, "MYS", "Mysuru Junction", None, "08:00", dist=0),
            tc_stop(2, "XST", "Transfer Station", "09:00", "09:05", dist=60, halt=5),
            tc_stop(3, "DWR", "Dharwad", "12:00", None, dist=300),
        ]),
    }


def partition_schedules() -> dict[str, dict]:
    return {
        PART: tc_schedule(PART, [
            tc_stop(1, "MYS", "Mysuru Junction", None, "08:00", dist=0),
            tc_stop(2, "AST", "Station A", "09:00", "09:05", dist=60, halt=5),
            tc_stop(3, "BST", "Station B", "10:30", "10:35", dist=160, halt=5),
            tc_stop(4, "DWR", "Dharwad", "12:00", None, dist=300),
        ]),
    }


def tie_schedules() -> dict[str, dict]:
    stops = [
        tc_stop(1, "MYS", "Mysuru Junction", None, "08:00", dist=0),
        tc_stop(2, "XST", "Transfer Station", "09:00", "09:05", dist=60, halt=5),
        tc_stop(3, "DWR", "Dharwad", "12:00", None, dist=300),
    ]
    return {
        TIE1: tc_schedule(TIE1, stops),
        TIE2: tc_schedule(TIE2, stops),
    }


# Availability for the main network: everything NOT_AVAILABLE by default
# (a definitive dead branch, not unverified noise) with selected overrides.
MAIN_AVAIL: dict[tuple[str, str, str], dict] = {
    (UA, "MYS", "KRNR"): tc_avail(fare=60),
    (UA, "KRNR", "DWR"): tc_avail(fare=350),
    (UA, "MYS", "ASK"): tc_avail(fare=165),
    (UA, "MYS", "DWR"): tc_avail(fare=380),  # direct through-route reference
    (UB, "ASK", "DWR"): tc_avail(fare=210),
}

OVERNIGHT_AVAIL: dict[tuple[str, str, str], dict] = {
    (UO, "MYS", "KRNR"): tc_avail(fare=60),
    (UO, "KRNR", "ASK"): tc_avail(fare=55),
    (UO, "ASK", "DWR"): tc_avail(fare=185),
    (UO, "MYS", "ASK"): tc_avail(fare=120),
    (UO, "KRNR", "DWR"): tc_avail(fare=250),
    (UO, "MYS", "DWR"): tc_avail(fare=295),
}

MINI_AVAIL: dict[tuple[str, str, str], dict] = {
    (MINI, "MYS", "XST"): tc_avail(fare=100),
    (MINI, "XST", "DWR"): tc_avail(fare=120),
    (MINI, "MYS", "DWR"): tc_avail(fare=220),
}

PARTITION_AVAIL: dict[tuple[str, str, str], dict] = {
    (PART, "MYS", "AST"): tc_avail(fare=100),
    (PART, "AST", "BST"): tc_avail(fare=40),
    (PART, "AST", "DWR"): tc_avail(fare=115),
    (PART, "MYS", "BST"): tc_avail(fare=150),
    (PART, "BST", "DWR"): tc_avail(fare=55),
    (PART, "MYS", "DWR"): tc_avail(fare=220),
}

TIE_AVAIL: dict[tuple[str, str, str], dict] = {
    (TIE1, "MYS", "XST"): tc_avail(fare=100),
    (TIE1, "XST", "DWR"): tc_avail(fare=120),
    (TIE1, "MYS", "DWR"): tc_avail(fare=220),
    (TIE2, "MYS", "XST"): tc_avail(fare=100),
    (TIE2, "XST", "DWR"): tc_avail(fare=120),
    (TIE2, "MYS", "DWR"): tc_avail(fare=220),
}


def routes_auto(
    schedules: dict[str, dict],
    forced: dict[tuple[str, str], list[tuple[str, str, str]]] | None = None,
):
    """Discovery responder: every (from, to) pair is answered with the
    schedule-trains that serve it forward - computed from the schedules,
    never a hardcoded list. Unknown pairs answer 404 NO_TRAINS_FOUND
    exactly like the real provider."""

    def responder(query: dict) -> httpx.Response:
        frm, to = query.get("from", ""), query.get("to", "")
        entries = []
        for number, payload in schedules.items():
            stops = payload["data"]["stops"]
            codes = [s["station_code"] for s in stops]
            if frm in codes and to in codes and codes.index(frm) < codes.index(to):
                dep = next(
                    s["departure_time"] for s in stops if s["station_code"] == frm and s["departure_time"]
                )
                arr = next(
                    s["arrival_time"] for s in stops if s["station_code"] == to and s["arrival_time"]
                )
                entries.append(tc_route_entry(number, frm=frm, to=to, dep=dep, arr=arr))
        for train_number, dep, arr in (forced or {}).get((frm, to), []):
            entries.append(tc_route_entry(train_number, frm=frm, to=to, dep=dep, arr=arr))
        if not entries:
            return httpx.Response(
                404,
                json=error_envelope(
                    "NO_TRAINS_FOUND", f"no trains between {frm} and {to}", "CLIENT"
                ),
            )
        return httpx.Response(200, json=tc_routes(frm, to, entries))

    return responder


def full_map(schedules: dict[str, dict], overrides: dict | None = None) -> dict:
    """Availability for EVERY forward pair of every schedule train, with a
    definitive default (NOT_AVAILABLE) so unmapped pairs are dead branches,
    not unverified noise. Selected pairs are overridden."""
    mapping: dict[tuple[str, str, str], dict] = {}
    for number, payload in schedules.items():
        stops = payload["data"]["stops"]
        for i, a in enumerate(stops):
            for b in stops[i + 1:]:
                mapping[(number, a["station_code"], b["station_code"])] = tc_avail(
                    status="NOT_AVAILABLE"
                )
    mapping.update(overrides or {})
    return mapping


def big_unified_budgets(**overrides) -> UnifiedJourneyBudgets:
    params = dict(
        max_api_requests=200,
        max_credits=400,
        time_budget_seconds=30.0,
        max_results=50,
    )
    params.update(overrides)
    return UnifiedJourneyBudgets(**params)


def uni_body(**overrides) -> dict:
    base = {
        "from_station": "MYS",
        "to_station": "DWR",
        "journey_date": tomorrow_ist(),
        "travel_class": "SL",
        "quota": "GN",
        "ranking_preference": "CHEAPEST",
    }
    base.update(overrides)
    return base


def uni_run(
    schedules: dict[str, dict],
    availability: dict | None = None,
    *,
    budgets: UnifiedJourneyBudgets | None = None,
    routes_responder=None,
    batch_item_failures=None,
    batch_responder=None,
    call_log: list | None = None,
    **body_overrides,
):
    """Run the unified orchestration service directly against a mocked provider."""
    handler = make_handler(
        route_trains_responder=routes_responder or routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, availability or {}),
        batch_item_failures=batch_item_failures,
        batch_responder=batch_responder,
        call_log=call_log,
    )
    client = make_client(handler)
    service = UnifiedJourneyOrchestrationService(client, budgets or big_unified_budgets())
    request = UnifiedSearchRequest(**uni_body(**body_overrides))
    body, status = service.run(request)
    client.close()
    return body, status


def batch_items(call_log: list) -> list[tuple[str, str, str, str]]:
    """Every availability item actually sent to the provider (ground truth
    for cross-engine query deduplication)."""
    items: list[tuple[str, str, str, str]] = []
    for entry in call_log:
        if entry.get("path") == "/availability/seats/batch":
            for item in entry.get("items", []):
                items.append(
                    (
                        str(item.get("train_number", "")),
                        str(item.get("from", "")),
                        str(item.get("to", "")),
                        str(item.get("date", "")),
                    )
                )
    return items


def all_result_segments(body: dict) -> list[dict]:
    segments = []
    for journey in body.get("results", []) + body.get("unverified_candidates", []):
        segments.extend(journey.get("segments", []))
    return segments


# ===================================================== merging & attribution

def test_all_three_engines_contribute_and_merge():
    call_log: list = []
    body, status = uni_run(main_schedules(), MAIN_AVAIL, call_log=call_log)

    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["success"] is True
    assert body["total_results"] == 2

    # All three engines ran and reported their own verbatim statuses.
    assert [report["engine"] for report in body["per_engine"]] == [
        "SAME_TRAIN", "TRAIN_CHANGE", "MIXED",
    ]
    assert [report["status"] for report in body["per_engine"]] == [
        "COMPLETED", "AVAILABLE_COMBINATION", "AVAILABLE_COMBINATION",
    ]
    assert all(report["results_before_merge"] >= 0 for report in body["per_engine"])
    assert body["per_engine"][0]["results_before_merge"] == 1  # P2: J1
    assert body["per_engine"][1]["results_before_merge"] == 1  # P3: J2
    assert body["per_engine"][2]["results_before_merge"] == 2  # P4: J1 + J2

    # The same physical journeys found by several engines appear once,
    # with every source engine recorded.
    by_first_station = {j["segments"][0]["to_station"]["code"]: j for j in body["results"]}
    j1 = by_first_station["KRNR"]  # same-train MYS->KRNR->DWR
    j2 = by_first_station["ASK"]  # train change MYS->ASK + ASK->DWR
    assert j1["source_engines"] == ["SAME_TRAIN", "MIXED"]
    assert j2["source_engines"] == ["TRAIN_CHANGE", "MIXED"]
    assert set(j1["engine_journey_ids"]) == {"SAME_TRAIN", "MIXED"}
    assert j1["engine_journey_ids"]["SAME_TRAIN"] == j1["journey_id"]
    assert set(j2["engine_journey_ids"]) == {"TRAIN_CHANGE", "MIXED"}
    assert j2["engine_journey_ids"]["TRAIN_CHANGE"] == j2["journey_id"]

    # Deduplication report: 4 collected -> 2 kept, 2 duplicates removed,
    # both cross-engine.
    dedup = body["deduplication"]
    assert dedup["journeys_before_merge"] == 4
    assert dedup["journeys_after_merge"] == 2
    assert dedup["duplicates_removed"] == 2
    assert dedup["cross_engine_duplicates"] == 2
    assert len(dedup["signature_collisions"]) == 2

    # Confirmed availability is enforced over the merged set.
    for journey in body["results"]:
        assert journey["all_segments_confirmed_available"] is True
        assert journey["journey_status"] == "AVAILABLE_COMBINATION"
        for segment in journey["segments"]:
            assert segment["status"] == CONFIRMED
    assert body["unverified_candidates"] == []

    # Direct through trains are reference-only and unioned across engines.
    assert [d["train_number"] for d in body["direct_trains"]] == [UA]
    assert all(d["availability_checked"] is False for d in body["direct_trains"])

    # The whole unified search shares one budget and one api_usage block;
    # the mixed engine needed zero additional provider calls (shared caches
    # and shared availability memo).
    completeness = body["search_completeness"]
    assert completeness["complete"] is True
    assert completeness["incomplete_reasons"] == []
    assert completeness["engines_reporting_incomplete"] == []
    assert completeness["budget"]["requests_used"] == 17
    assert completeness["budget"]["requests_max"] == 200
    assert completeness["budget"]["credits_used_estimate"] == 29
    assert completeness["budget"]["credits_max"] == 400
    assert completeness["segments_checked"] == 15
    assert completeness["distinct_availability_queries"] == 15
    assert completeness["schedule_cache_hits"] >= 1
    assert completeness["discovery_cache_hits"] >= 1
    assert completeness["search_scope"]["engines"] == [
        "SAME_TRAIN", "TRAIN_CHANGE", "MIXED",
    ]
    assert body["api_usage"]["provider"] == "railcore"
    assert body["api_usage"]["requests_sent"] == len(call_log) == 17
    # Header-reported credits only cover the responses whose mocks carry
    # usage headers (schedules + batches; the routes mock carries none).
    assert body["api_usage"]["credits_cost"] == 5
    assert body["api_usage"]["credits_remaining"] == 498
    assert body["availability_checked_at"] is not None
    assert any("does not book tickets" in note for note in body["notes"])
    assert any("deduplicated" in note for note in body["notes"])


def test_engine_subset_selection():
    schedules = main_schedules()

    # Single engine: only the same-train engine runs.
    body, status = uni_run(schedules, MAIN_AVAIL, engines=["SAME_TRAIN"])
    assert status == 200
    assert [report["engine"] for report in body["per_engine"]] == ["SAME_TRAIN"]
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey["source_engines"] == ["SAME_TRAIN"]
    assert journey["journey_type"] == "SAME_TRAIN_SEGMENTS"
    assert dedup_empty(body)

    # Two engines without the same-train engine: the train-change journey
    # comes from both, and the same-train journey is still found by the
    # mixed engine alone (same-run continuation legs).
    body, status = uni_run(
        schedules, MAIN_AVAIL, engines=["TRAIN_CHANGE", "MIXED"],
    )
    assert status == 200
    assert [report["engine"] for report in body["per_engine"]] == ["TRAIN_CHANGE", "MIXED"]
    assert body["total_results"] == 2
    by_first_station = {j["segments"][0]["to_station"]["code"]: j for j in body["results"]}
    assert by_first_station["ASK"]["source_engines"] == ["TRAIN_CHANGE", "MIXED"]
    assert by_first_station["KRNR"]["source_engines"] == ["MIXED"]
    assert by_first_station["KRNR"]["journey_type"] == "SAME_TRAIN_SEGMENTS"
    assert body["deduplication"]["duplicates_removed"] == 1


def dedup_empty(body: dict) -> bool:
    return (
        body["deduplication"]["duplicates_removed"] == 0
        and body["deduplication"]["signature_collisions"] == []
    )


# ============================================================ normalization

def test_p2_normalization_fields():
    body, status = uni_run(main_schedules(), MAIN_AVAIL, engines=["SAME_TRAIN"])
    assert status == 200
    journey = body["results"][0]

    # Unified typing: same-train segments + derived seat-change transition.
    assert journey["journey_type"] == "SAME_TRAIN_SEGMENTS"
    assert journey["train_changes"] == 0
    assert journey["seat_changes"] == 1
    assert journey["segment_count"] == 2
    assert journey["distinct_trains"] == [UA]
    assert journey["distinct_train_count"] == 1
    assert journey["total_transfer_wait_minutes"] == 0
    journey_date = journey["journey_date"]

    # Phase 2 extras are preserved.
    assert journey["train_number"] == UA
    assert journey["train_run_origin_departure_date"] == journey_date
    assert journey["fare_comparison"]["direct_total_fare"] == 380
    assert journey["fare_comparison"]["difference"] == 30
    assert journey["fare_comparison"]["difference_percent"] == 7.9

    # Segment typing + derived absolute timestamps (daytime: same date).
    segment_1, segment_2 = journey["segments"]
    assert segment_1["segment_type"] == "FIRST_SEGMENT"
    assert segment_2["segment_type"] == "SAME_TRAIN_SEGMENT"
    assert segment_1["train_name"] == "MOCK 99801 EXPRESS"
    assert segment_1["departure_datetime"] == f"{journey_date}T08:00:00+05:30"
    assert segment_1["arrival_datetime"] == f"{journey_date}T09:10:00+05:30"
    assert segment_2["departure_datetime"] == f"{journey_date}T09:15:00+05:30"
    assert segment_2["arrival_datetime"] == f"{journey_date}T17:30:00+05:30"
    assert journey["departure_datetime"] == segment_1["departure_datetime"]
    assert journey["arrival_datetime"] == segment_2["arrival_datetime"]

    # Derived seat-change transition (never a transfer).
    (transition,) = journey["transitions"]
    assert transition["transition_type"] == "SAME_TRAIN_SEAT_CHANGE"
    assert transition["is_train_change"] is False
    assert transition["station"]["code"] == "KRNR"
    assert transition["from_train"] == UA
    assert transition["to_train"] == UA
    assert transition["onboard_halt_minutes"] == 5
    assert "wait_minutes" not in transition
    assert "within_min_buffer" not in transition

    # Booking information: RailCore supplies no booking URLs.
    assert "booking_url" not in segment_1


def test_p3_normalization_fields():
    body, status = uni_run(main_schedules(), MAIN_AVAIL, engines=["TRAIN_CHANGE"])
    assert status == 200
    journey = body["results"][0]

    assert journey["journey_type"] == "TRAIN_CHANGE"
    assert journey["train_changes"] == 1
    assert journey["seat_changes"] == 0
    assert journey["segment_count"] == 2
    assert journey["distinct_trains"] == [UA, UB]
    assert journey["distinct_train_count"] == 2
    assert journey["total_fare"] == 375.0
    assert journey["total_duration_minutes"] == 630
    assert journey["total_transfer_wait_minutes"] == 65

    # Segment typing is recomputed to the unified contract.
    segment_1, segment_2 = journey["segments"]
    assert segment_1["segment_type"] == "FIRST_SEGMENT"
    assert segment_2["segment_type"] == "TRAIN_CHANGE"
    assert segment_1["departure_datetime"] == f"{journey['journey_date']}T08:00:00+05:30"
    assert segment_2["arrival_datetime"] == f"{journey['journey_date']}T18:30:00+05:30"

    # Transfers become unified transitions (genuine train changes).
    (transition,) = journey["transitions"]
    assert transition["transition_type"] == "TRAIN_CHANGE"
    assert transition["is_train_change"] is True
    assert transition["station"]["code"] == "ASK"
    assert transition["from_train"] == UA
    assert transition["to_train"] == UB
    assert transition["wait_minutes"] == 65
    assert transition["within_min_buffer"] is True
    assert transition["within_max_wait"] is True
    assert "onboard_halt_minutes" not in transition

    # Phase 2 extras are absent for train-change journeys.
    assert "fare_comparison" not in journey
    assert "train_number" not in journey


def test_p4_normalization_fields():
    body, status = uni_run(main_schedules(), MAIN_AVAIL, engines=["MIXED"])
    assert status == 200
    assert body["total_results"] == 2
    by_first_station = {j["segments"][0]["to_station"]["code"]: j for j in body["results"]}

    same_train = by_first_station["KRNR"]
    train_change = by_first_station["ASK"]

    # Phase 4 shape maps 1:1, attribution is mixed-only, and phase 2
    # extras (fare comparison) genuinely only come from the phase 2 engine.
    assert same_train["source_engines"] == ["MIXED"]
    assert same_train["journey_type"] == "SAME_TRAIN_SEGMENTS"
    assert same_train["seat_changes"] == 1
    assert same_train["train_changes"] == 0
    assert same_train["distinct_trains"] == [UA]
    assert "fare_comparison" not in same_train
    (transition,) = same_train["transitions"]
    assert transition["transition_type"] == "SAME_TRAIN_SEAT_CHANGE"
    assert transition["is_train_change"] is False

    assert train_change["source_engines"] == ["MIXED"]
    assert train_change["journey_type"] == "TRAIN_CHANGE"
    assert train_change["train_changes"] == 1
    assert train_change["seat_changes"] == 0
    assert train_change["distinct_trains"] == [UA, UB]
    (transition,) = train_change["transitions"]
    assert transition["transition_type"] == "TRAIN_CHANGE"
    assert transition["is_train_change"] is True


def test_p2_overnight_datetimes_reuse_engine_semantics():
    body, status = uni_run(overnight_schedules(), OVERNIGHT_AVAIL, engines=["SAME_TRAIN"])
    assert status == 200
    by_partition = {
        tuple(s["from_station"]["code"] for s in j["segments"]): j for j in body["results"]
    }
    journey = by_partition[("MYS", "KRNR", "ASK")]  # MYS->KRNR->ASK->DWR (3 segments)
    journey_date = journey["journey_date"]

    assert journey["overnight"] is True
    segment_1, segment_2, segment_3 = journey["segments"]
    # Boarding dates follow IRCTC semantics: ASK is boarded on day 2.
    assert segment_1["boarding_query_date"] == journey_date
    assert segment_2["boarding_query_date"] == journey_date
    assert segment_3["boarding_query_date"] == _d(1)

    # Derived absolute timestamps anchor on those boarding dates.
    assert segment_1["departure_datetime"] == f"{journey_date}T22:45:00+05:30"
    assert segment_2["departure_datetime"] == f"{journey_date}T23:16:00+05:30"
    assert segment_3["departure_datetime"] == f"{_d(1)}T01:27:00+05:30"
    assert segment_3["arrival_datetime"] == f"{_d(1)}T06:43:00+05:30"
    assert journey["departure_datetime"] == segment_1["departure_datetime"]
    assert journey["arrival_datetime"] == segment_3["arrival_datetime"]

    # The derivation is consistent with the engine's own day-offset duration.
    departure = datetime.fromisoformat(journey["departure_datetime"])
    arrival = datetime.fromisoformat(journey["arrival_datetime"])
    derived_minutes = int((arrival - departure).total_seconds() // 60)
    assert derived_minutes == journey["total_duration_minutes"] == 478


def test_overnight_cross_engine_dedup():
    # The overnight train's same-train journeys are found by BOTH the
    # same-train engine and the mixed engine: the signatures (with the
    # advanced day-2 boarding dates) must match and collapse to one.
    body, status = uni_run(
        overnight_schedules(), OVERNIGHT_AVAIL, engines=["SAME_TRAIN", "MIXED"]
    )
    assert status == 200
    assert body["total_results"] == 3  # the three distinct partitions
    assert body["deduplication"]["journeys_before_merge"] == 6
    assert body["deduplication"]["journeys_after_merge"] == 3
    assert body["deduplication"]["duplicates_removed"] == 3
    for journey in body["results"]:
        assert journey["source_engines"] == ["SAME_TRAIN", "MIXED"]

    # Different partitions remain distinct (not over-deduplicated).
    partitions = {tuple(s["from_station"]["code"] for s in j["segments"]) for j in body["results"]}
    assert partitions == {("MYS", "KRNR"), ("MYS", "KRNR", "ASK"), ("MYS", "ASK")}


# =========================================================== deduplication

def test_cross_engine_dedup_minimal_network():
    # One train, one 2-segment partition: found by the same-train engine
    # and by the mixed engine; the train-change engine finds nothing.
    call_log: list = []
    body, status = uni_run(
        mini_schedules(), MINI_AVAIL, engines=["SAME_TRAIN", "TRAIN_CHANGE", "MIXED"],
        call_log=call_log,
    )
    assert status == 200
    assert body["per_engine"][1]["status"] in ("NO_COMPLETE_JOURNEY", "AVAILABLE_COMBINATION")
    assert body["per_engine"][1]["total_results"] == 0

    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey["source_engines"] == ["SAME_TRAIN", "MIXED"]
    assert journey["engine_journey_ids"]["MIXED"].endswith("DWR")
    assert "@" in journey["engine_journey_ids"]["MIXED"]  # phase 3/4 id format
    assert journey["engine_journey_ids"]["SAME_TRAIN"] == journey["journey_id"]

    assert body["deduplication"]["duplicates_removed"] == 1
    assert body["deduplication"]["cross_engine_duplicates"] == 1
    (collision,) = body["deduplication"]["signature_collisions"]
    assert collision["duplicate_engine"] == "MIXED"
    assert collision["kept_journey_id"] == journey["journey_id"]
    assert collision["signature"] == [
        [MINI, 1, 2, tomorrow_ist()],
        [MINI, 2, 3, tomorrow_ist()],
    ]


def test_shared_query_deduplication_one_credit_per_item():
    # The same physical availability items are needed by the same-train
    # engine AND the mixed engine: the shared planner memo means each is
    # sent to the provider exactly once.
    call_log: list = []
    body, status = uni_run(
        mini_schedules(), MINI_AVAIL, engines=["SAME_TRAIN", "MIXED"],
        call_log=call_log,
    )
    assert status == 200
    items = batch_items(call_log)
    # Exactly the three distinct items of the one train's window, once each
    # (MYS->XST, MYS->DWR reference, XST->DWR) - the mixed engine's identical
    # queries were all memo hits.
    assert len(items) == 3
    assert items.count((MINI, "MYS", "XST", tomorrow_ist())) == 1
    assert items.count((MINI, "XST", "DWR", tomorrow_ist())) == 1
    assert items.count((MINI, "MYS", "DWR", tomorrow_ist())) == 1
    assert body["search_completeness"]["segments_checked"] == 3
    assert body["search_completeness"]["distinct_availability_queries"] == 3

    # Cross-engine on the main network too: the item checked by the
    # same-train engine is not re-sent by the train-change/mixed engines.
    # (A different synthetic network answers the same discovery pair, so
    # the shared caches are cleared between the two runs.)
    SegmentQueryPlanner.clear_shared_caches()
    call_log2: list = []
    body2, status2 = uni_run(main_schedules(), MAIN_AVAIL, call_log=call_log2)
    assert status2 == 200
    items2 = batch_items(call_log2)
    assert items2.count((UA, "MYS", "ASK", tomorrow_ist())) == 1
    assert items2.count((UA, "KRNR", "DWR", tomorrow_ist())) == 1
    assert items2.count((UB, "ASK", "DWR", tomorrow_ist())) == 1


def test_partitions_remain_distinct_after_merge():
    schedules = partition_schedules()
    body, status = uni_run(schedules, PARTITION_AVAIL)
    assert status == 200
    # Three distinct partitions of one train, found by two engines each.
    assert body["total_results"] == 3
    assert body["deduplication"]["journeys_before_merge"] == 6
    assert body["deduplication"]["journeys_after_merge"] == 3
    assert body["deduplication"]["duplicates_removed"] == 3
    ids = {j["journey_id"] for j in body["results"]}
    assert len(ids) == 3
    signatures = {
        tuple(
            (s["train_number"], s["from_station"]["sequence"], s["to_station"]["sequence"])
            for s in j["segments"]
        )
        for j in body["results"]
    }
    assert len(signatures) == 3


# ============================================================== validation

@pytest.mark.parametrize("bad_status", ["WAITLIST", "RAC", "NOT_AVAILABLE"])
def test_definitive_non_available_never_qualifies(bad_status):
    # One confirmed leg becomes WAITLIST / RAC / NOT_AVAILABLE: every
    # journey through it disappears (definitive dead branch) and no
    # returned journey ever carries that status.
    overrides = dict(MAIN_AVAIL)
    overrides[(UA, "MYS", "ASK")] = tc_avail(status=bad_status)
    body, status = uni_run(main_schedules(), overrides)
    assert status == 200
    # J1 (MYS->KRNR->DWR on UA) survives; J2 (through MYS->ASK) is gone.
    assert body["total_results"] == 1
    assert body["results"][0]["segments"][0]["to_station"]["code"] == "KRNR"
    for segment in all_result_segments(body):
        assert segment["status"] == CONFIRMED


def test_unverified_candidates_separated_with_diagnostics():
    # One availability item fails in place (BOOKING_NOT_OPEN -> UNKNOWN):
    # the journeys through it must land in unverified_candidates with their
    # diagnostics preserved, never in results.
    body, status = uni_run(
        main_schedules(), MAIN_AVAIL, batch_item_failures={(UA, "MYS", "ASK")}
    )
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["success"] is True

    # Results keep only the fully confirmed same-train journey.
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey["segments"][0]["to_station"]["code"] == "KRNR"
    assert journey["all_segments_confirmed_available"] is True

    # The train-change journey through the failed item is an unverified
    # candidate with full diagnostics.
    assert body["total_unverified_candidates"] == 1
    candidate = body["unverified_candidates"][0]
    assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
    assert candidate["all_segments_confirmed_available"] is False
    assert candidate["unverified_segment_numbers"] == [1]
    failed_segment = candidate["segments"][0]
    assert failed_segment["status"] == "UNKNOWN"
    assert failed_segment["error"]["code"] == "BOOKING_NOT_OPEN"
    assert failed_segment["error"]["details"]["train_number"] == UA
    # Attribution survives in the unverified channel too.
    assert candidate["source_engines"] == ["TRAIN_CHANGE", "MIXED"]
    assert body["deduplication"]["duplicates_removed_unverified_candidates"] == 1

    # Never a booking claim, and the failure is surfaced as an error.
    assert any(error.get("code") == "BOOKING_NOT_OPEN" for error in body["errors"])


def test_provider_error_is_never_unavailability():
    # The availability batch endpoint fails with 503: every segment becomes
    # PROVIDER_ERROR; journeys are unverified candidates with diagnostics -
    # never results, never NOT_AVAILABLE. (While traversing the unverified
    # legs the engines also hit their own journey caps, so the aggregate
    # status is SEARCH_LIMIT_REACHED - exactly the engines' precedence.)
    def failing_batch(_body):
        return httpx.Response(
            503,
            json=error_envelope("UPSTREAM_FAILURE", "availability backend down", "SERVER", True),
        )

    body, status = uni_run(main_schedules(), MAIN_AVAIL, batch_responder=failing_batch)
    assert status == 200  # provider error != unavailable: not a 502 for candidates
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert body["success"] is False
    assert body["results"] == []
    assert body["total_unverified_candidates"] >= 20

    for candidate in body["unverified_candidates"]:
        assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
        assert candidate["all_segments_confirmed_available"] is False
        for segment in candidate["segments"]:
            assert segment["status"] == "PROVIDER_ERROR"
            assert segment["error"]["code"] == "UPSTREAM_FAILURE"
    # The engines' own statuses reflect the provider error honestly.
    statuses = {report["status"] for report in body["per_engine"]}
    assert "PARTIAL_PROVIDER_ERROR" in statuses
    assert any(error.get("code") == "UPSTREAM_FAILURE" for error in body["errors"])


# ================================================================ ranking

def test_all_four_ranking_preferences_cross_engine():
    schedules = main_schedules()
    # J1: fare 410, duration 570, train_changes 0, seat_changes 1.
    # J2: fare 375, duration 630, train_changes 1, seat_changes 0.
    first_codes = {"CHEAPEST": "ASK", "FASTEST": "KRNR"}

    for preference, expected_first in (
        ("CHEAPEST", "ASK"),
        ("FASTEST", "KRNR"),
        ("FEWEST_TRAIN_CHANGES", "KRNR"),
        ("FEWEST_SEAT_CHANGES", "ASK"),
    ):
        body, status = uni_run(
            schedules, MAIN_AVAIL, ranking_preference=preference
        )
        assert status == 200
        assert body["ranked_by"] == preference
        assert body["total_results"] == 2
        first = body["results"][0]
        assert first["segments"][0]["to_station"]["code"] == expected_first, preference
        # One deterministic order per preference over the MERGED set.
        assert {j["journey_id"] for j in body["results"]} == {
            j["journey_id"] for j in uni_run(schedules, MAIN_AVAIL)[0]["results"]
        }
    assert first_codes["CHEAPEST"] == "ASK" and first_codes["FASTEST"] == "KRNR"


def test_ranking_partitions_and_seat_change_order():
    schedules = partition_schedules()
    # Partitions: [MYS->AST->DWR] 215, [MYS->BST->DWR] 205, [MYS->AST->BST->DWR] 195.
    ids = {
        ("MYS", "AST"): f"{PART}-{tomorrow_ist()}-MYS-AST-DWR",
        ("MYS", "BST"): f"{PART}-{tomorrow_ist()}-MYS-BST-DWR",
        ("MYS", "AST", "BST"): f"{PART}-{tomorrow_ist()}-MYS-AST-BST-DWR",
    }

    body, _ = uni_run(schedules, PARTITION_AVAIL, engines=["SAME_TRAIN"], ranking_preference="CHEAPEST")
    assert [j["journey_id"] for j in body["results"]] == [
        ids[("MYS", "AST", "BST")], ids[("MYS", "BST")], ids[("MYS", "AST")],
    ]

    # FEWEST_SEAT_CHANGES orders seat changes ascending (1, 1, then 2),
    # which differs from CHEAPEST.
    body, _ = uni_run(
        schedules, PARTITION_AVAIL, engines=["SAME_TRAIN"], ranking_preference="FEWEST_SEAT_CHANGES"
    )
    assert [j["journey_id"] for j in body["results"]] == [
        ids[("MYS", "BST")], ids[("MYS", "AST")], ids[("MYS", "AST", "BST")],
    ]

    # FASTEST: identical durations tie on the secondary fare metric.
    body, _ = uni_run(schedules, PARTITION_AVAIL, engines=["SAME_TRAIN"], ranking_preference="FASTEST")
    assert [j["journey_id"] for j in body["results"]] == [
        ids[("MYS", "AST", "BST")], ids[("MYS", "BST")], ids[("MYS", "AST")],
    ]


def test_ranking_tie_break_is_journey_id():
    # Two trains with identical times and fares: a full metric tie is
    # broken by the single deterministic rule's final key (journey id).
    body, _ = uni_run(tie_schedules(), TIE_AVAIL, engines=["SAME_TRAIN"])
    assert body["total_results"] == 2
    first, second = body["results"]
    assert first["total_fare"] == second["total_fare"] == 220.0
    assert first["total_duration_minutes"] == second["total_duration_minutes"] == 240
    assert first["journey_id"] == f"{TIE1}-{tomorrow_ist()}-MYS-XST-DWR"
    assert second["journey_id"] == f"{TIE2}-{tomorrow_ist()}-MYS-XST-DWR"


def test_missing_fare_ranks_below_known_fares():
    overrides = dict(PARTITION_AVAIL)
    overrides[(PART, "MYS", "AST")] = tc_avail(no_fare=True)
    body, _ = uni_run(
        partition_schedules(), overrides, engines=["SAME_TRAIN"], ranking_preference="CHEAPEST"
    )
    assert body["total_results"] == 3
    first = body["results"][0]
    # The only journey with a fully known fare (205) ranks first.
    assert first["fares_complete"] is True
    assert first["total_fare"] == 205.0
    assert first["journey_id"].endswith("MYS-BST-DWR")
    for journey in body["results"][1:]:
        assert journey["fares_complete"] is False
        assert "total_fare" not in journey  # never estimated
        assert journey["segments_missing_fare"] == [1]


# ================================================================= budgets

def test_shared_request_budget():
    # Two requests cover discovery + one schedule: the availability search
    # never starts, later engines are skipped honestly, and the total never
    # exceeds the envelope.
    call_log: list = []
    body, status = uni_run(
        main_schedules(), MAIN_AVAIL, budgets=big_unified_budgets(max_api_requests=2),
        call_log=call_log,
    )
    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert body["success"] is False
    assert body["results"] == []

    assert body["search_completeness"]["budget"]["requests_used"] == 2
    assert body["search_completeness"]["budget"]["requests_max"] == 2
    assert len(call_log) == 2
    assert all(entry["path"] != "/availability/seats/batch" for entry in call_log)

    # The same-train engine stopped inside its own budget logic; the other
    # two engines were skipped because the SHARED envelope was exhausted.
    assert body["per_engine"][0]["status"] == "PARTIAL_BUDGET_EXHAUSTED"
    assert body["per_engine"][1]["status"] == "SKIPPED_SHARED_BUDGET_EXHAUSTED"
    assert body["per_engine"][2]["status"] == "SKIPPED_SHARED_BUDGET_EXHAUSTED"
    assert "BUDGET_REQUESTS_EXHAUSTED" in body["search_completeness"]["incomplete_reasons"]
    assert body["search_completeness"]["engines_reporting_incomplete"] == [
        "SAME_TRAIN", "TRAIN_CHANGE", "MIXED",
    ]


def test_shared_credit_budget():
    # Three estimated credits: discovery (1) + schedule (1) + exactly one
    # availability item (1) - the credit budget caps the batch size and
    # stops everything after it.
    call_log: list = []
    body, status = uni_run(
        main_schedules(), MAIN_AVAIL, budgets=big_unified_budgets(max_credits=3),
        call_log=call_log,
    )
    assert status == 200
    budget = body["search_completeness"]["budget"]
    assert budget["credits_used_estimate"] == 3
    assert budget["credits_max"] == 3
    assert body["search_completeness"]["segments_checked"] == 1
    items = batch_items(call_log)
    assert len(items) == 1
    # Discovery + schedule + exactly one single-item availability batch.
    assert len(call_log) == 3


def test_budget_exhaustion_before_availability_requests():
    # A one-request envelope: only candidate discovery runs; NO availability
    # item is ever sent and the response says so honestly.
    call_log: list = []
    body, status = uni_run(
        main_schedules(), MAIN_AVAIL, budgets=big_unified_budgets(max_api_requests=1),
        call_log=call_log,
    )
    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert len(call_log) == 1
    assert call_log[0]["path"] == "/routes/trains"
    assert batch_items(call_log) == []
    assert body["search_completeness"]["segments_checked"] == 0
    assert body["search_completeness"]["budget"]["requests_used"] == 1
    for report in body["per_engine"]:
        assert report["total_results"] == 0


def test_shared_wall_clock_deadline():
    # One wall-clock deadline for the WHOLE search: with a slow provider
    # the first engine stops at the deadline and the later engines are
    # skipped - the unified search never runs past its time budget.
    schedules = main_schedules()
    base_handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MAIN_AVAIL),
    )

    def slow_handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.35)
        return base_handler(request)

    client = make_client(slow_handler)
    service = UnifiedJourneyOrchestrationService(
        client, big_unified_budgets(time_budget_seconds=1.0)
    )
    body, status = service.run(UnifiedSearchRequest(**uni_body()))
    client.close()

    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert body["per_engine"][0]["status"] == "PARTIAL_TIME_LIMIT"
    assert body["per_engine"][1]["status"] == "SKIPPED_TIME_BUDGET_EXHAUSTED"
    assert body["per_engine"][2]["status"] == "SKIPPED_TIME_BUDGET_EXHAUSTED"
    assert "TIME_LIMIT_REACHED" in body["search_completeness"]["incomplete_reasons"]
    assert body["search_completeness"]["budget"]["duration_ms"] < 5000
    assert body["search_completeness"]["budget"]["time_budget_ms"] == 1000


def test_shared_budget_counters_never_reset_between_engines():
    # The same-train engine consumes 4 requests; the train-change engine
    # continues counting from there and exhausts the shared envelope; the
    # mixed engine is skipped. Total requests == the envelope exactly, and
    # the confirmed results from the completed engine are preserved.
    call_log: list = []
    body, status = uni_run(
        main_schedules(), MAIN_AVAIL, budgets=big_unified_budgets(max_api_requests=8),
        call_log=call_log,
    )
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["total_results"] == 1  # the same-train engine's confirmed journey
    assert body["results"][0]["source_engines"] == ["SAME_TRAIN"]

    assert body["search_completeness"]["budget"]["requests_used"] == 8
    assert len(call_log) == 8  # the client-side counter agrees: never exceeded
    assert body["per_engine"][0]["status"] == "COMPLETED"
    assert body["per_engine"][1]["status"] == "SEARCH_LIMIT_REACHED"
    assert body["per_engine"][2]["status"] == "SKIPPED_SHARED_BUDGET_EXHAUSTED"
    assert body["search_completeness"]["engines_reporting_incomplete"] == [
        "TRAIN_CHANGE", "MIXED",
    ]
    assert any("more journeys may exist" in body["message"] for _ in [0])


def test_request_budget_overrides_applied():
    # The optional per-request shared budget replaces the configured
    # envelope and is echoed back.
    body, status = uni_run(
        main_schedules(), MAIN_AVAIL,
        budgets=big_unified_budgets(),
        budget={"max_api_requests": 2, "time_budget_seconds": 20},
    )
    assert status == 200
    assert body["search_completeness"]["budget"]["requests_max"] == 2
    assert body["search_completeness"]["budget"]["time_budget_ms"] == 20000
    assert body["search"]["budget"] == {"max_api_requests": 2, "time_budget_seconds": 20}


# ======================================================= failure isolation

def test_one_engine_provider_error_others_succeed():
    # The FIRST discovery call fails (the same-train engine's turn); every
    # later call succeeds, so the train-change and mixed engines run and
    # their confirmed results are preserved with the failure recorded.
    schedules = main_schedules()
    auto = routes_auto(schedules)
    calls = {"count": 0}

    def flaky_routes(query: dict) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                503,
                json=error_envelope("DISCOVERY_OUTAGE", "routes backend briefly down", "SERVER", True),
            )
        return auto(query)

    body, status = uni_run(main_schedules(), MAIN_AVAIL, routes_responder=flaky_routes)
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["success"] is True
    # The failed engine's journeys are absent (it made no claims), but the
    # other engines' confirmed results are preserved: the train-change
    # journey from both surviving engines AND the same-train journey found
    # by the mixed engine alone.
    assert body["total_results"] == 2
    by_first_station = {j["segments"][0]["to_station"]["code"]: j for j in body["results"]}
    assert by_first_station["ASK"]["source_engines"] == ["TRAIN_CHANGE", "MIXED"]
    assert by_first_station["KRNR"]["source_engines"] == ["MIXED"]

    # The failure is recorded per engine, never turned into unavailability.
    assert body["per_engine"][0]["status"] == "PROVIDER_ERROR"
    assert body["per_engine"][0]["http_status"] == 502
    assert body["per_engine"][1]["status"] == "AVAILABLE_COMBINATION"
    assert any(error.get("code") == "DISCOVERY_OUTAGE" for error in body["errors"])
    assert "PROVIDER_ERROR" in body["search_completeness"]["incomplete_reasons"]
    assert body["search_completeness"]["engines_reporting_incomplete"] == ["SAME_TRAIN"]
    assert any("partially incomplete" in body["message"] for _ in [0])


def test_engine_exception_isolated(monkeypatch):
    # An unexpected engine crash is recorded and the other engines'
    # confirmed results are preserved.
    def boom(self, request):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(SameTrainCombinationService, "run", boom)
    body, status = uni_run(main_schedules(), MAIN_AVAIL)
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["total_results"] == 2  # both surviving engines' journeys
    by_first_station = {j["segments"][0]["to_station"]["code"]: j for j in body["results"]}
    assert by_first_station["ASK"]["source_engines"] == ["TRAIN_CHANGE", "MIXED"]
    assert by_first_station["KRNR"]["source_engines"] == ["MIXED"]
    assert body["per_engine"][0]["status"] == "ENGINE_ERROR"
    assert any(error.get("code") == "ENGINE_FAILURE" for error in body["errors"])


# ===================================================================== API

def test_api_happy_path_and_response_shape(make_test_client):
    schedules = main_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MAIN_AVAIL),
    )
    test_client = make_test_client(handler)
    response = test_client.post(SEARCH_URL, json=uni_body())
    assert response.status_code == 200
    body = response.json()

    # Every documented response field is present.
    for field in (
        "success", "status", "message", "search", "ranked_by", "results",
        "total_results", "results_truncated", "unverified_candidates",
        "total_unverified_candidates", "deduplication", "per_engine",
        "direct_trains", "search_completeness", "api_usage",
        "availability_checked_at", "errors", "notes",
    ):
        assert field in body, f"response field {field} missing"

    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["ranked_by"] == "CHEAPEST"
    assert body["total_results"] == 2
    assert body["search"]["engines"] == ["SAME_TRAIN", "TRAIN_CHANGE", "MIXED"]
    assert body["search"]["from_station"] == "MYS"
    assert body["search"]["max_results"] == 30
    assert len(body["per_engine"]) == 3
    assert body["deduplication"]["duplicates_removed"] == 2
    assert body["api_usage"]["provider"] == "railcore"
    assert body["api_usage"]["requests_sent"] > 0

    # Normalized journeys in the unified shape.
    journey = body["results"][0]
    for field in (
        "journey_id", "journey_type", "journey_status", "from_station", "to_station",
        "journey_date", "departure", "arrival", "departure_datetime",
        "arrival_datetime", "overnight", "total_duration_minutes", "total_fare",
        "fares_complete", "segment_count", "train_changes", "seat_changes",
        "distinct_train_count", "distinct_trains", "transitions", "segments",
        "all_segments_confirmed_available", "source_engines", "engine_journey_ids",
    ):
        assert field in journey, f"journey field {field} missing"
    for segment in journey["segments"]:
        for field in (
            "segment_number", "segment_type", "train_number", "from_station",
            "to_station", "boarding_query_date", "travel_class", "quota", "status",
        ):
            assert field in segment, f"segment field {field} missing"


def test_openapi_documents_unified_endpoint(make_test_client):
    schedules = main_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MAIN_AVAIL),
    )
    test_client = make_test_client(handler)
    schema = test_client.get("/openapi.json").json()

    assert "/api/v1/journeys/search" in schema["paths"]
    post = schema["paths"]["/api/v1/journeys/search"]["post"]
    assert "unified" in post["summary"].lower()
    request_ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert request_ref.endswith("UnifiedSearchRequest")
    response_ref = post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert response_ref.endswith("UnifiedSearchResponse")

    components = schema["components"]["schemas"]
    for component in (
        "UnifiedSearchRequest",
        "UnifiedSearchResponse",
        "UnifiedJourneyOut",
        "UnifiedSegmentOut",
        "UnifiedTransitionOut",
        "UnifiedEngineReportOut",
        "UnifiedDeduplicationOut",
        "UnifiedCompletenessOut",
        "UnifiedBudgetOverrides",
    ):
        assert component in components, f"schema component {component} missing"

    # The four ranking preferences and the engine selection are documented.
    request_schema = components["UnifiedSearchRequest"]
    preference = request_schema["properties"]["ranking_preference"]
    values = set(preference.get("enum") or [])
    if not values and "$ref" in preference:
        values = set(components[preference["$ref"].split("/")[-1]]["enum"])
    assert values == {"CHEAPEST", "FASTEST", "FEWEST_TRAIN_CHANGES", "FEWEST_SEAT_CHANGES"}
    engines = request_schema["properties"]["engines"]
    engine_values = set(engines.get("enum") or []) or set(
        (engines.get("items") or {}).get("enum") or []
    )
    if not engine_values and "$ref" in (engines.get("items") or {}):
        engine_values = set(
            components[engines["items"]["$ref"].split("/")[-1]]["enum"]
        )
    assert engine_values == {"SAME_TRAIN", "TRAIN_CHANGE", "MIXED"}

    # Segment typing is documented on the unified segment.
    segment_schema = components["UnifiedSegmentOut"]["properties"]["segment_type"]
    assert "SAME_TRAIN_SEGMENT" in segment_schema["description"]
    assert "TRAIN_CHANGE" in segment_schema["description"]


def test_request_validation_errors(make_test_client):
    test_client = make_test_client(make_handler())
    invalid_payloads = [
        uni_body(from_station="DWR"),  # source equals destination
        uni_body(engines=[]),  # at least one engine must be selected
        uni_body(engines=["SAME_TRAIN", "SAME_TRAIN"]),  # no duplicates
        uni_body(engines=["BOGUS_ENGINE"]),  # unknown engine
        uni_body(ranking_preference="FEWEST_CHANGES"),  # engine enum, not unified
        uni_body(budget={"max_api_requests": 0}),  # below the minimum
        uni_body(budget={"time_budget_seconds": 0.1}),  # below the minimum
        uni_body(max_results=0),  # below the minimum
        uni_body(travel_class="XX"),  # class not offered by any railway
        uni_body(journey_date="2020-01-01"),  # past date
    ]
    for payload in invalid_payloads:
        response = test_client.post(SEARCH_URL, json=payload)
        assert response.status_code == 422, f"expected 422 for {payload}"
    # The unified four preferences are all accepted.
    for preference in ("CHEAPEST", "FASTEST", "FEWEST_TRAIN_CHANGES", "FEWEST_SEAT_CHANGES"):
        response = test_client.post(SEARCH_URL, json=uni_body(ranking_preference=preference))
        assert response.status_code == 200


def test_missing_api_key_is_config_error_503(make_test_client):
    schedules = main_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MAIN_AVAIL),
    )
    # An unconfigured client: every engine reports CONFIG_ERROR and the
    # unified search surfaces it (no availability claims made).
    client = make_client(handler, api_key="")
    from app.api.deps import get_railcore_client  # local import: override for this test

    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides[get_railcore_client] = lambda: client
    try:
        service = UnifiedJourneyOrchestrationService(client, big_unified_budgets())
        body, status = service.run(UnifiedSearchRequest(**uni_body()))
    finally:
        fastapi_app.dependency_overrides.pop(get_railcore_client, None)
        client.close()
    assert status == 503
    assert body["status"] == "CONFIG_ERROR"
    assert body["total_results"] == 0
    assert {report["status"] for report in body["per_engine"]} == {"CONFIG_ERROR"}
    assert "RAILCORE_API_KEY" in body["per_engine"][0]["message"]


def test_seed_provider_failure_is_502(make_test_client):
    # Every discovery call fails and no engine produces results: the
    # unified response is an honest PROVIDER_ERROR, never NOT_AVAILABLE.
    def failing_routes(_query):
        return httpx.Response(
            503,
            json=error_envelope("DISCOVERY_DOWN", "routes backend down", "SERVER", True),
        )

    schedules = main_schedules()
    handler = make_handler(
        route_trains_responder=failing_routes,
        schedules=schedules,
        availability_by_train=full_map(schedules, MAIN_AVAIL),
    )
    test_client = make_test_client(handler)
    response = test_client.post(SEARCH_URL, json=uni_body())
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "PROVIDER_ERROR"
    assert body["success"] is False
    assert body["results"] == []
    assert {report["status"] for report in body["per_engine"]} == {"PROVIDER_ERROR"}
    assert any(error.get("code") == "DISCOVERY_DOWN" for error in body["errors"])


# ================================================== regression & wiring

def test_regression_three_existing_endpoints(make_test_client):
    # The three engine endpoints behave exactly as before the unified
    # layer was added (their engines were not modified).
    schedules = main_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MAIN_AVAIL),
    )
    test_client = make_test_client(handler)

    response = test_client.post("/api/v1/journeys/same-train/search", json={
        "from_station": "MYS", "to_station": "DWR",
        "journey_date": tomorrow_ist(), "travel_class": "SL", "quota": "GN",
        "ranking_preference": "CHEAPEST",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["total_results"] == 1

    response = test_client.post("/api/v1/journeys/train-change/search", json={
        "from_station": "MYS", "to_station": "DWR",
        "journey_date": tomorrow_ist(), "travel_class": "SL", "quota": "GN",
        "ranking_preference": "CHEAPEST",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "AVAILABLE_COMBINATION"
    assert response.json()["total_results"] == 1

    response = test_client.post("/api/v1/journeys/mixed/search", json={
        "from_station": "MYS", "to_station": "DWR",
        "journey_date": tomorrow_ist(), "travel_class": "SL", "quota": "GN",
        "ranking_preference": "CHEAPEST",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "AVAILABLE_COMBINATION"
    assert response.json()["total_results"] == 2


def test_unified_budgets_from_settings(monkeypatch):
    monkeypatch.setenv("UNIFIED_MAX_API_REQUESTS", "77")
    monkeypatch.setenv("UNIFIED_MAX_CREDITS", "160")
    monkeypatch.setenv("UNIFIED_TIME_BUDGET_SECONDS", "12.5")
    monkeypatch.setenv("UNIFIED_MAX_RESULTS", "9")
    budgets = UnifiedJourneyBudgets.from_settings(Settings())
    assert budgets.max_api_requests == 77
    assert budgets.max_credits == 160
    assert budgets.time_budget_seconds == 12.5
    assert budgets.max_results == 9


def test_unified_budget_clamps():
    budgets = UnifiedJourneyBudgets(
        max_api_requests=0,
        max_credits=0,
        time_budget_seconds=0.0,
        max_results=0,
        batch_size=99,
    )
    assert budgets.max_api_requests == 1
    assert budgets.max_credits == 1
    assert budgets.time_budget_seconds == 0.5
    assert budgets.max_results == 1
    assert budgets.batch_size == 25


def test_segment_distance_km_uses_delta_not_cumulative_origin_distance():
    """A train starting 1800km earlier must report segment delta (192km), not 1992km."""
    from app.services.journey_normalizer import _p2_journey

    p2_combo = {
        "combination_id": "11020-2026-11-26-PUNE-KYN-CSMT",
        "train_number": "11020",
        "train_name": "KONARK EXPRESS",
        "journey_date": "2026-11-26",
        "departure": {"station": {"code": "PUNE", "name": "Pune Jn", "sequence": 25}, "time": "01:25", "day": 2},
        "arrival": {"station": {"code": "CSMT", "name": "Mumbai CSMT", "sequence": 28}, "time": "05:50", "day": 2},
        "total_duration_minutes": 265,
        "total_fare": 520.0,
        "fares_complete": True,
        "segments_missing_fare": [],
        "segment_count": 2,
        "seat_changes": 1,
        "overnight": False,
        "all_segments_confirmed_available": True,
        "segments": [
            {
                "segment_number": 1,
                "train_number": "11020",
                "from_station": {"code": "PUNE", "name": "Pune Jn", "sequence": 25},
                "to_station": {"code": "KYN", "name": "Kalyan Jn", "sequence": 27},
                "departure_time": "01:25",
                "departure_day": 2,
                "arrival_time": "03:49",
                "arrival_day": 2,
                "boarding_query_date": "2026-11-26",
                "distance_km_at_to_station": 1938.0,
                "segment_distance_km": 138.0,
                "travel_class": "3A",
                "quota": "GN",
                "status": "CONFIRMED_AVAILABLE",
                "fare": 310.0,
            },
            {
                "segment_number": 2,
                "train_number": "11020",
                "from_station": {"code": "KYN", "name": "Kalyan Jn", "sequence": 27},
                "to_station": {"code": "CSMT", "name": "Mumbai CSMT", "sequence": 28},
                "departure_time": "04:00",
                "departure_day": 2,
                "arrival_time": "05:50",
                "arrival_day": 2,
                "boarding_query_date": "2026-11-26",
                "distance_km_at_to_station": 1992.0,
                "segment_distance_km": 54.0,
                "travel_class": "3A",
                "quota": "GN",
                "status": "CONFIRMED_AVAILABLE",
                "fare": 210.0,
            },
        ],
    }

    unified = _p2_journey(p2_combo)
    assert [s.segment_distance_km for s in unified.segments] == [138.0, 54.0]
    assert unified.total_distance_km == 192.0

