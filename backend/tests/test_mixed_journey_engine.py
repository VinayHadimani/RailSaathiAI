"""Mixed journey combination engine tests (Phase 4).

Covers POST /api/v1/journeys/mixed/search and the engine service directly,
against a fully synthetic rail network (trains 997xx, mirroring the
MYS -> DWR product scenario but with fictional schedules and configurable
availability). Every provider response is mocked; no test ever contacts the
real RailCore API and no credits are consumed.

Required scenarios (numbered per the phase brief):
 1 same-train segments + one train change     16 one UNKNOWN segment
 2 train change + same-train segments         17 provider error on one segment
 3 two same-train groups via one change        18 duplicate candidate journeys
 4 multiple train changes + seat changes      19 cycle prevention
 5 journey with only same-train segments       20 more than two train changes
 6 journey with only train changes            21 more than two seat changes
 7 invalid station sequence                    22 missing fare data
 8 invalid interchange station                23 cheapest ranking
 9 insufficient transfer buffer               24 fastest ranking
10 excessive transfer wait                     25 fewest train changes ranking
11 overnight same-train segments              26 fewest seat changes ranking
12 overnight train change                     27 search budget exhaustion
13 confirmed availability everywhere          28 search horizon exhaustion
14 one WAITLIST segment                        29 query deduplication
15 one RAC segment                            30 unverified never fully available

plus: API response shape, OpenAPI documentation, settings wiring, budget
clamps, validation, direct trains never availability-checked, seat changes
never labelled transfers, and cross-engine consistency with the phase 2 and
phase 3 engines on the same network.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import Settings
from app.schemas.journey_search import SameTrainSearchRequest
from app.schemas.mixed_search import MixedSearchRequest
from app.schemas.train_change_search import TrainChangeSearchRequest
from app.services.same_train_combination_service import SameTrainCombinationService
from app.services.search_budgets import (
    MixedJourneyBudgets,
    SearchBudgets,
    TrainChangeBudgets,
)
from app.services.segment_query_planner import SegmentQueryPlanner
from app.services.train_change_connection_service import TrainChangeConnectionService
from app.services.mixed_journey_service import MixedJourneyCombinationService

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
    usage_headers,
)

SEARCH_URL = "/api/v1/journeys/mixed/search"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
CONFIRMED = "CONFIRMED_AVAILABLE"

# Synthetic train numbers (997xx do not exist; never queried live).
TA = "99701"  # MYS 08:00 -> KRNR 09:10 -> ASK 10:30 -> SBC 12:30 -> UBL 15:00 -> DWR 17:30
TB = "99702"  # ASK 11:35 -> SBC 13:30 -> UBL 16:00 -> DWR 18:30
TC = "99703"  # SBC 14:05 -> UBL 16:30 -> DWR 19:00
TO = "99704"  # MYS 22:45 d1 -> KRNR 23:15 d1 -> ASK 01:25 d2 -> SBC 03:30 d2 -> UBL 06:10 d2 -> DWR 06:43 d2 (overnight)
TX = "99705"  # KRNR 05:00 d2 -> DWR 08:00 d2 (overnight connection)
TN = "99706"  # UBL 19:00 -> DWR 19:30 -> BGM 21:00 -> UBL 23:00 -> MYS 02:00 d2 (circular)
TREV = "99707"  # DWR 08:00 -> MYS 17:00 (reverse direction: invalid order)
TPASS = "99708"  # SBC 14:00 (pass-through) -> UBL 16:00 -> DWR 18:00
TBUF = "99709"  # ASK 10:55 -> DWR 13:00 (25-minute transfer: below default buffer)
TWAIT = "99710"  # ASK 22:35 -> DWR 23:59 (725-minute transfer: above default max wait)
R1 = "99721"  # MYS 08:00 -> A 09:00 -> DWR 13:00
R2 = "99722"  # A 10:05 -> B 11:00 -> DWR 12:00
R3 = "99723"  # B 12:05 -> DWR 12:35
C1 = "99731"  # MYS 08:00 -> X1 09:00 -> X2 10:00
C2 = "99732"  # X1 10:35 -> X2 11:30 -> X3 12:30
C3 = "99733"  # X2 13:05 -> X3 14:00 -> X4 15:00
C4 = "99734"  # X3 16:05 -> X4 17:00 -> DWR 18:00


@pytest.fixture(autouse=True)
def _clear_planner_caches():
    """Isolate process-wide schedule/discovery caches between tests."""
    SegmentQueryPlanner.clear_shared_caches()
    yield
    SegmentQueryPlanner.clear_shared_caches()


# ================================================================ fixtures


def _d(offset_days: int = 0) -> str:
    return (datetime.now(tz=IST).date() + timedelta(days=1 + offset_days)).isoformat()


def master_schedules() -> dict[str, dict]:
    return {
        TA: tc_schedule(TA, [
            tc_stop(1, "MYS", "Mysuru Junction", None, "08:00", dist=0),
            tc_stop(2, "KRNR", "Krishnarajanagar", "09:10", "09:15", dist=35),
            tc_stop(3, "ASK", "Arsikere Junction", "10:30", "10:35", dist=160),
            tc_stop(4, "SBC", "Bengaluru", "12:30", "12:35", dist=320),
            tc_stop(5, "UBL", "Hubballi Junction", "15:00", "15:05", dist=410),
            tc_stop(6, "DWR", "Dharwad", "17:30", None, dist=440),
        ]),
        TB: tc_schedule(TB, [
            tc_stop(1, "ASK", "Arsikere Junction", None, "11:35"),
            tc_stop(2, "SBC", "Bengaluru", "13:30", "13:35", dist=160),
            tc_stop(3, "UBL", "Hubballi Junction", "16:00", "16:05", dist=250),
            tc_stop(4, "DWR", "Dharwad", "18:30", None, dist=280),
        ]),
        TC: tc_schedule(TC, [
            tc_stop(1, "SBC", "Bengaluru", None, "14:05"),
            tc_stop(2, "UBL", "Hubballi Junction", "16:30", "16:35", dist=90),
            tc_stop(3, "DWR", "Dharwad", "19:00", None, dist=120),
        ]),
    }


def routes_auto(
    schedules: dict[str, dict],
    duplicates: dict[tuple[str, str], int] | None = None,
    forced: dict[tuple[str, str], list[tuple[str, str, str]]] | None = None,
):
    """Discovery responder: every (from, to) pair is answered with the
    schedule-trains that serve it forward - computed from the schedules,
    never a hardcoded list. Unknown pairs answer 404 NO_TRAINS_FOUND
    exactly like the real provider.

    - duplicates: extra copies of the first entry for a pair (provider
      returning the same train twice).
    - forced: extra (train, dep, arr) route entries appended to a pair's
      answer - for scenarios where the provider's discovery answer claims
      a train the schedule contradicts (e.g. reverse-direction trains).
    """

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
        extra = (duplicates or {}).get((frm, to), 0)
        if entries and extra:
            entries = entries + [entries[0]] * extra
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


MASTER_AVAIL: dict[tuple[str, str, str], dict] = {
    (TA, "MYS", "KRNR"): tc_avail(fare=60),
    (TA, "KRNR", "ASK"): tc_avail(fare=55),
    (TA, "MYS", "ASK"): tc_avail(fare=165),
    (TA, "ASK", "SBC"): tc_avail(fare=45),
    (TA, "SBC", "UBL"): tc_avail(fare=180),
    (TA, "UBL", "DWR"): tc_avail(fare=75),
    (TB, "ASK", "DWR"): tc_avail(fare=210),
    (TB, "SBC", "DWR"): tc_avail(fare=120),
    (TC, "UBL", "DWR"): tc_avail(fare=90),
}


def big_budgets(**overrides) -> MixedJourneyBudgets:
    params = dict(
        min_transfer_buffer_minutes=30,
        max_transfer_wait_minutes=720,
        search_horizon_days=2,
        max_discovery_pairs=60,
        max_trains=20,
        max_api_requests=200,
        max_credits=400,
        time_budget_seconds=30.0,
        max_expanded_states=500,
        max_journeys=100,
        max_results=100,
    )
    params.update(overrides)
    return MixedJourneyBudgets(**params)


def mx_body(**overrides) -> dict:
    base = {
        "from_station": "MYS",
        "to_station": "DWR",
        "journey_date": tomorrow_ist(),
        "travel_class": "SL",
        "quota": "GN",
        "ranking_preference": "FASTEST",
    }
    base.update(overrides)
    return base


def mx_run(
    schedules: dict[str, dict],
    availability: dict | None = None,
    *,
    routes_duplicates: dict[tuple[str, str], int] | None = None,
    routes_forced: dict[tuple[str, str], list[tuple[str, str, str]]] | None = None,
    budgets: MixedJourneyBudgets | None = None,
    batch_item_failures=None,
    call_log: list | None = None,
    fresh_caches: bool = True,
    **body_overrides,
):
    """Run the mixed engine service directly against a mocked provider.

    fresh_caches clears the process-wide schedule/discovery caches first:
    two synthetic networks answering the same (from, to, date) pair
    differently would otherwise leak into each other (a real provider is
    consistent per pair, so the caches are correct in production)."""
    if fresh_caches:
        SegmentQueryPlanner.clear_shared_caches()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules, routes_duplicates, routes_forced),
        schedules=schedules,
        availability_by_train=availability if availability is not None else full_map(schedules, MASTER_AVAIL),
        batch_item_failures=batch_item_failures,
        call_log=call_log,
    )
    client = make_client(handler)
    service = MixedJourneyCombinationService(client, budgets or big_budgets())
    request = MixedSearchRequest(**mx_body(**body_overrides))
    return service.run(request)


# ---------------------------------------------------------------- helpers


def seg_sig(segment: dict) -> tuple[str, str, str]:
    return (segment["train_number"], segment["from_station"]["code"], segment["to_station"]["code"])


def journey_sig(journey: dict) -> tuple:
    return tuple(seg_sig(s) for s in journey["segments"])


def find_by_sig(body: dict, signature: tuple, section: str = "results"):
    for journey in body[section]:
        if journey_sig(journey) == signature:
            return journey
    return None


def trains_of(journey: dict) -> tuple:
    return tuple(segment["train_number"] for segment in journey["segments"])


def station_path(journey: dict) -> list[str]:
    codes = [journey["segments"][0]["from_station"]["code"]]
    codes.extend(segment["to_station"]["code"] for segment in journey["segments"])
    return codes


def batch_items(call_log: list) -> list[dict]:
    return [
        item
        for call in call_log
        if call["path"] == "/availability/seats/batch"
        for item in call["items"]
    ]


def schedule_calls(call_log: list) -> list[dict]:
    return [call for call in call_log if call["path"] == "/trains/{n}/schedule"]


def assert_every_segment_confirmed(body: dict, section: str = "results") -> None:
    for journey in body[section]:
        assert journey["all_segments_confirmed_available"] is True
        assert journey["journey_status"] == "AVAILABLE_COMBINATION"
        for segment in journey["segments"]:
            assert segment["status"] == CONFIRMED


# Expected master journeys as segment signatures ((train, from, to), ...).
J_A = ((TA, "MYS", "ASK"), (TB, "ASK", "DWR"))                                     # 1 change, 0 seats
J_B = ((TA, "MYS", "KRNR"), (TA, "KRNR", "ASK"), (TB, "ASK", "DWR"))                # 1 seat + 1 change
J_C = ((TA, "MYS", "ASK"), (TA, "ASK", "SBC"), (TB, "SBC", "DWR"))                 # 1 seat + 1 change
J_D = ((TA, "MYS", "KRNR"), (TA, "KRNR", "ASK"), (TA, "ASK", "SBC"), (TB, "SBC", "DWR"))
J_E = ((TA, "MYS", "ASK"), (TA, "ASK", "SBC"), (TA, "SBC", "UBL"), (TC, "UBL", "DWR"))
J_F = ((TA, "MYS", "ASK"), (TA, "ASK", "SBC"), (TA, "SBC", "UBL"), (TA, "UBL", "DWR"))
J_G = ((TA, "MYS", "KRNR"), (TA, "KRNR", "ASK"), (TA, "ASK", "SBC"), (TA, "SBC", "UBL"), (TC, "UBL", "DWR"))
J_H = ((TA, "MYS", "KRNR"), (TA, "KRNR", "ASK"), (TA, "ASK", "SBC"), (TA, "SBC", "UBL"), (TA, "UBL", "DWR"))
ALL_MASTER = {J_A, J_B, J_C, J_D, J_E, J_F, J_G, J_H}


# =================================================================
# Scenarios 1-4: the four mixed shapes
# =================================================================


def test_scenario_1_same_train_segments_then_one_train_change():
    availability = full_map(master_schedules(), {
        (TA, "MYS", "KRNR"): tc_avail(fare=60),
        (TA, "KRNR", "ASK"): tc_avail(fare=55),
        (TB, "ASK", "DWR"): tc_avail(fare=210),
    })
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == J_B
    assert journey["journey_type"] == "MIXED"
    assert journey["segment_count"] == 3
    assert journey["seat_changes"] == 1
    assert journey["train_changes"] == 1
    assert journey["total_fare"] == 325.0
    assert journey["total_duration_minutes"] == 630  # 08:00 -> 18:30
    assert journey["total_transfer_wait_minutes"] == 65  # ASK 10:30 -> 11:35
    assert journey["distinct_trains"] == [TA, TB]
    assert [s["segment_type"] for s in journey["segments"]] == [
        "FIRST_SEGMENT", "SAME_TRAIN_SEGMENT", "TRAIN_CHANGE",
    ]
    types = [t["transition_type"] for t in journey["transitions"]]
    assert types == ["SAME_TRAIN_SEAT_CHANGE", "TRAIN_CHANGE"]
    assert journey["transitions"][0]["is_train_change"] is False
    assert journey["transitions"][1]["is_train_change"] is True


def test_scenario_2_train_change_then_same_train_segments():
    availability = full_map(master_schedules(), {
        (TA, "MYS", "ASK"): tc_avail(fare=165),
        (TB, "ASK", "SBC"): tc_avail(fare=60),
        (TB, "SBC", "DWR"): tc_avail(fare=120),
    })
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == ((TA, "MYS", "ASK"), (TB, "ASK", "SBC"), (TB, "SBC", "DWR"))
    assert journey["journey_type"] == "MIXED"
    assert journey["segment_count"] == 3
    assert journey["train_changes"] == 1
    assert journey["seat_changes"] == 1
    assert journey["total_fare"] == 345.0
    assert journey["total_duration_minutes"] == 630
    assert journey["total_transfer_wait_minutes"] == 65  # only the ASK change
    assert [s["segment_type"] for s in journey["segments"]] == [
        "FIRST_SEGMENT", "TRAIN_CHANGE", "SAME_TRAIN_SEGMENT",
    ]
    # The seat change at SBC is NOT a transfer: no wait/buffer claims
    # (null fields are excluded from the JSON body).
    seat = journey["transitions"][1]
    assert seat["transition_type"] == "SAME_TRAIN_SEAT_CHANGE"
    assert "wait_minutes" not in seat
    assert "within_min_buffer" not in seat
    assert "within_max_wait" not in seat
    assert seat["onboard_halt_minutes"] == 5  # SBC 13:30 -> 13:35


def test_scenario_3_two_same_train_groups_connected_by_a_train_change():
    availability = full_map(master_schedules(), {
        (TA, "MYS", "KRNR"): tc_avail(fare=60),
        (TA, "KRNR", "ASK"): tc_avail(fare=55),
        (TA, "ASK", "SBC"): tc_avail(fare=45),
        (TB, "SBC", "UBL"): tc_avail(fare=70),
        (TB, "UBL", "DWR"): tc_avail(fare=80),
    })
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    expected = (
        (TA, "MYS", "KRNR"), (TA, "KRNR", "ASK"), (TA, "ASK", "SBC"),
        (TB, "SBC", "UBL"), (TB, "UBL", "DWR"),
    )
    assert journey_sig(journey) == expected
    assert journey["journey_type"] == "MIXED"
    assert journey["segment_count"] == 5
    assert journey["seat_changes"] == 3  # KRNR, ASK (TA group) + UBL (TB group)
    assert journey["train_changes"] == 1  # SBC
    assert journey["distinct_trains"] == [TA, TB]
    assert [t["transition_type"] for t in journey["transitions"]] == [
        "SAME_TRAIN_SEAT_CHANGE", "SAME_TRAIN_SEAT_CHANGE", "TRAIN_CHANGE", "SAME_TRAIN_SEAT_CHANGE",
    ]
    assert journey["total_fare"] == 310.0


def test_scenario_4_multiple_train_changes_and_seat_changes_in_one_journey():
    availability = full_map(master_schedules(), {
        (TA, "MYS", "KRNR"): tc_avail(fare=60),
        (TA, "KRNR", "ASK"): tc_avail(fare=55),
        (TB, "ASK", "SBC"): tc_avail(fare=60),
        (TB, "SBC", "UBL"): tc_avail(fare=180),
        (TC, "UBL", "DWR"): tc_avail(fare=90),
    })
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    expected = (
        (TA, "MYS", "KRNR"), (TA, "KRNR", "ASK"), (TB, "ASK", "SBC"),
        (TB, "SBC", "UBL"), (TC, "UBL", "DWR"),
    )
    assert journey_sig(journey) == expected
    assert journey["journey_type"] == "MIXED"
    assert journey["segment_count"] == 5
    assert journey["train_changes"] == 2  # ASK and UBL
    assert journey["seat_changes"] == 2  # KRNR (TA) and SBC (TB)
    assert journey["total_fare"] == 445.0
    assert journey["total_duration_minutes"] == 660  # 08:00 -> 19:00
    assert journey["total_transfer_wait_minutes"] == 65 + 35  # ASK 10:30->11:35, UBL 16:00->16:35
    assert [t["transition_type"] for t in journey["transitions"]] == [
        "SAME_TRAIN_SEAT_CHANGE", "TRAIN_CHANGE", "SAME_TRAIN_SEAT_CHANGE", "TRAIN_CHANGE",
    ]


# =================================================================
# Scenarios 5-6: pure journeys of one kind
# =================================================================


def test_scenario_5_journey_containing_only_same_train_segments():
    schedules = master_schedules()
    availability = full_map(schedules, {
        (TA, "MYS", "KRNR"): tc_avail(fare=60),
        (TA, "KRNR", "ASK"): tc_avail(fare=55),
        (TA, "ASK", "DWR"): tc_avail(fare=250),
    })
    body, status = mx_run(schedules, availability)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == ((TA, "MYS", "KRNR"), (TA, "KRNR", "ASK"), (TA, "ASK", "DWR"))
    assert journey["journey_type"] == "SAME_TRAIN_SEGMENTS"
    assert journey["train_changes"] == 0
    assert journey["seat_changes"] == 2
    assert journey["total_transfer_wait_minutes"] == 0
    assert journey["distinct_trains"] == [TA]
    for transition in journey["transitions"]:
        assert transition["transition_type"] == "SAME_TRAIN_SEAT_CHANGE"
        assert transition["is_train_change"] is False


def test_scenario_6_journey_containing_only_train_changes():
    availability = full_map(master_schedules(), {
        (TA, "MYS", "ASK"): tc_avail(fare=165),
        (TB, "ASK", "DWR"): tc_avail(fare=210),
    })
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == J_A
    assert journey["journey_type"] == "TRAIN_CHANGE"
    assert journey["train_changes"] == 1
    assert journey["seat_changes"] == 0
    for transition in journey["transitions"]:
        assert transition["transition_type"] == "TRAIN_CHANGE"
        assert transition["is_train_change"] is True


# =================================================================
# Scenarios 7-8: invalid station sequences and interchanges
# =================================================================


def test_scenario_7_invalid_station_sequence():
    # TREV runs DWR -> MYS (the destination before the source). The provider
    # discovery answer claims it serves MYS -> DWR, but its schedule puts DWR
    # BEFORE MYS: no forward leg from MYS exists, so it can never be part of
    # a journey - and its availability is never queried.
    schedules = master_schedules()
    schedules[TREV] = tc_schedule(TREV, [
        tc_stop(1, "DWR", "Dharwad", None, "08:00"),
        tc_stop(2, "UBL", "Hubballi Junction", "10:00", "10:05"),
        tc_stop(3, "MYS", "Mysuru Junction", "17:00", None),
    ])
    call_log: list = []
    body, status = mx_run(
        schedules,
        routes_forced={("MYS", "DWR"): [(TREV, "08:00", "17:00")]},
        call_log=call_log,
    )
    assert status == 200
    assert {journey_sig(j) for j in body["results"]} == ALL_MASTER
    assert all(TREV not in trains_of(j) for j in body["results"])
    assert not any(item["train_number"] == TREV for item in batch_items(call_log)), (
        "a train whose station order is invalid must never be queried for availability"
    )
    # It is still visible as a reference through train (never availability-checked).
    assert any(d["train_number"] == TREV for d in body["direct_trains"])
    assert all(d["availability_checked"] is False for d in body["direct_trains"])


def test_scenario_8_second_train_not_stopping_at_the_interchange():
    # TPASS passes SBC without stopping: discovery offers it for
    # SBC -> DWR, but no leg can board there, so no journey can use it.
    schedules = master_schedules()
    schedules[TPASS] = tc_schedule(TPASS, [
        tc_stop(1, "SBC", "Bengaluru", "13:30", "14:00", is_stop=False),
        tc_stop(2, "UBL", "Hubballi Junction", "16:00", "16:05"),
        tc_stop(3, "DWR", "Dharwad", "18:00", None),
    ])
    availability = full_map(schedules, {
        (TA, "MYS", "ASK"): tc_avail(fare=165),
        (TA, "ASK", "SBC"): tc_avail(fare=45),
        (TPASS, "UBL", "DWR"): tc_avail(fare=90),
    })
    call_log: list = []
    body, status = mx_run(schedules, availability, call_log=call_log)
    assert status == 200
    assert body["status"] == "NO_COMPLETE_JOURNEY"
    assert body["total_results"] == 0
    assert not any(item["train_number"] == TPASS for item in batch_items(call_log)), (
        "a train that does not stop at the interchange must never be queried from it"
    )
    assert body["search_completeness"]["complete"] is True


# =================================================================
# Scenarios 9-10: transfer window boundaries
# =================================================================


def test_scenario_9_insufficient_transfer_buffer():
    # TBUF departs ASK 10:55, 25 minutes after TA's 10:30 arrival: below
    # the default 30-minute buffer, so the leg is never generated (and
    # never queried). With a 20-minute buffer override the connection is
    # valid.
    schedules = master_schedules()
    schedules[TBUF] = tc_schedule(TBUF, [
        tc_stop(1, "ASK", "Arsikere Junction", None, "10:55"),
        tc_stop(2, "DWR", "Dharwad", "13:00", None, dist=280),
    ])
    availability = full_map(schedules, {
        (TA, "MYS", "ASK"): tc_avail(fare=165),
        (TBUF, "ASK", "DWR"): tc_avail(fare=200),
    })

    call_log: list = []
    body, status = mx_run(schedules, availability, call_log=call_log)
    assert status == 200
    assert body["status"] == "NO_COMPLETE_JOURNEY"
    assert not any(item["train_number"] == TBUF for item in batch_items(call_log)), (
        "a below-buffer connection must be rejected before any availability query"
    )

    body, status = mx_run(schedules, availability, min_transfer_buffer_minutes=20)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == ((TA, "MYS", "ASK"), (TBUF, "ASK", "DWR"))
    transfer = journey["transitions"][0]
    assert transfer["wait_minutes"] == 25
    assert transfer["within_min_buffer"] is True
    assert transfer["within_max_wait"] is True


def test_scenario_10_excessive_transfer_wait():
    # TWAIT departs ASK 22:35, 725 minutes after TA's 10:30 arrival:
    # above the default 720-minute maximum wait, so the leg is never
    # generated. With an 800-minute wait override the connection is valid.
    schedules = master_schedules()
    schedules[TWAIT] = tc_schedule(TWAIT, [
        tc_stop(1, "ASK", "Arsikere Junction", None, "22:35"),
        tc_stop(2, "DWR", "Dharwad", "23:59", None, dist=280),
    ])
    availability = full_map(schedules, {
        (TA, "MYS", "ASK"): tc_avail(fare=165),
        (TWAIT, "ASK", "DWR"): tc_avail(fare=200),
    })

    call_log: list = []
    body, status = mx_run(schedules, availability, call_log=call_log)
    assert status == 200
    assert body["status"] == "NO_COMPLETE_JOURNEY"
    assert not any(item["train_number"] == TWAIT for item in batch_items(call_log)), (
        "an above-max-wait connection must be rejected before any availability query"
    )

    body, status = mx_run(schedules, availability, max_transfer_wait_minutes=800)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == ((TA, "MYS", "ASK"), (TWAIT, "ASK", "DWR"))
    transfer = journey["transfers"][0] if "transfers" in journey else journey["transitions"][0]
    assert transfer["wait_minutes"] == 725
    assert transfer["within_min_buffer"] is True
    assert transfer["within_max_wait"] is True


# =================================================================
# Scenarios 11-12: overnight journeys
# =================================================================


def overnight_schedules() -> dict[str, dict]:
    schedules = master_schedules()
    schedules[TO] = tc_schedule(TO, [
        tc_stop(1, "MYS", "Mysuru Junction", None, "22:45", day=1, dist=0),
        tc_stop(2, "KRNR", "Krishnarajanagar", "23:15", "23:20", day=1, dist=35),
        tc_stop(3, "ASK", "Arsikere Junction", "01:25", "01:30", day=2, dist=160),
        tc_stop(4, "SBC", "Bengaluru", "03:30", "03:35", day=2, dist=320),
        tc_stop(5, "UBL", "Hubballi Junction", "06:10", "06:20", day=2, dist=410),
        tc_stop(6, "DWR", "Dharwad", "06:43", None, day=2, dist=440),
    ])
    return schedules


def test_scenario_11_overnight_same_train_segments():
    # Board TO at MYS 22:45 day 1; seat change at KRNR (23:15 -> 23:20,
    # still day 1) and again at ASK (01:25 -> 01:30, day 2). The boarding
    # dates must follow IRCTC semantics: d, d, d+1.
    schedules = overnight_schedules()
    availability = full_map(schedules, {
        (TO, "MYS", "KRNR"): tc_avail(fare=60),
        (TO, "KRNR", "ASK"): tc_avail(fare=55),
        (TO, "ASK", "DWR"): tc_avail(fare=250),
    })
    body, status = mx_run(schedules, availability)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == (
        (TO, "MYS", "KRNR"), (TO, "KRNR", "ASK"), (TO, "ASK", "DWR"),
    )
    assert journey["journey_type"] == "SAME_TRAIN_SEGMENTS"
    assert journey["overnight"] is True
    assert journey["seat_changes"] == 2
    assert journey["train_changes"] == 0
    assert [s["boarding_query_date"] for s in journey["segments"]] == [_d(), _d(), _d(1)]
    assert [s["departure_day"] for s in journey["segments"]] == [1, 1, 2]
    assert [s["arrival_day"] for s in journey["segments"]] == [1, 2, 2]
    assert journey["departure_datetime"].endswith("22:45:00+05:30")
    assert journey["arrival_datetime"].startswith(_d(1))
    assert journey["total_duration_minutes"] == 478  # 22:45 -> 06:43
    assert journey["total_fare"] == 365.0
    # All three tickets belong to the same overnight train run.
    assert journey["distinct_trains"] == [TO]


def test_scenario_12_overnight_train_change():
    # Alight TO at KRNR 23:15 day 1; TX departs KRNR 05:00 day 2: a train
    # change whose transfer window itself crosses midnight (wait 345 min).
    schedules = overnight_schedules()
    schedules[TX] = tc_schedule(TX, [
        tc_stop(1, "KRNR", "Krishnarajanagar", None, "05:00", day=2, dist=35),
        tc_stop(2, "DWR", "Dharwad", "08:00", None, day=2, dist=440),
    ])
    availability = full_map(schedules, {
        (TO, "MYS", "KRNR"): tc_avail(fare=60),
        (TX, "KRNR", "DWR"): tc_avail(fare=300),
    })
    body, status = mx_run(schedules, availability)
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == ((TO, "MYS", "KRNR"), (TX, "KRNR", "DWR"))
    assert journey["journey_type"] == "TRAIN_CHANGE"
    assert journey["overnight"] is True
    assert journey["segments"][0]["boarding_query_date"] == _d()
    assert journey["segments"][1]["boarding_query_date"] == _d(1)
    transfer = journey["transitions"][0]
    assert transfer["transition_type"] == "TRAIN_CHANGE"
    assert transfer["station"]["code"] == "KRNR"
    assert transfer["arrival_datetime"].endswith("23:15:00+05:30")
    assert transfer["departure_datetime"].startswith(_d(1))
    assert transfer["wait_minutes"] == 345
    assert transfer["within_min_buffer"] is True
    assert transfer["within_max_wait"] is True
    assert journey["total_transfer_wait_minutes"] == 345


# =================================================================
# Scenario 13: confirmed availability across every segment
# =================================================================


def test_scenario_13_confirmed_availability_across_every_segment():
    call_log: list = []
    body, status = mx_run(master_schedules(), call_log=call_log)
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["total_results"] == 8
    assert body["total_unverified_candidates"] == 0
    assert_every_segment_confirmed(body)
    for journey in body["results"]:
        assert journey["distinct_train_count"] == len(journey["distinct_trains"])
        assert journey["segment_count"] == len(journey["segments"])
        assert journey["train_changes"] + journey["seat_changes"] == len(journey["segments"]) - 1
        for segment in journey["segments"]:
            assert segment["provider_status"] == "AVAILABLE"
            assert segment["availability_text"].startswith("AVAILABLE-")
            assert segment["fare"] is not None
            assert segment["checked_at"] is not None
            assert segment["boarding_query_date"] == _d()
            assert segment["boarding_query_date"] == segment["boarding_query_date"]
    # Every availability answer came back as an explicit provider AVAILABLE.
    assert all(item["train_number"] in (TA, TB, TC) for item in batch_items(call_log))


# =================================================================
# Scenarios 14-17: non-qualifying availability statuses
# =================================================================


def test_scenario_14_one_waitlist_segment():
    # (TA, MYS, ASK) on WAITLIST: every journey through it is a dead
    # branch; the four KRNR-rooted journeys survive.
    availability = full_map(master_schedules(), dict(MASTER_AVAIL))
    availability[(TA, "MYS", "ASK")] = tc_avail(status="WAITLIST", fare=165)
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert {journey_sig(j) for j in body["results"]} == {J_B, J_D, J_G, J_H}
    assert body["total_unverified_candidates"] == 0
    assert_every_segment_confirmed(body)
    for journey in body["results"]:
        assert (TA, "MYS", "ASK") not in journey_sig(journey)


def test_scenario_15_one_rac_segment():
    # (TA, KRNR, ASK) on RAC: every journey through it is a dead branch;
    # the four ASK-rooted journeys survive.
    availability = full_map(master_schedules(), dict(MASTER_AVAIL))
    availability[(TA, "KRNR", "ASK")] = tc_avail(status="RAC", fare=55)
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert {journey_sig(j) for j in body["results"]} == {J_A, J_C, J_E, J_F}
    assert body["total_unverified_candidates"] == 0
    assert_every_segment_confirmed(body)
    for journey in body["results"]:
        assert (TA, "KRNR", "ASK") not in journey_sig(journey)


def test_scenario_16_one_unknown_segment():
    # (TA, KRNR, ASK) has no availability data (UNKNOWN): journeys through
    # it are traversed but reported as unverified candidates, never results.
    availability = full_map(master_schedules(), dict(MASTER_AVAIL))
    availability[(TA, "KRNR", "ASK")] = tc_avail(status="UNKNOWN")
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert {journey_sig(j) for j in body["results"]} == {J_A, J_C, J_E, J_F}
    assert {journey_sig(j) for j in body["unverified_candidates"]} == {J_B, J_D, J_G, J_H}
    assert_every_segment_confirmed(body)
    for candidate in body["unverified_candidates"]:
        assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
        assert candidate["all_segments_confirmed_available"] is False
        assert candidate["unverified_segment_numbers"] == [2]
        assert candidate["segments"][1]["status"] == "UNKNOWN"


def test_scenario_17_provider_error_on_one_segment():
    # The provider fails to answer the (TA, KRNR, ASK) item in place: the
    # segment is UNKNOWN with an error payload - never unavailable. The
    # journeys through it are unverified candidates, the error is surfaced.
    call_log: list = []
    body, status = mx_run(
        master_schedules(),
        full_map(master_schedules(), dict(MASTER_AVAIL)),
        batch_item_failures={(TA, "KRNR", "ASK")},
        call_log=call_log,
    )
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert {journey_sig(j) for j in body["results"]} == {J_A, J_C, J_E, J_F}
    assert {journey_sig(j) for j in body["unverified_candidates"]} == {J_B, J_D, J_G, J_H}
    for candidate in body["unverified_candidates"]:
        assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
        segment = candidate["segments"][1]
        assert segment["status"] == "UNKNOWN"
        assert segment["error"]["code"] == "BOOKING_NOT_OPEN"
    assert any(error["code"] == "BOOKING_NOT_OPEN" for error in body["errors"])
    assert body["message"], "provider failure must be reported, never silent"


# =================================================================
# Scenarios 18-19: duplicates and cycles
# =================================================================


def test_scenario_18_duplicate_candidate_journeys():
    # The provider returns TA twice for the seed pair: the schedule is
    # fetched once, the direct-train reference is deduplicated, and all
    # eight journeys appear exactly once.
    call_log: list = []
    body, status = mx_run(
        master_schedules(),
        routes_duplicates={("MYS", "DWR"): 1},
        call_log=call_log,
    )
    assert status == 200
    ta_schedule_calls = [c for c in schedule_calls(call_log) if c["train_number"] == TA]
    assert len(ta_schedule_calls) == 1, "duplicate train must be schedule-fetched once"
    assert [d["train_number"] for d in body["direct_trains"]] == [TA]
    assert body["total_results"] == 8
    ids = [j["journey_id"] for j in body["results"]]
    signatures = [journey_sig(j) for j in body["results"]]
    assert len(ids) == len(set(ids))
    assert len(signatures) == len(set(signatures)), "duplicate journeys must be recorded once"
    assert {journey_sig(j) for j in body["results"]} == ALL_MASTER


def test_scenario_19_cycle_prevention():
    # TN is a circular service (revisits UBL, then runs to MYS). No
    # journey - confirmed result or unverified candidate - may revisit a
    # station or loop back to the source.
    schedules = master_schedules()
    schedules[TN] = tc_schedule(TN, [
        tc_stop(1, "UBL", "Hubballi Junction", None, "19:00"),
        tc_stop(2, "DWR", "Dharwad", "19:30", "19:35"),
        tc_stop(3, "BGM", "Belagavi", "21:00", "21:05"),
        tc_stop(4, "UBL", "Hubballi Junction", "23:00", "23:05"),  # second visit
        tc_stop(5, "MYS", "Mysuru Junction", "02:00", None, day=2),
    ])
    availability = full_map(schedules, dict(MASTER_AVAIL))
    availability[(TN, "UBL", "DWR")] = tc_avail(fare=110)
    availability[(TN, "UBL", "BGM")] = tc_avail(fare=140)

    body, status = mx_run(schedules, availability)
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["total_unverified_candidates"] == 0
    tn_journeys = [j for j in body["results"] if TN in trains_of(j)]
    assert len(tn_journeys) == 2  # board at TN's first UBL visit, alight DWR
    for section in ("results", "unverified_candidates"):
        for journey in body[section]:
            path = station_path(journey)
            assert len(set(path)) == len(path), f"station revisited in {path}"
            assert "MYS" not in path[1:], "journey must never loop back to the source"
            for segment in journey["segments"]:
                if segment["train_number"] == TN:
                    assert segment["from_station"]["code"] == "UBL"
                    assert segment["to_station"]["code"] in ("DWR",)
    assert body["search_completeness"]["complete"] is True


# =================================================================
# Scenarios 20-21: more than two changes of either kind
# =================================================================


def test_scenario_20_more_than_two_train_changes():
    # A four-train chain (three train changes, no seat changes) that has
    # no through train at all: via_stations hints bootstrap the corridor
    # discovery, pair by pair.
    schedules = {
        C1: tc_schedule(C1, [
            tc_stop(1, "MYS", "Mysuru Junction", None, "08:00"),
            tc_stop(2, "X1", "Chain One", "09:00", "09:05", dist=50),
            tc_stop(3, "X2", "Chain Two", "10:00", "10:05", dist=120),
        ]),
        C2: tc_schedule(C2, [
            tc_stop(1, "X1", "Chain One", None, "10:35"),
            tc_stop(2, "X2", "Chain Two", "11:30", "11:35", dist=70),
            tc_stop(3, "X3", "Chain Three", "12:30", "12:35", dist=140),
        ]),
        C3: tc_schedule(C3, [
            tc_stop(1, "X2", "Chain Two", None, "13:05"),
            tc_stop(2, "X3", "Chain Three", "14:00", "14:05", dist=70),
            tc_stop(3, "X4", "Chain Four", "15:00", "15:05", dist=140),
        ]),
        C4: tc_schedule(C4, [
            tc_stop(1, "X3", "Chain Three", None, "16:05"),
            tc_stop(2, "X4", "Chain Four", "17:00", "17:05", dist=70),
            tc_stop(3, "DWR", "Dharwad", "18:00", None, dist=210),
        ]),
    }
    availability = full_map(schedules, {
        (C1, "MYS", "X1"): tc_avail(fare=100),
        (C2, "X1", "X2"): tc_avail(fare=60),
        (C3, "X2", "X3"): tc_avail(fare=60),
        (C4, "X3", "DWR"): tc_avail(fare=60),
    })
    body, status = mx_run(schedules, availability, via_stations=["X1"])
    assert status == 200
    assert body["total_results"] == 1
    journey = body["results"][0]
    assert journey_sig(journey) == (
        (C1, "MYS", "X1"), (C2, "X1", "X2"), (C3, "X2", "X3"), (C4, "X3", "DWR"),
    )
    assert journey["journey_type"] == "TRAIN_CHANGE"
    assert journey["train_changes"] == 3
    assert journey["seat_changes"] == 0
    assert journey["segment_count"] == 4
    assert journey["distinct_trains"] == [C1, C2, C3, C4]
    assert journey["total_duration_minutes"] == 600  # 08:00 -> 18:00
    assert journey["total_transfer_wait_minutes"] == 95 + 95 + 125
    for transition in journey["transitions"]:
        assert transition["transition_type"] == "TRAIN_CHANGE"


def test_scenario_21_more_than_two_seat_changes():
    body, status = mx_run(master_schedules())
    assert status == 200
    journey = find_by_sig(body, J_H)
    assert journey is not None
    assert journey["seat_changes"] == 4
    assert journey["segment_count"] == 5
    assert journey["train_changes"] == 0
    assert journey["journey_type"] == "SAME_TRAIN_SEGMENTS"
    assert [s["segment_type"] for s in journey["segments"]] == [
        "FIRST_SEGMENT", "SAME_TRAIN_SEGMENT", "SAME_TRAIN_SEGMENT",
        "SAME_TRAIN_SEGMENT", "SAME_TRAIN_SEGMENT",
    ]


# =================================================================
# Scenario 22: missing fare data
# =================================================================


def test_scenario_22_missing_fare_data():
    availability = full_map(master_schedules(), dict(MASTER_AVAIL))
    availability[(TA, "UBL", "DWR")] = tc_avail(no_fare=True)  # AVAILABLE, fare unknown
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    journey_f = find_by_sig(body, J_F)
    journey_h = find_by_sig(body, J_H)
    journey_a = find_by_sig(body, J_A)
    assert journey_f is not None and journey_h is not None and journey_a is not None
    # Journeys through the fare-less segment: total fare unknown, never estimated.
    assert "total_fare" not in journey_f
    assert journey_f["fares_complete"] is False
    assert journey_f["segments_missing_fare"] == [4]
    assert "total_fare" not in journey_h
    assert journey_h["segments_missing_fare"] == [5]
    assert "fare" not in journey_f["segments"][3]
    # Journeys not touching it keep their exact fare sums.
    assert journey_a["total_fare"] == 375.0
    assert journey_a["fares_complete"] is True
    # Unknown fares rank last under CHEAPEST (never estimated, never 0).
    cheapest, _ = mx_run(
        master_schedules(), availability, ranking_preference="CHEAPEST"
    )
    fares = [j.get("total_fare") for j in cheapest["results"]]
    assert fares[:6] == sorted(f for f in fares if f is not None)[:6]
    assert find_by_sig(cheapest, J_F) in cheapest["results"][-2:]


# =================================================================
# Scenarios 23-26: the four ranking preferences
# =================================================================


def _fare_of(journey: dict):
    return journey.get("total_fare")


def test_scenario_23_cheapest_ranking():
    body, _ = mx_run(master_schedules(), ranking_preference="CHEAPEST")
    assert body["ranked_by"] == "CHEAPEST"
    fares = [j["total_fare"] for j in body["results"]]
    assert fares == sorted(fares), "CHEAPEST must order by total fare"
    assert journey_sig(body["results"][0]) == J_D  # 280 INR
    assert body["results"][0]["total_fare"] == 280.0


def test_scenario_24_fastest_ranking():
    body, _ = mx_run(master_schedules(), ranking_preference="FASTEST")
    assert body["ranked_by"] == "FASTEST"
    durations = [j["total_duration_minutes"] for j in body["results"]]
    assert durations == sorted(durations), "FASTEST must order by duration"
    assert journey_sig(body["results"][0]) == J_H  # 570 minutes, 415 INR tiebreak
    assert body["results"][0]["total_duration_minutes"] == 570


def test_scenario_25_fewest_train_changes_ranking():
    body, _ = mx_run(master_schedules(), ranking_preference="FEWEST_TRAIN_CHANGES")
    assert body["ranked_by"] == "FEWEST_TRAIN_CHANGES"
    changes = [j["train_changes"] for j in body["results"]]
    assert changes == sorted(changes), "FEWEST_TRAIN_CHANGES must order by train changes"
    assert journey_sig(body["results"][0]) == J_H  # zero train changes
    assert body["results"][0]["train_changes"] == 0

    # A dedicated three-journey network where the orderings genuinely differ:
    # cheapest = 2-change chain, fastest = 1-change, fewest-changes = same-train.
    schedules = {
        R1: tc_schedule(R1, [
            tc_stop(1, "MYS", "Mysuru Junction", None, "08:00"),
            tc_stop(2, "A", "Rank Junction A", "09:00", "09:05", dist=50),
            tc_stop(3, "DWR", "Dharwad", "13:00", None, dist=300),
        ]),
        R2: tc_schedule(R2, [
            tc_stop(1, "A", "Rank Junction A", None, "10:05"),
            tc_stop(2, "B", "Rank Junction B", "11:00", "11:05", dist=60),
            tc_stop(3, "DWR", "Dharwad", "12:00", None, dist=120),
        ]),
        R3: tc_schedule(R3, [
            tc_stop(1, "B", "Rank Junction B", None, "12:05"),
            tc_stop(2, "DWR", "Dharwad", "12:35", None, dist=30),
        ]),
    }
    availability = full_map(schedules, {
        (R1, "MYS", "A"): tc_avail(fare=100),
        (R1, "A", "DWR"): tc_avail(fare=120),
        (R2, "A", "DWR"): tc_avail(fare=250),
        (R2, "A", "B"): tc_avail(fare=60),
        (R3, "B", "DWR"): tc_avail(fare=50),
    })
    body, _ = mx_run(schedules, availability, ranking_preference="FEWEST_TRAIN_CHANGES")
    assert body["total_results"] == 3
    cheapest, _ = mx_run(schedules, availability, ranking_preference="CHEAPEST")
    fastest, _ = mx_run(schedules, availability, ranking_preference="FASTEST")
    assert journey_sig(body["results"][0]) == ((R1, "MYS", "A"), (R1, "A", "DWR"))  # 0 changes
    assert journey_sig(cheapest["results"][0]) == (
        (R1, "MYS", "A"), (R2, "A", "B"), (R3, "B", "DWR")
    )  # 210 INR
    assert journey_sig(fastest["results"][0]) == ((R1, "MYS", "A"), (R2, "A", "DWR"))  # 240 min


def test_scenario_26_fewest_seat_changes_ranking():
    body, _ = mx_run(master_schedules(), ranking_preference="FEWEST_SEAT_CHANGES")
    assert body["ranked_by"] == "FEWEST_SEAT_CHANGES"
    seats = [j["seat_changes"] for j in body["results"]]
    assert seats == sorted(seats), "FEWEST_SEAT_CHANGES must order by seat changes"
    assert journey_sig(body["results"][0]) == J_A  # zero seat changes
    assert body["results"][0]["seat_changes"] == 0
    # Fewest seat changes keeps zero-seat journeys ahead of one-seat ones
    # even when the one-seat journey is cheaper.
    assert [j["seat_changes"] for j in body["results"]][:3] == [0, 1, 1]
    # Within the same seat count the tiebreak is deterministic (changes,
    # duration, fare, segment count, journey id).
    keys = [
        (j["seat_changes"], j["train_changes"], j["total_duration_minutes"],
         j.get("total_fare"), j["segment_count"], j["journey_id"])
        for j in body["results"]
    ]
    assert keys == sorted(keys)


# =================================================================
# Scenario 27: search budget exhaustion (before any API request)
# =================================================================


def test_scenario_27_request_budget_exhausted_before_any_availability_call():
    call_log: list = []
    budgets = big_budgets(max_api_requests=3)
    body, status = mx_run(master_schedules(), budgets=budgets, call_log=call_log)
    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert body["total_results"] == 0
    reasons = body["search_completeness"]["incomplete_reasons"]
    assert "BUDGET_REQUESTS_EXHAUSTED" in reasons
    # Budget enforcement BEFORE requests: exactly 3 provider calls happened
    # (seed discovery + TA schedule + one seed-pair discovery), none over budget.
    assert len(call_log) == 3
    assert len(batch_items(call_log)) == 0, "no availability call may exceed the request budget"
    assert body["search_completeness"]["budget"]["requests_used"] == 3
    assert body["search_completeness"]["budget"]["requests_used"] <= budgets.max_api_requests
    assert "NOT all possibilities were checked" in body["message"]


def test_scenario_27_credit_budget_exhausted_before_requests():
    call_log: list = []
    budgets = big_budgets(max_credits=2)
    body, status = mx_run(master_schedules(), budgets=budgets, call_log=call_log)
    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert "BUDGET_CREDITS_EXHAUSTED" in body["search_completeness"]["incomplete_reasons"]
    # 1 discovery credit + 1 schedule credit = the whole budget: nothing else
    # was sent, and never a seat-availability call.
    assert len(call_log) == 2
    assert len(batch_items(call_log)) == 0
    assert body["search_completeness"]["budget"]["credits_used_estimate"] <= budgets.max_credits


# =================================================================
# Scenario 28: search horizon exhaustion
# =================================================================


def test_scenario_28_search_horizon_exhaustion():
    schedules = overnight_schedules()
    availability = full_map(schedules, {
        (TO, "MYS", "KRNR"): tc_avail(fare=60),
        (TO, "KRNR", "ASK"): tc_avail(fare=55),
        (TO, "ASK", "DWR"): tc_avail(fare=250),
    })
    # Horizon 0 (same-day arrivals only): the overnight legs are rejected
    # during leg generation - before ANY availability query for them.
    call_log: list = []
    body, status = mx_run(
        schedules, availability, budgets=big_budgets(search_horizon_days=0), call_log=call_log
    )
    assert status == 200
    # The horizon is a configured limit that truncated the search: with no
    # fully confirmed journey found, the honest status is SEARCH_LIMIT_REACHED
    # (never a claim that no journey exists).
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert body["total_results"] == 0
    reasons = body["search_completeness"]["incomplete_reasons"]
    assert "HORIZON_LIMIT_REACHED" in reasons
    scope = body["search_completeness"]["search_scope"]
    assert scope["search_horizon_days"] == 0
    assert scope["horizon_end_date"] == _d()
    for journey in body["results"]:
        assert journey["arrival_datetime"].startswith(_d())
        assert journey["overnight"] is False
    assert not any(
        item["train_number"] == TO and item["to"] == "ASK"
        for item in batch_items(call_log)
    ), "beyond-horizon leg must be rejected before any availability query"

    # With the default 2-day horizon the same overnight journey is found.
    body, status = mx_run(schedules, availability)
    assert status == 200
    assert body["total_results"] == 1
    assert journey_sig(body["results"][0]) == (
        (TO, "MYS", "KRNR"), (TO, "KRNR", "ASK"), (TO, "ASK", "DWR"),
    )


# =================================================================
# Scenario 29: query deduplication
# =================================================================


def test_scenario_29_query_deduplication():
    # ASK is expanded twice (two arrival events at 10:30 on TA: the direct
    # MYS->ASK leg and the seat-changed MYS->KRNR->ASK path). The identical
    # (TA, ASK, SBC) availability query must hit the provider exactly once.
    call_log: list = []
    body, status = mx_run(master_schedules(), call_log=call_log)
    assert status == 200
    items = batch_items(call_log)
    assert items, "expected availability queries"
    ask_report = next(s for s in body["stations"] if s["station_code"] == "ASK")
    assert ask_report["events_expanded"] == 2
    ask_sbc = [
        item for item in items
        if item["train_number"] == TA and item["from"] == "ASK" and item["to"] == "SBC"
    ]
    assert len(ask_sbc) == 1, "identical (train, from, to, date) queries must deduplicate"
    seen = {(item["train_number"], item["from"], item["to"], item["date"]) for item in items}
    assert len(seen) == len(items), "the same segment may never be queried twice within one search"
    completeness = body["search_completeness"]
    assert completeness["segments_checked"] == len(items)
    assert completeness["distinct_availability_queries"] == len(items)


# =================================================================
# Scenario 30: an unverified journey can never be labelled fully available
# =================================================================


def test_scenario_30_unverified_journey_never_fully_available():
    availability = full_map(master_schedules(), dict(MASTER_AVAIL))
    availability[(TA, "KRNR", "ASK")] = tc_avail(status="UNKNOWN")
    body, status = mx_run(master_schedules(), availability)
    assert status == 200
    assert body["success"] is True
    results = body["results"]
    candidates = body["unverified_candidates"]
    assert candidates, "unverified candidates expected"
    # No journey that was never fully verified may appear as a result.
    result_signatures = {journey_sig(j) for j in results}
    for candidate in candidates:
        assert journey_sig(candidate) not in result_signatures
        assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
        assert candidate["all_segments_confirmed_available"] is False
        assert candidate["unverified_segment_numbers"]
        for number in candidate["unverified_segment_numbers"]:
            assert candidate["segments"][number - 1]["status"] != CONFIRMED
    for journey in results:
        assert journey["journey_status"] == "AVAILABLE_COMBINATION"
        assert journey["all_segments_confirmed_available"] is True
        assert journey["unverified_segment_numbers"] == []


# =================================================================
# Integration: API-level behaviour, budgets, configuration, OpenAPI
# =================================================================


def test_api_happy_path_and_response_shape(make_test_client):
    schedules = master_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MASTER_AVAIL),
    )
    test_client = make_test_client(handler)
    response = test_client.post(SEARCH_URL, json=mx_body())
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["success"] is True
    assert body["total_results"] == 8
    assert {journey_sig(j) for j in body["results"]} == ALL_MASTER
    assert body["unverified_candidates"] == []
    assert len({j["journey_id"] for j in body["results"]}) == 8

    # Direct through trains are reference-only: availability NOT checked.
    assert [d["train_number"] for d in body["direct_trains"]] == [TA]
    assert all(d["availability_checked"] is False for d in body["direct_trains"])

    completeness = body["search_completeness"]
    assert completeness["complete"] is True
    assert completeness["incomplete_reasons"] == []
    assert completeness["budget"]["requests_used"] > 0
    assert completeness["search_scope"]["engine"] == "mixed"
    assert completeness["search_scope"]["min_transfer_buffer_minutes"] == 30
    assert completeness["search_scope"]["max_transfer_wait_minutes"] == 720
    assert body["api_usage"]["provider"] == "railcore"
    assert body["ranked_by"] == "FASTEST"
    assert any("does not book tickets" in note for note in body["notes"])

    # Explicit typing: a seat change is never labelled a train transfer.
    for journey in body["results"]:
        for transition in journey["transitions"]:
            if transition["transition_type"] == "SAME_TRAIN_SEAT_CHANGE":
                assert transition["is_train_change"] is False
                assert "wait_minutes" not in transition
                assert "within_min_buffer" not in transition
                assert "within_max_wait" not in transition
            else:
                assert transition["transition_type"] == "TRAIN_CHANGE"
                assert transition["is_train_change"] is True
                assert "wait_minutes" in transition
                assert "within_min_buffer" in transition
                assert "within_max_wait" in transition


def test_openapi_documents_mixed_endpoint(make_test_client):
    schedules = master_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MASTER_AVAIL),
    )
    test_client = make_test_client(handler)
    schema = test_client.get("/openapi.json").json()

    assert "/api/v1/journeys/mixed/search" in schema["paths"]
    post = schema["paths"]["/api/v1/journeys/mixed/search"]["post"]
    assert "mixed" in post["summary"].lower()
    request_ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert request_ref.endswith("MixedSearchRequest")
    response_ref = post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert response_ref.endswith("MixedSearchResponse")

    components = schema["components"]["schemas"]
    for component in (
        "MixedSearchRequest",
        "MixedSearchResponse",
        "MixedJourneyOut",
        "MixedSegmentOut",
        "MixedTransitionOut",
        "MixedCompletenessOut",
    ):
        assert component in components, f"schema component {component} missing"
    # The four ranking preferences are documented on the request.
    request_schema = components["MixedSearchRequest"]
    preference = request_schema["properties"]["ranking_preference"]
    values = set(preference.get("enum") or [])
    if not values and "$ref" in preference:
        values = set(components[preference["$ref"].split("/")[-1]]["enum"])
    assert values == {"CHEAPEST", "FASTEST", "FEWEST_TRAIN_CHANGES", "FEWEST_SEAT_CHANGES"}
    # Segment types and transition types are documented.
    segment_schema = components["MixedSegmentOut"]["properties"]["segment_type"]
    assert "SAME_TRAIN_SEGMENT" in segment_schema["description"]
    assert "TRAIN_CHANGE" in segment_schema["description"]


def test_mixed_budgets_from_settings(monkeypatch):
    monkeypatch.setenv("MIXED_MIN_TRANSFER_BUFFER_MINUTES", "17")
    monkeypatch.setenv("MIXED_MAX_TRANSFER_WAIT_MINUTES", "300")
    monkeypatch.setenv("MIXED_SEARCH_HORIZON_DAYS", "1")
    monkeypatch.setenv("MIXED_MAX_DISCOVERY_PAIRS", "9")
    monkeypatch.setenv("MIXED_MAX_TRAINS", "4")
    monkeypatch.setenv("MIXED_MAX_API_REQUESTS", "11")
    monkeypatch.setenv("MIXED_MAX_CREDITS", "12")
    monkeypatch.setenv("MIXED_TIME_BUDGET_SECONDS", "3.5")
    monkeypatch.setenv("MIXED_MAX_EXPANDED_STATES", "40")
    monkeypatch.setenv("MIXED_MAX_JOURNEYS", "5")
    monkeypatch.setenv("MIXED_MAX_RESULTS", "6")
    budgets = MixedJourneyBudgets.from_settings(Settings())
    assert budgets.min_transfer_buffer_minutes == 17
    assert budgets.max_transfer_wait_minutes == 300
    assert budgets.search_horizon_days == 1
    assert budgets.max_discovery_pairs == 9
    assert budgets.max_trains == 4
    assert budgets.max_api_requests == 11
    assert budgets.max_credits == 12
    assert budgets.time_budget_seconds == 3.5
    assert budgets.max_expanded_states == 40
    assert budgets.max_journeys == 5
    assert budgets.max_results == 6


def test_mixed_budget_clamps_and_override_validation():
    budgets = MixedJourneyBudgets(
        min_transfer_buffer_minutes=-5,
        max_transfer_wait_minutes=-5,
        search_horizon_days=99,
        max_discovery_pairs=0,
        max_trains=0,
        max_api_requests=0,
        max_credits=0,
        time_budget_seconds=0.0,
        max_expanded_states=0,
        max_journeys=0,
        max_results=0,
        batch_size=99,
    )
    assert budgets.min_transfer_buffer_minutes == 0
    assert budgets.max_transfer_wait_minutes >= budgets.min_transfer_buffer_minutes
    assert budgets.search_horizon_days <= 7
    assert budgets.max_discovery_pairs == 1
    assert budgets.max_trains == 1
    assert budgets.max_api_requests == 1
    assert budgets.max_credits == 1
    assert budgets.time_budget_seconds >= 0.5
    assert budgets.max_expanded_states == 1
    assert budgets.max_journeys == 1
    assert budgets.max_results == 1
    assert budgets.batch_size <= 25

    with pytest.raises(ValueError):
        budgets.with_overrides(50, 10)  # wait below buffer
    ok = budgets.with_overrides(5, 300)
    assert ok.min_transfer_buffer_minutes == 5
    assert ok.max_transfer_wait_minutes == 300
    assert isinstance(ok, MixedJourneyBudgets)


def test_request_validation_errors(make_test_client):
    test_client = make_test_client(make_handler())
    invalid_payloads = [
        mx_body(from_station="DWR"),  # source equals destination
        mx_body(ranking_preference="FEWEST_CHANGES"),  # not a mixed preference
        mx_body(via_stations=["MYS"]),  # via may not contain the source
        mx_body(min_transfer_buffer_minutes=120, max_transfer_wait_minutes=60),  # wait < buffer
        mx_body(travel_class="XX"),  # class not offered by any railway
        mx_body(journey_date="2020-01-01"),  # past date
        mx_body(from_station="toolongstation"),  # invalid station code
    ]
    for payload in invalid_payloads:
        response = test_client.post(SEARCH_URL, json=payload)
        assert response.status_code == 422, f"expected 422 for {payload}, got {response.status_code}"
    # The mixed engine accepts its own four preferences (and rejects nothing valid).
    for preference in ("CHEAPEST", "FASTEST", "FEWEST_TRAIN_CHANGES", "FEWEST_SEAT_CHANGES"):
        response = test_client.post(SEARCH_URL, json=mx_body(ranking_preference=preference))
        assert response.status_code == 200
        # The handler has no routes: discovery finds no trains (an honest
        # negative, never an error).
        assert response.json()["status"] in (
            "NO_CANDIDATE_TRAINS",
            "AVAILABLE_COMBINATION",
            "NO_COMPLETE_JOURNEY",
        )


def test_missing_api_key_is_config_error_503():
    schedules = master_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MASTER_AVAIL),
    )
    client = make_client(handler, api_key="")
    service = MixedJourneyCombinationService(client, big_budgets())
    body, status = service.run(MixedSearchRequest(**mx_body()))
    assert status == 503
    assert body["status"] == "CONFIG_ERROR"
    assert body["total_results"] == 0
    assert "RAILCORE_API_KEY" in body["message"]


def test_seed_discovery_provider_failure_is_502():
    def failing_routes(query):
        return httpx.Response(
            503,
            json=error_envelope("UPSTREAM_UNAVAILABLE", "provider down", "SERVER", retryable=True),
            headers={"retry-after": "0"},
        )

    schedules = master_schedules()
    handler = make_handler(
        route_trains_responder=failing_routes,
        schedules=schedules,
        availability_by_train=full_map(schedules, MASTER_AVAIL),
    )
    client = make_client(handler)
    service = MixedJourneyCombinationService(client, big_budgets())
    body, status = service.run(MixedSearchRequest(**mx_body()))
    assert status == 502
    assert body["status"] == "PROVIDER_ERROR"
    assert body["total_results"] == 0
    assert body["errors"], "the discovery failure must be surfaced"
    assert body["search_completeness"]["incomplete_reasons"] == ["PROVIDER_ERROR"]


def test_no_through_trains_honest_negative():
    body, status = mx_run({})  # empty network: every discovery pair 404s
    assert status == 200
    assert body["status"] == "NO_CANDIDATE_TRAINS"
    assert body["total_results"] == 0
    assert "via_stations" in body["message"]
    assert body["errors"] == []


def test_time_limit_stops_search():
    seconds = 0.25
    schedules = master_schedules()
    base_routes = routes_auto(schedules)

    def slow_routes(query):
        time.sleep(seconds)
        return base_routes(query)

    def slow_schedules(request):
        time.sleep(seconds)
        train = request.url.path.split("/")[-2]
        payload = schedules.get(train)
        if payload is None:
            return httpx.Response(404, json=error_envelope("TRAIN_NOT_FOUND", f"no {train}", "CLIENT"))
        return httpx.Response(200, json=payload, headers=usage_headers(f"req_sched_{train}"))

    handler = make_handler(
        route_trains_responder=slow_routes,
        schedule_responder=slow_schedules,
        schedules=schedules,
        availability_by_train=full_map(schedules, MASTER_AVAIL),
    )
    client = make_client(handler)
    service = MixedJourneyCombinationService(client, big_budgets(time_budget_seconds=0.8))
    body, status = service.run(MixedSearchRequest(**mx_body()))
    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert "TIME_LIMIT_REACHED" in body["search_completeness"]["incomplete_reasons"]
    used = body["search_completeness"]["budget"]["requests_used"]
    assert 3 <= used <= 8, "the time budget must stop the search after a few calls"
    assert "NOT all possibilities were checked" in body["message"]


def test_direct_train_availability_never_queried():
    call_log: list = []
    body, status = mx_run(master_schedules(), call_log=call_log)
    assert status == 200
    items = batch_items(call_log)
    assert items, "expected availability queries"
    assert not any(
        item["train_number"] == TA and item["from"] == "MYS" and item["to"] == "DWR"
        for item in items
    ), "the direct through-train segment must never be availability-checked"


# =================================================================
# Cross-engine consistency: phases 2 and 3 stay independent and the
# mixed engine reuses their semantics on the same network
# =================================================================


def test_phase2_phase3_and_mixed_engines_consistent_on_the_same_network():
    # One mocked network, three engines, one shared provider client: the
    # mixed engine's same-train-only journeys equal the phase 2 engine's
    # combinations, and the phase 3 engine's pure train-change journeys are
    # a subset of the mixed results (phase 3 finds the one connection it
    # can see; mixed additionally finds everything involving seat changes).
    schedules = master_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MASTER_AVAIL),
    )
    client = make_client(handler)

    mixed_body, _ = MixedJourneyCombinationService(client, big_budgets()).run(
        MixedSearchRequest(**mx_body())
    )
    phase3_body, _ = TrainChangeConnectionService(
        client,
        TrainChangeBudgets(
            min_transfer_buffer_minutes=30,
            max_transfer_wait_minutes=720,
            search_horizon_days=2,
            max_discovery_pairs=60,
            max_trains=20,
            max_api_requests=200,
            max_credits=400,
            time_budget_seconds=30.0,
            max_expanded_states=500,
            max_journeys=100,
            max_results=100,
        ),
    ).run(
        TrainChangeSearchRequest(
            from_station="MYS",
            to_station="DWR",
            journey_date=tomorrow_ist(),
            travel_class="SL",
            quota="GN",
        )
    )
    phase2_body, _ = SameTrainCombinationService(
        client,
        SearchBudgets(
            max_trains=5,
            max_api_requests=100,
            max_credits=200,
            time_budget_seconds=30.0,
            max_combinations_per_train=50,
            max_results=50,
        ),
    ).run(
        SameTrainSearchRequest(
            from_station="MYS",
            to_station="DWR",
            journey_date=tomorrow_ist(),
            travel_class="SL",
            quota="GN",
        )
    )

    mixed_results = {journey_sig(j) for j in mixed_body["results"]}
    phase2_signatures = {journey_sig(j) for j in phase2_body["results"]}
    phase3_signatures = {journey_sig(j) for j in phase3_body["results"]}

    # Phase 2 finds exactly the two same-train combinations (J_F, J_H).
    assert phase2_signatures == {J_F, J_H}
    # Phase 3 finds exactly the one pure train-change journey it can see
    # (no same-train continuations are allowed there).
    assert phase3_signatures == {J_A}
    # The mixed engine finds all of them plus the genuinely mixed journeys.
    assert phase2_signatures | phase3_signatures <= mixed_results
    assert mixed_results == ALL_MASTER

    # The mixed engine's same-train-only journeys equal phase 2's output,
    # and its no-seat-change journeys include phase 3's output.
    same_train_only = {
        sig
        for sig in mixed_results
        if all(seg[0] == sig[0][0] for seg in sig)
    }
    assert same_train_only == phase2_signatures


def test_cross_search_schedule_and_discovery_cache_reuse():
    schedules = master_schedules()
    handler = make_handler(
        route_trains_responder=routes_auto(schedules),
        schedules=schedules,
        availability_by_train=full_map(schedules, MASTER_AVAIL),
    )
    client = make_client(handler)
    request = MixedSearchRequest(**mx_body())

    first, _ = MixedJourneyCombinationService(client, big_budgets()).run(request)
    second, _ = MixedJourneyCombinationService(client, big_budgets()).run(request)

    assert second["search_completeness"]["schedule_cache_hits"] > 0
    assert second["search_completeness"]["discovery_cache_hits"] > 0
    assert {journey_sig(j) for j in first["results"]} == {journey_sig(j) for j in second["results"]}
    # Availability is never cached across searches (freshness).
    assert second["search_completeness"]["distinct_availability_queries"] > 0
