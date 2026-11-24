"""Train-change connection search engine tests (Phase 3).

Covers POST /api/v1/journeys/train-change/search and the engine service
directly, against a fully synthetic rail network (trains 998xx, mirroring
the MYS -> DWR product scenario but with fictional schedules and
configurable availability). Every provider response is mocked; no test
ever contacts the real RailCore API.

Required scenarios (numbered per the phase brief):
 1 one train change            11 UNKNOWN segment
 2 two train changes            12 provider error during verification
 3 several valid connections   13 duplicate candidate trains
 4 min transfer buffer          14 cycle / repeated station path
 5 max transfer wait           15 query-budget exhaustion
 6 no stop at interchange       16 time-limit / horizon exhaustion
 7 overnight crossing midnight 17 cheapest/fastest/fewest-changes ranking
 8 confirmed availability       18 missing fare data
 9 WAITLIST segment             19 more than two train changes
10 RAC segment                  20 unverified segment never recommended

plus: budget enforcement BEFORE API requests, direct trains never
availability-checked, via_stations bootstrap, no-through-train honest
negative, validation, OpenAPI docs, settings wiring and cache reuse.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import Settings
from app.schemas.train_change_search import TrainChangeSearchRequest
from app.services.search_budgets import TrainChangeBudgets
from app.services.segment_query_planner import SegmentQueryPlanner
from app.services.train_change_connection_service import TrainChangeConnectionService

from tests.conftest import (
    error_envelope,
    make_client,
    make_handler,
    tc_avail,
    tc_route_entry,
    tc_routes,
    tc_routes_responder,
    tc_schedule,
    tc_stop,
    tomorrow_ist,
    usage_headers,
)

SEARCH_URL = "/api/v1/journeys/train-change/search"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Synthetic train numbers (998xx do not exist; never queried live).
TA = "99801"  # MYS 08:00 -> HAS 10:05 -> UBL 13:00
TB = "99802"  # UBL 15:00 -> DWR 16:30
TC = "99803"  # MYS 22:45 d1 -> ASK 01:27 d2 -> UBL 06:20 d2 -> DWR 06:45 d2 -> BGM 09:55 d2
TD = "99804"  # ASK 05:00 -> UBL 08:05 -> DWR 09:30
TE = "99805"  # UBL 09:00 -> DWR 10:00
TF = "99806"  # HAS 11:00 -> ASK 13:00
TH = "99808"  # ASK 14:30 -> DWR 19:00
TI = "99809"  # ASK 13:30 -> UBL 16:00
TJ = "99810"  # UBL 17:00 -> DWR 18:30
TK = "99811"  # ASK 13:20 -> UBL 16:20
TL = "99812"  # HAS 23:00 d1 -> DWR 08:00 d2 (overnight)
TM = "99813"  # MYS 07:00 -> (ASK, pass-through) -> UBL 12:05 -> DWR 13:30
TN = "99814"  # UBL 19:05 -> DWR 19:35 -> BGM 21:05 -> UBL 23:05 -> MYS 02:00 d2 (circular)

CONFIRMED = "CONFIRMED_AVAILABLE"


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
            tc_stop(2, "HAS", "Hassan", "10:00", "10:05", dist=50),
            tc_stop(3, "UBL", "Hubballi Junction", "13:00", None, dist=250),
        ]),
        TB: tc_schedule(TB, [
            tc_stop(1, "UBL", "Hubballi Junction", None, "15:00"),
            tc_stop(2, "DWR", "Dharwad", "16:30", None, dist=30),
        ]),
        TC: tc_schedule(TC, [
            tc_stop(1, "MYS", "Mysuru Junction", None, "22:45", day=1),
            tc_stop(2, "ASK", "Arsikere Junction", "01:25", "01:27", day=2, dist=80),
            tc_stop(3, "UBL", "Hubballi Junction", "06:10", "06:20", day=2, dist=330),
            tc_stop(4, "DWR", "Dharwad", "06:43", "06:45", day=2, dist=360),
            tc_stop(5, "BGM", "Belagavi", "09:55", None, day=2, dist=490),
        ]),
        TD: tc_schedule(TD, [
            tc_stop(1, "ASK", "Arsikere Junction", None, "05:00"),
            tc_stop(2, "UBL", "Hubballi Junction", "08:00", "08:05", dist=180),
            tc_stop(3, "DWR", "Dharwad", "09:30", None, dist=210),
        ]),
        TE: tc_schedule(TE, [
            tc_stop(1, "UBL", "Hubballi Junction", None, "09:00"),
            tc_stop(2, "DWR", "Dharwad", "10:00", None, dist=30),
        ]),
        TF: tc_schedule(TF, [
            tc_stop(1, "HAS", "Hassan", None, "11:00"),
            tc_stop(2, "ASK", "Arsikere Junction", "13:00", None, dist=60),
        ]),
        TH: tc_schedule(TH, [
            tc_stop(1, "ASK", "Arsikere Junction", None, "14:30"),
            tc_stop(2, "DWR", "Dharwad", "19:00", None, dist=200),
        ]),
        TI: tc_schedule(TI, [
            tc_stop(1, "ASK", "Arsikere Junction", None, "13:30"),
            tc_stop(2, "UBL", "Hubballi Junction", "16:00", None, dist=150),
        ]),
        TJ: tc_schedule(TJ, [
            tc_stop(1, "UBL", "Hubballi Junction", None, "17:00"),
            tc_stop(2, "DWR", "Dharwad", "18:30", None, dist=30),
        ]),
        TK: tc_schedule(TK, [
            tc_stop(1, "ASK", "Arsikere Junction", None, "13:20"),
            tc_stop(2, "UBL", "Hubballi Junction", "16:20", None, dist=150),
        ]),
        TL: tc_schedule(TL, [
            tc_stop(1, "HAS", "Hassan", None, "23:00", day=1),
            tc_stop(2, "DWR", "Dharwad", "08:00", None, day=2, dist=260),
        ]),
    }


# Distinct legs queried by the master scenario, with fares chosen so the
# CHEAPEST / FASTEST / FEWEST_CHANGES orderings all differ.
MASTER_FARES: dict[tuple[str, str, str], float] = {
    (TA, "MYS", "HAS"): 120, (TA, "MYS", "UBL"): 300,
    (TB, "UBL", "DWR"): 100,
    (TC, "MYS", "ASK"): 250, (TC, "MYS", "UBL"): 400, (TC, "MYS", "BGM"): 450,
    (TD, "ASK", "UBL"): 120, (TD, "ASK", "DWR"): 200, (TD, "UBL", "DWR"): 60,
    (TE, "UBL", "DWR"): 95,
    (TF, "HAS", "ASK"): 50,
    (TH, "ASK", "DWR"): 150,
    (TI, "ASK", "UBL"): 80,
    (TJ, "UBL", "DWR"): 90,
    (TK, "ASK", "UBL"): 75,
    (TL, "HAS", "DWR"): 210,
}

# The 12 distinct train sequences the master network yields (the (TC, TD)
# sequence occurs twice: change at ASK and change at UBL -> 13 journeys).
EXPECTED_MASTER_SEQUENCES = {
    (TA, TB), (TA, TJ), (TA, TF, TH), (TA, TF, TI, TJ),
    (TC, TD), (TC, TD, TB), (TC, TD, TE), (TC, TD, TJ),
    (TC, TE), (TC, TB), (TC, TJ), (TC, TK, TJ),
}


def master_availability(overrides: dict | None = None) -> dict:
    mapping = {
        (train, frm, to): tc_avail(fare=fare)
        for (train, frm, to), fare in MASTER_FARES.items()
    }
    if overrides:
        mapping.update(overrides)
    return mapping


def master_routes() -> dict[tuple[str, str], dict]:
    pairs: dict[tuple[str, str], list[str]] = {
        ("MYS", "DWR"): [TC],
        ("MYS", "ASK"): [TC],
        ("MYS", "UBL"): [TA],
        ("MYS", "HAS"): [TA],
        ("HAS", "DWR"): [TL],
        ("HAS", "UBL"): [TA],
        ("HAS", "ASK"): [TF],
        ("ASK", "DWR"): [TD, TH],
        ("ASK", "UBL"): [TC, TD, TI, TK],
        ("UBL", "DWR"): [TB, TE, TJ, TD],
    }
    schedules = master_schedules()

    def entry(train: str, frm: str, to: str) -> dict:
        stops = schedules[train]["data"]["stops"]
        dep = next(s["departure_time"] for s in stops if s["station_code"] == frm)
        arr = next(s["arrival_time"] for s in stops if s["station_code"] == to)
        return tc_route_entry(train, frm=frm, to=to, dep=dep, arr=arr)

    return {
        (frm, to): tc_routes(frm, to, [entry(train, frm, to) for train in trains])
        for (frm, to), trains in pairs.items()
    }


def big_budgets(**overrides) -> TrainChangeBudgets:
    params = dict(
        min_transfer_buffer_minutes=30,
        max_transfer_wait_minutes=720,
        search_horizon_days=2,
        max_discovery_pairs=40,
        max_trains=20,
        max_api_requests=120,
        max_credits=250,
        time_budget_seconds=30.0,
        max_expanded_states=200,
        max_journeys=80,
        max_results=80,
    )
    params.update(overrides)
    return TrainChangeBudgets(**params)


def journey_body_tc(**overrides) -> dict:
    base = {
        "from_station": "MYS",
        "to_station": "DWR",
        "journey_date": tomorrow_ist(),
        "travel_class": "SL",
        "quota": "GN",
    }
    base.update(overrides)
    return base


def tc_run(
    routes: dict,
    schedules: dict | None = None,
    availability: dict | None = None,
    *,
    budgets: TrainChangeBudgets | None = None,
    batch_item_failures=None,
    batch_responder=None,
    call_log: list | None = None,
    handler_extras: dict | None = None,
    **body_overrides,
):
    handler = make_handler(
        route_trains_responder=tc_routes_responder(routes),
        schedules=schedules if schedules is not None else master_schedules(),
        availability_by_train=availability or master_availability(),
        batch_item_failures=batch_item_failures,
        batch_responder=batch_responder,
        call_log=call_log,
        **(handler_extras or {}),
    )
    client = make_client(handler)
    service = TrainChangeConnectionService(client, budgets or big_budgets())
    request = TrainChangeSearchRequest(**journey_body_tc(**body_overrides))
    body, status = service.run(request)
    return body, status


# ---------------------------------------------------------------- helpers


def trains_of(journey: dict) -> tuple:
    return tuple(segment["train_number"] for segment in journey["segments"])


def station_path(journey: dict) -> list[str]:
    codes = [journey["segments"][0]["from_station"]["code"]]
    codes.extend(segment["to_station"]["code"] for segment in journey["segments"])
    return codes


def find_journey(body: dict, trains, *, section: str = "results"):
    target = tuple(trains)
    for journey in body[section]:
        if trains_of(journey) == target:
            return journey
    return None


def batch_items(call_log: list) -> list[dict]:
    return [
        item
        for call in call_log
        if call["path"] == "/availability/seats/batch"
        for item in call["items"]
    ]


def route_discovery_calls(call_log: list) -> list[dict]:
    return [call for call in call_log if call["path"] == "/routes/trains"]


def schedule_calls(call_log: list) -> list[dict]:
    return [call for call in call_log if call["path"] == "/trains/{n}/schedule"]


def assert_every_segment_confirmed(body: dict, section: str = "results") -> None:
    for journey in body[section]:
        assert journey["all_segments_confirmed_available"] is True
        assert journey["journey_status"] == "AVAILABLE_COMBINATION"
        for segment in journey["segments"]:
            assert segment["status"] == CONFIRMED
            assert segment["segment_type"] == "TRAIN"


# =================================================================
# API-level happy path and response shape
# =================================================================


def test_api_happy_path_and_response_shape(make_test_client):
    handler = make_handler(
        route_trains_responder=tc_routes_responder(master_routes()),
        schedules=master_schedules(),
        availability_by_train=master_availability(),
    )
    test_client = make_test_client(handler)
    response = test_client.post(SEARCH_URL, json=journey_body_tc())
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["success"] is True
    assert body["total_results"] == 13
    assert {trains_of(j) for j in body["results"]} == EXPECTED_MASTER_SEQUENCES
    assert len({j["journey_id"] for j in body["results"]}) == 13
    assert body["unverified_candidates"] == []
    assert_every_segment_confirmed(body)

    # No journey ever changes to the train it just arrived on, and no
    # journey revisits a station.
    for journey in body["results"]:
        trains = trains_of(journey)
        assert all(trains[i] != trains[i + 1] for i in range(len(trains) - 1))
        assert len(set(station_path(journey))) == len(station_path(journey))

    # Direct through trains are reference-only: availability NOT checked.
    assert [d["train_number"] for d in body["direct_trains"]] == [TC]
    assert all(d["availability_checked"] is False for d in body["direct_trains"])

    completeness = body["search_completeness"]
    assert completeness["complete"] is True
    assert completeness["incomplete_reasons"] == []
    assert completeness["budget"]["requests_used"] > 0
    assert completeness["search_scope"]["min_transfer_buffer_minutes"] == 30
    assert completeness["search_scope"]["max_transfer_wait_minutes"] == 720
    assert body["api_usage"]["provider"] == "railcore"
    assert any("never books tickets" in note or "does not book tickets" in note for note in body["notes"])


def test_direct_train_availability_never_queried():
    call_log: list = []
    body, status = tc_run(master_routes(), call_log=call_log)
    assert status == 200
    items = batch_items(call_log)
    assert items, "expected availability queries"
    assert not any(
        item["train_number"] == TC and item["from"] == "MYS" and item["to"] == "DWR"
        for item in items
    ), "direct through-train segment must never be availability-checked"


# =================================================================
# Scenario 1: simple journey requiring one train change
# =================================================================


def test_scenario_1_one_train_change():
    body, status = tc_run(master_routes())
    assert status == 200
    journey = find_journey(body, (TA, TB))
    assert journey is not None
    assert journey["train_changes"] == 1
    assert journey["segment_count"] == 2
    assert journey["total_fare"] == 400.0
    assert journey["total_duration_minutes"] == 510  # 08:00 -> 16:30 door-to-door
    assert journey["total_transfer_wait_minutes"] == 120  # UBL 13:00 -> 15:00
    transfer = journey["transfers"][0]
    assert transfer["transfer_type"] == "TRAIN_CHANGE"
    assert transfer["station"]["code"] == "UBL"
    assert transfer["wait_minutes"] == 120
    assert transfer["within_min_buffer"] is True
    assert transfer["within_max_wait"] is True
    assert journey["segments"][0]["boarding_query_date"] == _d()
    assert journey["overnight"] is False


# =================================================================
# Scenario 2: two train changes
# =================================================================


def test_scenario_2_two_train_changes():
    body, status = tc_run(master_routes())
    assert status == 200
    journey = find_journey(body, (TA, TF, TH))
    assert journey is not None
    assert journey["train_changes"] == 2
    assert [t["station"]["code"] for t in journey["transfers"]] == ["HAS", "ASK"]
    # waits: HAS 10:00 -> 11:00 (60) + ASK 13:00 -> 14:30 (90); waits start at arrival
    assert journey["total_transfer_wait_minutes"] == 60 + 90
    assert journey["total_fare"] == 320.0
    assert journey["total_duration_minutes"] == 660  # 08:00 -> 19:00


# =================================================================
# Scenario 19: more than two train changes (three changes)
# =================================================================


def test_scenario_19_more_than_two_train_changes():
    body, status = tc_run(master_routes())
    assert status == 200
    journey = find_journey(body, (TA, TF, TI, TJ))
    assert journey is not None
    assert journey["train_changes"] == 3
    assert journey["segment_count"] == 4
    assert [t["station"]["code"] for t in journey["transfers"]] == ["HAS", "ASK", "UBL"]
    # waits: 60 (HAS) + 30 (ASK, exactly the minimum buffer) + 60 (UBL)
    assert journey["total_transfer_wait_minutes"] == 60 + 30 + 60
    assert journey["total_fare"] == 120 + 50 + 80 + 90
    assert journey["all_segments_confirmed_available"] is True


# =================================================================
# Scenario 3: several valid connections
# =================================================================


def test_scenario_3_several_valid_connections():
    body, status = tc_run(master_routes())
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert body["total_results"] == 13
    ids = [j["journey_id"] for j in body["results"]]
    assert len(set(ids)) == len(ids), "duplicate journeys must be deduplicated"
    interchanges = {
        transfer["station"]["code"]
        for journey in body["results"]
        for transfer in journey["transfers"]
    }
    assert {"UBL", "HAS", "ASK"} <= interchanges
    assert_every_segment_confirmed(body)


# =================================================================
# Scenario 4: minimum transfer buffer
# =================================================================


def test_scenario_4_min_transfer_buffer_rejects_then_overrides():
    # TK leaves ASK 13:20 after TF arrives 13:00: a 20-minute transfer,
    # below the default 30-minute buffer. The leg must never be generated,
    # so no availability query may be sent for it.
    call_log: list = []
    body, status = tc_run(master_routes(), call_log=call_log)
    assert status == 200
    assert not any(
        item["train_number"] == TK and item["from"] == "ASK" and item["date"] == _d()
        for item in batch_items(call_log)
    ), "sub-buffer connection must be rejected before any availability query"
    assert find_journey(body, (TA, TF, TK, TJ)) is None

    # Per-request override: a 15-minute buffer makes the same connection valid.
    body, status = tc_run(
        master_routes(),
        budgets=big_budgets(min_transfer_buffer_minutes=15),
        min_transfer_buffer_minutes=15,
    )
    assert status == 200
    journey = find_journey(body, (TA, TF, TK, TJ))
    assert journey is not None, "buffer override must accept the 20-minute transfer"
    assert journey["transfers"][1]["wait_minutes"] == 20
    assert journey["transfers"][1]["within_min_buffer"] is True
    scope = body["search_completeness"]["search_scope"]
    assert scope["min_transfer_buffer_minutes"] == 15


# =================================================================
# Scenario 5: maximum transfer wait
# =================================================================


def test_scenario_5_max_transfer_wait_rejects_then_overrides():
    # TL leaves HAS 23:00 after TA arrives 10:00: a 780-minute wait, above
    # the default 720-minute maximum. No availability query for it either.
    call_log: list = []
    body, status = tc_run(master_routes(), call_log=call_log)
    assert status == 200
    assert not any(
        item["train_number"] == TL for item in batch_items(call_log)
    ), "over-max-wait connection must be rejected before any availability query"
    assert find_journey(body, (TA, TL)) is None

    # Per-request override to 800 minutes makes the overnight transfer valid.
    body, status = tc_run(
        master_routes(),
        budgets=big_budgets(max_transfer_wait_minutes=800),
        max_transfer_wait_minutes=800,
    )
    assert status == 200
    journey = find_journey(body, (TA, TL))
    assert journey is not None
    assert journey["transfers"][0]["wait_minutes"] == 780
    assert journey["transfers"][0]["within_max_wait"] is True
    assert journey["overnight"] is True


# =================================================================
# Scenario 6: second train does not stop at the interchange station
# =================================================================


def test_scenario_6_second_train_not_stopping_at_interchange():
    # TM passes ASK without stopping (is_stop=false) but is (defensively)
    # listed by the provider for the ASK -> DWR pair. The engine must
    # reject boarding TM at ASK before any availability query, while
    # still using TM where it does stop (from MYS).
    routes = dict(master_routes())
    routes[("MYS", "DWR")] = tc_routes("MYS", "DWR", [
        tc_route_entry(TC, frm="MYS", to="DWR", dep="22:45", arr="06:43"),
        tc_route_entry(TM, frm="MYS", to="DWR", dep="07:00", arr="13:30"),
    ])
    routes[("ASK", "DWR")] = tc_routes("ASK", "DWR", [
        tc_route_entry(TD, frm="ASK", to="DWR", dep="05:00", arr="09:30"),
        tc_route_entry(TH, frm="ASK", to="DWR", dep="14:30", arr="19:00"),
        tc_route_entry(TM, frm="ASK", to="DWR", dep="09:57", arr="13:30"),
    ])
    schedules = master_schedules()
    schedules[TM] = tc_schedule(TM, [
        tc_stop(1, "MYS", "Mysuru Junction", None, "07:00"),
        tc_stop(2, "ASK", "Arsikere Junction", "09:55", "09:57", is_stop=False),
        tc_stop(3, "UBL", "Hubballi Junction", "12:00", "12:05"),
        tc_stop(4, "DWR", "Dharwad", "13:30", None),
    ])
    availability = master_availability({
        (TM, "MYS", "UBL"): tc_avail(fare=200),
        (TM, "UBL", "DWR"): tc_avail(fare=90),
    })

    call_log: list = []
    body, status = tc_run(routes, schedules, availability, call_log=call_log)
    assert status == 200

    assert not any(
        item["train_number"] == TM and item["from"] == "ASK"
        for item in batch_items(call_log)
    ), "a train that does not stop at the interchange must never be queried there"

    for journey in body["results"]:
        for segment in journey["segments"]:
            if segment["train_number"] == TM:
                assert segment["from_station"]["code"] != "ASK"

    # TM IS usable where it actually stops: a 1-change journey via UBL.
    assert find_journey(body, (TM, TB)) is not None
    # And the interchange journeys that do stop at ASK are unaffected.
    assert find_journey(body, (TC, TD)) is not None
    assert find_journey(body, (TA, TF, TH)) is not None


# =================================================================
# Scenario 7: overnight connection crossing midnight
# =================================================================


def test_scenario_7_overnight_connection_crossing_midnight():
    call_log: list = []
    body, status = tc_run(master_routes(), call_log=call_log)
    assert status == 200
    journey = find_journey(body, (TC, TD))
    assert journey is not None, "overnight MYS->ASK->DWR journey expected"
    assert journey["overnight"] is True
    assert journey["departure_datetime"].startswith(_d())
    assert journey["arrival_datetime"].startswith(_d(1))
    assert journey["segments"][0]["boarding_query_date"] == _d()
    # The second segment is boarded after midnight: IRCTC semantics mean it
    # is queried with the calendar date the train leaves ASK (D+1), while
    # still being the same journey.
    assert journey["segments"][1]["boarding_query_date"] == _d(1)
    assert journey["segments"][1]["departure_day"] == 1

    items = batch_items(call_log)
    assert any(
        item["train_number"] == TC and item["from"] == "MYS" and item["to"] == "ASK"
        and item["date"] == _d()
        for item in items
    )
    assert any(
        item["train_number"] == TD and item["from"] == "ASK" and item["to"] == "DWR"
        and item["date"] == _d(1)
        for item in items
    )

    # Two distinct (TC, TD) journeys exist: change at ASK and change at UBL.
    tc_td = [j for j in body["results"] if trains_of(j) == (TC, TD)]
    assert len(tc_td) == 2
    assert {j["transfers"][0]["station"]["code"] for j in tc_td} == {"ASK", "UBL"}


# =================================================================
# Scenario 8: confirmed availability shape
# =================================================================


def test_scenario_8_confirmed_availability_fields():
    body, status = tc_run(master_routes())
    assert status == 200
    assert_every_segment_confirmed(body)
    for journey in body["results"]:
        for segment in journey["segments"]:
            assert segment["provider_status"] == "AVAILABLE"
            assert segment["checked_at"]  # last availability check timestamp
            assert segment["travel_class"] == "SL"
            assert segment["quota"] == "GN"
            assert segment["fare"] is not None
            assert segment.get("booking_url") is None  # provider supplies none (dropped by exclude_none)
        assert journey["availability_checked_at"]
        assert journey["oldest_availability_update_at"]


# =================================================================
# Scenario 9: WAITLIST segment
# =================================================================


def test_scenario_9_waitlist_segment_rejected():
    availability = master_availability({
        (TB, "UBL", "DWR"): tc_avail(status="WAITLIST", fare=100),
    })
    body, status = tc_run(master_routes(), availability=availability)
    assert status == 200
    assert all(TB not in trains_of(j) for j in body["results"]), "waitlisted leg must disqualify"
    assert all(TB not in trains_of(j) for j in body["unverified_candidates"]), \
        "a waitlist is a definitive answer, not an unverified segment"
    # 13 master journeys - 3 through TB (TA-TB, TC-TB, TC-TD-TB) = 10
    assert body["total_results"] == 10
    ubl_report = next(r for r in body["stations"] if r["station_code"] == "UBL")
    assert ubl_report["legs_rejected"] >= 1


# =================================================================
# Scenario 10: RAC segment
# =================================================================


def test_scenario_10_rac_segment_rejected():
    availability = master_availability({
        (TE, "UBL", "DWR"): tc_avail(status="RAC", fare=95),
    })
    body, status = tc_run(master_routes(), availability=availability)
    assert status == 200
    assert all(TE not in trains_of(j) for j in body["results"])
    assert body["total_results"] == 11  # 13 - (TC-TE, TC-TD-TE)


# =================================================================
# Scenario 11: UNKNOWN provider status
# =================================================================


def test_scenario_11_unknown_segment_is_unverified_candidate():
    availability = master_availability({
        (TH, "ASK", "DWR"): tc_avail(status="UNKNOWN", fare=150),
    })
    body, status = tc_run(master_routes(), availability=availability)
    assert status == 200
    assert find_journey(body, (TA, TF, TH), section="results") is None
    assert body["total_results"] == 12
    candidate = find_journey(body, (TA, TF, TH), section="unverified_candidates")
    assert candidate is not None
    assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
    assert candidate["all_segments_confirmed_available"] is False
    assert candidate["unverified_segment_numbers"] == [3]
    assert candidate["segments"][2]["status"] == "UNKNOWN"
    # The search itself stayed complete and honest.
    assert body["search_completeness"]["complete"] is True


# =================================================================
# Scenario 12: provider error during availability verification
# =================================================================


def test_scenario_12_provider_error_never_becomes_unavailable():
    def failing_batch(body: dict) -> httpx.Response:
        return httpx.Response(
            503,
            json=error_envelope(
                "UPSTREAM_UNAVAILABLE", "upstream down", "SERVER", retryable=True
            ),
            headers={"retry-after": "0"},
        )

    body, status = tc_run(master_routes(), batch_responder=failing_batch)
    assert status == 200
    assert body["status"] == "PARTIALLY_VERIFIED"
    assert body["success"] is False
    assert body["total_results"] == 0
    assert body["total_unverified_candidates"] >= 1
    assert "PROVIDER_ERROR" in body["search_completeness"]["incomplete_reasons"]
    assert body["errors"], "provider errors must be surfaced, never hidden"
    for candidate in body["unverified_candidates"]:
        assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
        assert candidate["all_segments_confirmed_available"] is False
        statuses = {segment["status"] for segment in candidate["segments"]}
        assert "CONFIRMED_AVAILABLE" not in statuses
        assert "PROVIDER_ERROR" in statuses
    # A provider error is never a NO_COMPLETE_JOURNEY claim of full search.
    assert body["status"] != "NO_COMPLETE_JOURNEY"


# =================================================================
# Scenario 13: duplicate candidate trains
# =================================================================


def test_scenario_13_duplicate_candidate_trains():
    routes = dict(master_routes())
    routes[("MYS", "DWR")] = tc_routes("MYS", "DWR", [
        tc_route_entry(TC, frm="MYS", to="DWR", dep="22:45", arr="06:43"),
        tc_route_entry(TC, frm="MYS", to="DWR", dep="22:45", arr="06:43"),  # duplicate
    ])
    call_log: list = []
    body, status = tc_run(routes, call_log=call_log)
    assert status == 200
    tc_schedule_calls = [c for c in schedule_calls(call_log) if c["train_number"] == TC]
    assert len(tc_schedule_calls) == 1, "duplicate train must be schedule-fetched once"
    assert len(body["direct_trains"]) == 1, "duplicate direct-train reference deduplicated"
    ids = [j["journey_id"] for j in body["results"]]
    assert len(ids) == len(set(ids))
    # The duplicate changes nothing: all 13 master journeys appear exactly once.
    assert {trains_of(j) for j in body["results"]} == EXPECTED_MASTER_SEQUENCES
    assert body["total_results"] == 13


# =================================================================
# Scenario 14: cycle / repeated station path
# =================================================================


def test_scenario_14_cycle_and_repeated_station_paths():
    # TN is a circular service (revisits UBL, then continues to MYS). A
    # journey that rides it past its first stops would have to revisit a
    # station; the engine must block that before querying availability.
    routes = dict(master_routes())
    routes[("UBL", "DWR")] = tc_routes("UBL", "DWR", [
        tc_route_entry(TB, frm="UBL", to="DWR", dep="15:00", arr="16:30"),
        tc_route_entry(TE, frm="UBL", to="DWR", dep="09:00", arr="10:00"),
        tc_route_entry(TJ, frm="UBL", to="DWR", dep="17:00", arr="18:30"),
        tc_route_entry(TD, frm="UBL", to="DWR", dep="08:00", arr="09:30"),
        tc_route_entry(TN, frm="UBL", to="DWR", dep="19:05", arr="19:30"),
    ])
    schedules = master_schedules()
    schedules[TN] = tc_schedule(TN, [
        tc_stop(1, "UBL", "Hubballi Junction", None, "19:00"),
        tc_stop(2, "DWR", "Dharwad", "19:30", "19:35"),
        tc_stop(3, "BGM", "Belagavi", "21:00", "21:05"),
        tc_stop(4, "UBL", "Hubballi Junction", "23:00", "23:05"),  # second visit
        tc_stop(5, "MYS", "Mysuru Junction", "02:00", None, day=2),
    ])
    availability = master_availability({
        (TN, "UBL", "DWR"): tc_avail(fare=110),
        (TN, "UBL", "BGM"): tc_avail(fare=140),
    })

    call_log: list = []
    body, status = tc_run(routes, schedules, availability, call_log=call_log)
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"

    for journey in body["results"]:
        path = station_path(journey)
        assert len(set(path)) == len(path), f"station revisited in {path}"
        trains = trains_of(journey)
        assert trains.count(TN) <= 1
        assert "MYS" not in path[1:], "journey must never loop back to the source"
        for segment in journey["segments"]:
            if segment["train_number"] == TN:
                assert segment["from_station"]["code"] in ("UBL",)
                assert segment["to_station"]["code"] in ("DWR", "BGM")

    # The legitimate TN journeys (board at its first UBL visit, alight DWR)
    # are still found, and the search terminated within its state budget.
    assert find_journey(body, (TA, TN)) is not None
    # 13 master + TA-TN, TA-TF-TI-TN, TC-TD-TN, TC-TK-TN
    assert len(body["results"]) == 17
    assert "STATE_LIMIT_REACHED" not in body["search_completeness"]["incomplete_reasons"]
    assert body["search_completeness"]["complete"] is True

    # Cycle prevention means exactly this: no journey - confirmed result OR
    # unverified candidate - ever revisits a station, even along exotic
    # overshoot paths such as MYS -> BGM -> UBL (TC -> TN -> TC) that pass
    # through TN's second lap without repeating any station.
    for section in ("results", "unverified_candidates"):
        for journey in body[section]:
            path = station_path(journey)
            assert len(set(path)) == len(path), f"station revisited in {path}"
            for segment in journey["segments"]:
                if segment["train_number"] == TN:
                    # TN is only ever boarded where it actually stops, and
                    # no journey rides it around its full circular lap.
                    assert segment["from_station"]["code"] in ("UBL", "BGM")
                    assert segment["to_station"]["code"] in ("DWR", "BGM", "UBL")
                    assert segment["to_station"]["code"] != segment["from_station"]["code"]
            trains = trains_of(journey)
            assert all(not (trains[i] == trains[i + 1]) for i in range(len(trains) - 1))


# =================================================================
# Scenario 15: query-budget exhaustion (enforced BEFORE API requests)
# =================================================================


def test_scenario_15_request_budget_exhausted_before_any_availability_call():
    call_log: list = []
    budgets = big_budgets(max_api_requests=3)
    body, status = tc_run(master_routes(), budgets=budgets, call_log=call_log)
    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert body["total_results"] == 0
    reasons = body["search_completeness"]["incomplete_reasons"]
    assert "BUDGET_REQUESTS_EXHAUSTED" in reasons
    # Budget enforcement BEFORE requests: exactly 3 provider calls happened
    # (seed discovery + 1 schedule + 1 seed-pair discovery), none over budget.
    assert len(call_log) == 3
    assert len(batch_items(call_log)) == 0, "no availability call may exceed the request budget"
    assert body["search_completeness"]["budget"]["requests_used"] == 3
    assert body["search_completeness"]["budget"]["requests_used"] <= budgets.max_api_requests
    assert "NOT all possibilities were checked" in body["message"]


def test_scenario_15_credit_budget_exhausted_before_requests():
    call_log: list = []
    budgets = big_budgets(max_credits=2)
    body, status = tc_run(master_routes(), budgets=budgets, call_log=call_log)
    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert "BUDGET_CREDITS_EXHAUSTED" in body["search_completeness"]["incomplete_reasons"]
    # 1 discovery credit + 1 schedule credit = the whole budget: nothing else
    # was sent, and never a seat-availability call.
    assert len(call_log) == 2
    assert len(batch_items(call_log)) == 0
    assert body["search_completeness"]["budget"]["credits_used_estimate"] <= budgets.max_credits


# =================================================================
# Scenario 16: search-timeout and search-horizon exhaustion
# =================================================================


def test_scenario_16_time_limit_stops_search():
    seconds = 0.25
    base_routes = tc_routes_responder(master_routes())
    schedules = master_schedules()

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
        availability_by_train=master_availability(),
    )
    client = make_client(handler)
    service = TrainChangeConnectionService(client, big_budgets(time_budget_seconds=0.8))
    request = TrainChangeSearchRequest(**journey_body_tc())
    body, status = service.run(request)
    assert status == 200
    assert body["status"] == "SEARCH_LIMIT_REACHED"
    assert "TIME_LIMIT_REACHED" in body["search_completeness"]["incomplete_reasons"]
    used = body["search_completeness"]["budget"]["requests_used"]
    assert 3 <= used <= 8, "the time budget must stop the search after a few calls"


def test_scenario_16_horizon_exhaustion():
    call_log: list = []
    body, status = tc_run(
        master_routes(),
        budgets=big_budgets(search_horizon_days=0),
        call_log=call_log,
    )
    assert status == 200
    # Same-day arrivals only: all four TA journeys survive, everything
    # arriving after midnight is honestly out of scope.
    assert {trains_of(j) for j in body["results"]} == {
        (TA, TB), (TA, TJ), (TA, TF, TH), (TA, TF, TI, TJ),
    }
    for journey in body["results"]:
        assert journey["arrival_datetime"].startswith(_d())
        assert journey["overnight"] is False
    reasons = body["search_completeness"]["incomplete_reasons"]
    assert "HORIZON_LIMIT_REACHED" in reasons
    scope = body["search_completeness"]["search_scope"]
    assert scope["search_horizon_days"] == 0
    assert scope["horizon_end_date"] == _d()
    # The overnight first legs (TC arriving after midnight) were rejected
    # during leg generation - before ANY availability query for them.
    assert not any(
        item["train_number"] == TC and item["from"] == "MYS" and item["date"] == _d()
        for item in batch_items(call_log)
    ), "beyond-horizon leg must be rejected before any availability query"
    assert all(TC not in trains_of(j) for j in body["results"])


# =================================================================
# Scenario 17: cheapest / fastest / fewest-changes ranking
# =================================================================


def test_scenario_17_ranking_preferences():
    cheapest, _ = tc_run(master_routes(), ranking_preference="CHEAPEST")
    fastest, _ = tc_run(master_routes(), ranking_preference="FASTEST")
    fewest, _ = tc_run(master_routes(), ranking_preference="FEWEST_CHANGES")

    assert trains_of(cheapest["results"][0]) == (TA, TF, TH)  # 320 INR
    assert cheapest["results"][0]["total_fare"] == 320.0
    assert trains_of(fastest["results"][0]) == (TA, TB)  # 510 minutes
    assert fastest["results"][0]["total_duration_minutes"] == 510
    assert trains_of(fewest["results"][0]) == (TA, TB)  # 1 change, 510 minutes

    fares = [j["total_fare"] for j in cheapest["results"]]
    assert fares == sorted(fares), "CHEAPEST must order by total fare"
    durations = [j["total_duration_minutes"] for j in fastest["results"]]
    assert durations == sorted(durations), "FASTEST must order by duration"
    changes = [j["train_changes"] for j in fewest["results"]]
    assert changes == sorted(changes), "FEWEST_CHANGES must order by train changes"


# =================================================================
# Scenario 18: missing fare data
# =================================================================


def test_scenario_18_missing_fare_never_estimated():
    availability = master_availability({
        (TF, "HAS", "ASK"): tc_avail(no_fare=True),
    })
    body, status = tc_run(master_routes(), availability=availability, ranking_preference="CHEAPEST")
    assert status == 200
    missing = [j for j in body["results"] if not j["fares_complete"]]
    assert len(missing) == 2  # (TA,TF,TH) and (TA,TF,TI,TJ)
    for journey in missing:
        # exclude_none=True drops null fares from the JSON: the fare must be
        # absent, never estimated and never zero-filled.
        assert journey.get("total_fare") is None, "unknown fare must never be estimated"
        assert journey["segments_missing_fare"] == [2]
        assert trains_of(journey)[1] == TF
    # With CHEAPEST ranking, unknown-fare journeys sort after priced ones.
    assert {trains_of(j) for j in body["results"][-2:]} == {(TA, TF, TH), (TA, TF, TI, TJ)}
    assert all(j["total_fare"] is not None for j in body["results"][:-2])


# =================================================================
# Scenario 20: unverified segment never recommended as available
# =================================================================


def test_scenario_20_unverified_segment_not_recommended():
    # The TF item fails in place inside the batch response (e.g.
    # BOOKING_NOT_OPEN): no availability information whatsoever.
    body, status = tc_run(master_routes(), batch_item_failures={(TF, "HAS", "ASK")})
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"  # other journeys confirmed

    for trains in ((TA, TF, TH), (TA, TF, TI, TJ)):
        assert find_journey(body, trains, section="results") is None, \
            "a journey with an unverified segment must never be a confirmed result"
        candidate = find_journey(body, trains, section="unverified_candidates")
        assert candidate is not None
        assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
        assert candidate["all_segments_confirmed_available"] is False
        assert candidate["unverified_segment_numbers"] == [2]
        assert candidate["segments"][1]["status"] == "UNKNOWN"
        assert candidate["segments"][1]["error"], "the item failure must be surfaced"
        # Confirmed segments keep their explicit confirmation.
        assert candidate["segments"][0]["status"] == CONFIRMED

    assert body["total_results"] == 11  # 13 master journeys - the 2 unverified


# =================================================================
# Honest bootstrap: no through trains
# =================================================================


def test_no_through_trains_honest_negative():
    call_log: list = []
    body, status = tc_run({}, call_log=call_log)
    assert status == 200
    assert body["status"] == "NO_CANDIDATE_TRAINS"
    assert body["success"] is False
    assert body["total_results"] == 0
    assert len(route_discovery_calls(call_log)) == 1
    assert len(batch_items(call_log)) == 0
    assert "via_stations" in body["message"]


def test_via_stations_bootstrap_without_through_train():
    routes = {
        key: payload for key, payload in master_routes().items()
        if key not in {("MYS", "DWR"), ("MYS", "ASK")}
    }
    # A single hint anchors UBL only: without any through train, no schedule
    # ever mentions ASK, so the (HAS -> ASK) shuttle is out of reach and the
    # engine honestly returns only the UBL-anchored journeys.
    body, status = tc_run(routes, via_stations=["UBL"])
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert {trains_of(j) for j in body["results"]} == {(TA, TB), (TA, TJ)}
    assert body["direct_trains"] == []

    # Hinting the second interchange (ASK) lets the engine discover the
    # HAS -> ASK shuttle and the deeper chains: one hint per interchange
    # that no through-train schedule reveals.
    body, status = tc_run(routes, via_stations=["UBL", "ASK"])
    assert status == 200
    assert body["status"] == "AVAILABLE_COMBINATION"
    assert {trains_of(j) for j in body["results"]} == {
        (TA, TB), (TA, TJ), (TA, TF, TH), (TA, TF, TI, TJ),
    }
    assert body["search"]["via_stations"] == ["UBL", "ASK"]
    # No through train was ever claimed: direct_trains stays empty.
    assert body["direct_trains"] == []


# =================================================================
# Candidate train hygiene: class filter and schedule fetch failure
# =================================================================


def test_class_not_offered_train_skipped_before_schedule_fetch():
    no_sl = "99815"
    routes = dict(master_routes())
    routes[("MYS", "UBL")] = tc_routes("MYS", "UBL", [
        tc_route_entry(TA, frm="MYS", to="UBL", dep="08:00", arr="13:00"),
        tc_route_entry(no_sl, frm="MYS", to="UBL", dep="10:00", arr="12:00", classes=("2A",)),
    ])
    call_log: list = []
    body, status = tc_run(routes, call_log=call_log)
    assert status == 200
    skip = next(s for s in body["skipped_trains"] if s["train_number"] == no_sl)
    assert skip["skip_reason"] == "CLASS_NOT_OFFERED"
    assert not any(c.get("train_number") == no_sl for c in schedule_calls(call_log)), \
        "a train without the requested class must not cost a schedule fetch"
    assert {trains_of(j) for j in body["results"]} == EXPECTED_MASTER_SEQUENCES


def test_schedule_fetch_failure_is_unverified_not_unavailable():
    routes = dict(master_routes())
    routes[("MYS", "DWR")] = tc_routes("MYS", "DWR", [
        tc_route_entry(TC, frm="MYS", to="DWR", dep="22:45", arr="06:43"),
        tc_route_entry(TM, frm="MYS", to="DWR", dep="07:00", arr="13:30"),
    ])
    schedules = master_schedules()
    schedules[TM] = tc_schedule(TM, [
        tc_stop(1, "MYS", "Mysuru Junction", None, "07:00"),
        tc_stop(2, "ASK", "Arsikere Junction", "09:55", "09:57", is_stop=False),
        tc_stop(3, "UBL", "Hubballi Junction", "12:00", "12:05"),
        tc_stop(4, "DWR", "Dharwad", "13:30", None),
    ])
    availability = master_availability({
        (TM, "MYS", "UBL"): tc_avail(fare=200),
        (TM, "UBL", "DWR"): tc_avail(fare=90),
    })
    call_log: list = []
    # TM's schedule is missing from the handler: its schedule fetch 404s.
    handler = make_handler(
        route_trains_responder=tc_routes_responder(routes),
        schedules={k: v for k, v in schedules.items() if k != TM},
        availability_by_train=availability,
        call_log=call_log,
    )
    client = make_client(handler)
    service = TrainChangeConnectionService(client, big_budgets())
    body, status = service.run(TrainChangeSearchRequest(**journey_body_tc()))
    assert status == 200
    skip = next(s for s in body["skipped_trains"] if s["train_number"] == TM)
    assert skip["skip_reason"] == "SCHEDULE_FETCH_FAILED"
    assert "unverified" in skip["skip_detail"]
    assert "PROVIDER_ERROR" in body["search_completeness"]["incomplete_reasons"]
    # TC-based journeys still complete.
    assert find_journey(body, (TC, TD)) is not None
    assert body["status"] == "AVAILABLE_COMBINATION"


# =================================================================
# Deduplication, caching and budget settings wiring
# =================================================================


def test_availability_queries_deduplicated_across_expansions():
    call_log: list = []
    body, status = tc_run(master_routes(), call_log=call_log)
    assert status == 200
    items = batch_items(call_log)
    tj_d1 = [
        item for item in items
        if item["train_number"] == TJ and item["date"] == _d()
    ]
    assert len(tj_d1) == 1, "identical (train, from, to, date) queries must deduplicate"
    seen = {
        (item["train_number"], item["from"], item["to"], item["date"])
        for item in items
    }
    assert len(seen) == len(items), \
        "the same segment may never be queried twice within one search"
    assert len(items) < 20, "memoized legs must not be re-sent"


def test_cross_search_schedule_and_discovery_cache_reuse():
    routes = master_routes()
    handler = make_handler(
        route_trains_responder=tc_routes_responder(routes),
        schedules=master_schedules(),
        availability_by_train=master_availability(),
    )
    client = make_client(handler)
    request = TrainChangeSearchRequest(**journey_body_tc())

    first, _ = TrainChangeConnectionService(client, big_budgets()).run(request)
    second, _ = TrainChangeConnectionService(client, big_budgets()).run(request)

    assert second["search_completeness"]["schedule_cache_hits"] > 0
    assert second["search_completeness"]["discovery_cache_hits"] > 0
    assert {trains_of(j) for j in first["results"]} == {trains_of(j) for j in second["results"]}
    # Availability is never cached across searches (freshness).
    assert second["search_completeness"]["distinct_availability_queries"] > 0


def test_train_change_budgets_from_settings(monkeypatch):
    monkeypatch.setenv("TRAIN_CHANGE_MIN_TRANSFER_BUFFER_MINUTES", "17")
    monkeypatch.setenv("TRAIN_CHANGE_MAX_TRANSFER_WAIT_MINUTES", "300")
    monkeypatch.setenv("TRAIN_CHANGE_SEARCH_HORIZON_DAYS", "1")
    monkeypatch.setenv("TRAIN_CHANGE_MAX_DISCOVERY_PAIRS", "9")
    monkeypatch.setenv("TRAIN_CHANGE_MAX_TRAINS", "4")
    monkeypatch.setenv("TRAIN_CHANGE_MAX_API_REQUESTS", "11")
    monkeypatch.setenv("TRAIN_CHANGE_MAX_CREDITS", "12")
    monkeypatch.setenv("TRAIN_CHANGE_TIME_BUDGET_SECONDS", "3.5")
    monkeypatch.setenv("TRAIN_CHANGE_MAX_EXPANDED_STATES", "40")
    monkeypatch.setenv("TRAIN_CHANGE_MAX_JOURNEYS", "5")
    monkeypatch.setenv("TRAIN_CHANGE_MAX_RESULTS", "6")
    budgets = TrainChangeBudgets.from_settings(Settings())
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


def test_budget_clamps_and_override_validation():
    clamped = TrainChangeBudgets(min_transfer_buffer_minutes=-5, max_transfer_wait_minutes=10, search_horizon_days=99)
    assert clamped.min_transfer_buffer_minutes == 0
    assert clamped.max_transfer_wait_minutes == 10
    assert clamped.search_horizon_days == 7
    with pytest.raises(ValueError):
        TrainChangeBudgets(min_transfer_buffer_minutes=30).with_overrides(None, 20)


# =================================================================
# API validation and documentation
# =================================================================


def test_request_validation_errors(make_test_client):
    test_client = make_test_client(make_handler())
    cases = [
        {"from_station": "MYS!"},
        {"from_station": "MYS", "to_station": "MYS"},
        {"journey_date": (datetime.now(tz=IST).date() - timedelta(days=1)).isoformat()},
        {"travel_class": "9Z"},
        {"quota": "XX"},
        {"via_stations": ["MYS"]},
        {"via_stations": ["UBL", "UBL"]},
        {"min_transfer_buffer_minutes": 30, "max_transfer_wait_minutes": 10},
        {"ranking_preference": "SHORTEST"},
    ]
    for case in cases:
        response = test_client.post(SEARCH_URL, json=journey_body_tc(**case))
        assert response.status_code == 422, f"expected 422 for {case}"


def test_via_stations_max_three(make_test_client):
    test_client = make_test_client(make_handler())
    response = test_client.post(
        SEARCH_URL, json=journey_body_tc(via_stations=["AAA", "BBB", "CCC", "DDD"])
    )
    assert response.status_code == 422


def test_openapi_documents_train_change_endpoint(make_test_client):
    test_client = make_test_client(make_handler())
    schema = test_client.get("/openapi.json").json()
    assert SEARCH_URL in schema["paths"]
    operation = schema["paths"][SEARCH_URL]["post"]
    assert "TrainChangeSearchResponse" in str(operation)
    assert "never books tickets" in operation["description"]
    assert "TrainChangeSearchRequest" in schema["components"]["schemas"]
    assert "TrainChangeSearchResponse" in schema["components"]["schemas"]


# =================================================================
# Hard failure paths: configuration and discovery failures
# =================================================================


def test_missing_api_key_is_config_error_503():
    handler = make_handler(
        route_trains_responder=tc_routes_responder(master_routes()),
        schedules=master_schedules(),
        availability_by_train=master_availability(),
    )
    client = make_client(handler, api_key="")
    service = TrainChangeConnectionService(client, big_budgets())
    body, status = service.run(TrainChangeSearchRequest(**journey_body_tc()))
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

    handler = make_handler(
        route_trains_responder=failing_routes,
        schedules=master_schedules(),
        availability_by_train=master_availability(),
    )
    client = make_client(handler)
    service = TrainChangeConnectionService(client, big_budgets())
    body, status = service.run(TrainChangeSearchRequest(**journey_body_tc()))
    assert status == 502
    assert body["status"] == "PROVIDER_ERROR"
    assert body["total_results"] == 0
    assert body["errors"], "the discovery failure must be surfaced"
    assert body["search_completeness"]["incomplete_reasons"] == ["PROVIDER_ERROR"]
