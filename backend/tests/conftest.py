"""Shared fixtures and mock RailCore payloads for unit tests.

All railway data here is SYNTHETIC (train 99999 does not exist). Mocked
data is used only in automated tests; the manual feasibility test runs
against the real RailCore API. The mock payloads mirror the documented
RailCore response shapes (envelope: success/data/error/meta).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_railcore_client
from app.main import app as fastapi_app
from app.providers.railcore import RailCoreClient

MOCK_TRAIN = "99999"  # deliberately synthetic; clearly not a real train
MOCK_BASE_URL = "https://railcore.test/v1"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# A synthetic Mysuru -> Belagavi overnight route (same station codes as the
# real 17301 route, so tests exercise the actual product scenario, including
# the day-1/day-2 overnight rollover).
MOCK_STOPS = [
    {"sequence": 1, "station_code": "MYS", "station_name": "Mysuru Junction", "stop_type": "ORIGIN", "arrival_time": None, "departure_time": "22:45", "halt_minutes": 0, "day": 1, "distance_km": 0, "is_stop": True},
    {"sequence": 2, "station_code": "KRNR", "station_name": "Krishnarajanagar", "stop_type": "HALT", "arrival_time": "23:15", "departure_time": "23:16", "halt_minutes": 1, "day": 1, "distance_km": 34, "is_stop": True},
    {"sequence": 3, "station_code": "HAS", "station_name": "Hassan", "stop_type": "HALT", "arrival_time": "00:35", "departure_time": "00:37", "halt_minutes": 2, "day": 2, "distance_km": 121, "is_stop": True},
    {"sequence": 4, "station_code": "ASK", "station_name": "Arsikere Junction", "stop_type": "HALT", "arrival_time": "01:25", "departure_time": "01:27", "halt_minutes": 2, "day": 2, "distance_km": 163, "is_stop": True},
    {"sequence": 5, "station_code": "UBL", "station_name": "SSS Hubballi Junction", "stop_type": "HALT", "arrival_time": "06:10", "departure_time": "06:20", "halt_minutes": 10, "day": 2, "distance_km": 408, "is_stop": True},
    {"sequence": 6, "station_code": "DWR", "station_name": "Dharwad", "stop_type": "HALT", "arrival_time": "06:43", "departure_time": "06:45", "halt_minutes": 2, "day": 2, "distance_km": 436, "is_stop": True},
    {"sequence": 7, "station_code": "BGM", "station_name": "Belagavi", "stop_type": "DESTINATION", "arrival_time": "09:55", "departure_time": None, "halt_minutes": 0, "day": 2, "distance_km": 491, "is_stop": True},
]


def tomorrow_ist() -> str:
    return (datetime.now(tz=IST) + timedelta(days=1)).date().isoformat()


def usage_headers(request_id: str) -> dict[str, str]:
    return {
        "x-railcore-request-id": request_id,
        "x-railcore-credits-cost": "1",
        "x-railcore-credits-remaining": "498",
        "x-railcore-ratelimit-remaining": "9",
        "x-railcore-ratelimit-day-remaining": "97",
    }


def error_envelope(code: str, message: str, category: str, retryable: bool = False) -> dict:
    return {
        "success": False,
        "error": {"code": code, "message": message, "category": category, "retryable": retryable},
        "meta": {"api_version": "v1", "request_id": "req_test_error", "timestamp": "2026-11-23T12:00:00+05:30"},
    }


def mock_schedule_payload(train_number: str = MOCK_TRAIN) -> dict:
    return {
        "success": True,
        "data": {
            "train_number": train_number,
            "train_name": "Mock Overnight Express",
            "source_station_code": "MYS",
            "destination_station_code": "BGM",
            "running_days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
            "classes": ["SL", "3A", "2A"],
            "total_duration_minutes": 670,
            "distance_km": 491,
            "stops": MOCK_STOPS,
        },
        "meta": {
            "api_version": "v1",
            "request_id": "req_test_sched",
            "trace_id": "trc_test_sched",
            "timestamp": "2026-11-23T12:00:00+05:30",
            "freshness": {"mode": "live", "retrieved_at": "2026-11-23T06:30:00Z"},
        },
    }


def avail_entry(
    *,
    status: str = "AVAILABLE",
    class_code: str = "SL",
    text: str | None = None,
    available_count: int | None = None,
    waitlist_count: int | None = None,
    rac_count: int | None = None,
    fare: float | None = None,
    total_fare: float | None = None,
) -> dict:
    return {
        "class_code": class_code,
        "status": status,
        "availability_text": text or status,
        "available_count": available_count,
        "waitlist_count": waitlist_count,
        "rac_count": rac_count,
        "fare": fare,
        "total_fare": total_fare if total_fare is not None else fare,
        "confirmation_probability": {"percentage": 100 if status == "AVAILABLE" else 20, "label": "HIGH" if status == "AVAILABLE" else "LOW", "text": "Available" if status == "AVAILABLE" else "Uncertain"},
        "last_updated_at": "2026-11-23T08:15:46.547+05:30",
    }


def availability_payload(
    from_station: str,
    to_station: str,
    classes: list[dict] | None = None,
    *,
    train_number: str = MOCK_TRAIN,
    journey_date: str | None = None,
    quota: str = "GN",
    drop_classes_key: bool = False,
) -> dict:
    data: dict = {
        "train_number": train_number,
        "from_station_code": from_station,
        "to_station_code": to_station,
        "journey_date": journey_date or tomorrow_ist(),
        "quota": quota,
    }
    if not drop_classes_key:
        data["classes"] = classes if classes is not None else [avail_entry()]
    return {
        "success": True,
        "data": data,
        "meta": {"api_version": "v1", "request_id": f"req_test_avail_{from_station}_{to_station}", "timestamp": "2026-11-23T12:00:00+05:30"},
    }


# Default per-segment availability: both segments explicitly AVAILABLE.
DEFAULT_AVAILABILITY: dict[tuple[str, str], dict] = {
    ("MYS", "ASK"): avail_entry(status="AVAILABLE", text="AVAILABLE-120", available_count=120, fare=255),
    ("ASK", "DWR"): avail_entry(status="AVAILABLE", text="AVAILABLE-83", available_count=83, fare=185),
    ("UBL", "DWR"): avail_entry(status="AVAILABLE", text="AVAILABLE-12", available_count=12, fare=120),
}


def make_handler(
    *,
    schedule_responder=None,
    availability_responses: dict[tuple[str, str], object] | None = None,
    default_availability: dict[tuple[str, str], dict] | None = None,
    route_trains: dict | None = None,
    route_trains_responder=None,
    schedules: dict[str, dict] | None = None,
    availability_by_train: dict[tuple[str, str, str], dict] | None = None,
    batch_item_failures: set[tuple[str, str, str]] | None = None,
    batch_responder=None,
    call_log: list | None = None,
):
    """Build a MockTransport handler emulating RailCore.

    - schedule_responder: callable(httpx.Request) -> httpx.Response
    - availability_responses: {(from, to): responder(query_dict)} where a
      responder is either a dict (classes entry) or a callable returning
      an httpx.Response (for error/timeout/malformed scenarios).
    - route_trains: full payload for GET /routes/trains (phase 2 engine).
      If absent, the endpoint answers 404 NO_TRAINS_FOUND.
    - route_trains_responder: callable(query_dict) -> httpx.Response.
    - schedules: {train_number: schedule payload} for the schedule
      endpoint (falls back to the default MOCK_TRAIN schedule).
    - availability_by_train: {(train, from, to): classes entry} - checked
      before the pair-keyed maps (phase 2 engine, multi-train support).
    - batch_item_failures: {(train, from, to)} items that fail in place
      inside a 200 batch response (e.g. BOOKING_NOT_OPEN).
    - batch_responder: callable(body_dict) -> httpx.Response replacing the
      whole batch endpoint (provider-failure scenarios, phase 3).
    - call_log: optional list that receives every request for dedup and
      date-semantics assertions.
    """

    def log_call(entry: dict) -> None:
        if call_log is not None:
            call_log.append(entry)

    def default_schedule(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_schedule_payload(), headers=usage_headers("req_test_sched"))

    def schedule_for(request: httpx.Request) -> httpx.Response:
        # /trains/{train_number}/schedule
        train_number = request.url.path.split("/")[-2]
        if schedules and train_number in schedules:
            return httpx.Response(
                200, json=schedules[train_number], headers=usage_headers(f"req_test_sched_{train_number}")
            )
        if train_number == MOCK_TRAIN:
            return default_schedule(request)
        return httpx.Response(
            404,
            json=error_envelope("TRAIN_NOT_FOUND", f"no train {train_number}", "CLIENT"),
        )

    def default_availability_response(query: dict) -> httpx.Response:
        mapping = default_availability or DEFAULT_AVAILABILITY
        pair = (query.get("from", ""), query.get("to", ""))
        entry = mapping.get(pair, avail_entry(status="UNKNOWN"))
        payload = availability_payload(pair[0], pair[1], [entry])
        return httpx.Response(200, json=payload, headers=usage_headers(f"req_test_avail_{pair[0]}_{pair[1]}"))

    def entry_for_item(train_number: str, frm: str, to: str) -> dict:
        if availability_by_train is not None:
            entry = availability_by_train.get((train_number, frm, to))
            if entry is not None:
                return entry
        mapping = default_availability or DEFAULT_AVAILABILITY
        return mapping.get((frm, to), avail_entry(status="UNKNOWN"))

    def handler(request: httpx.Request) -> httpx.Response:
        # Safety net: the API key must never leak into URLs.
        assert "rk_live" not in str(request.url), "API key leaked into request URL"
        assert request.headers.get("x-railcore-key") == "test-key", "missing X-RailCore-Key header"

        if request.url.path.endswith("/availability/seats/batch"):
            body = json.loads(request.read().decode("utf-8"))
            log_call({"path": "/availability/seats/batch", "items": body.get("items", [])})
            if batch_responder is not None:
                return batch_responder(body)
            results = []
            for item in body.get("items", []):
                pair = (item.get("from", ""), item.get("to", ""))
                train_number = str(item.get("train_number", ""))
                if pair == ("FAIL", "ITEM") or (
                    batch_item_failures is not None
                    and (train_number, pair[0], pair[1]) in batch_item_failures
                ):
                    results.append(
                        {
                            "error": "BOOKING_NOT_OPEN",
                            "message": "date is past the booking window",
                            "train_number": item.get("train_number"),
                            "from_station_code": pair[0],
                            "to_station_code": pair[1],
                            "journey_date": item.get("date"),
                        }
                    )
                    continue
                entry = entry_for_item(train_number, pair[0], pair[1])
                results.append(
                    {
                        "train_number": item.get("train_number"),
                        "from_station_code": pair[0],
                        "to_station_code": pair[1],
                        "journey_date": item.get("date"),
                        "quota": item.get("quota", "GN"),
                        "classes": [entry],
                    }
                )
            return httpx.Response(
                200,
                json={"success": True, "data": {"results": results}, "meta": {"api_version": "v1", "request_id": "req_test_batch"}},
                headers=usage_headers("req_test_batch"),
            )

        if request.url.path.endswith("/routes/trains"):
            query = dict(request.url.params)
            log_call({"path": "/routes/trains", "query": query})
            if route_trains_responder is not None:
                return route_trains_responder(query)
            if route_trains is not None:
                return httpx.Response(
                    200, json=route_trains, headers=usage_headers("req_test_routes")
                )
            return httpx.Response(
                404,
                json=error_envelope(
                    "NO_TRAINS_FOUND", "no trains between the requested stations", "CLIENT"
                ),
            )

        if request.url.path.endswith("/schedule"):
            log_call({"path": "/trains/{n}/schedule", "train_number": request.url.path.split("/")[-2]})
            responder = schedule_responder or schedule_for
            return responder(request)
        if request.url.path.endswith("/availability/seats"):
            query = dict(request.url.params)
            log_call({"path": "/availability/seats", "query": query})
            pair = (query.get("from", ""), query.get("to", ""))
            train_number = str(query.get("train_number", ""))
            if availability_by_train is not None:
                train_entry = availability_by_train.get((train_number, pair[0], pair[1]))
                if train_entry is not None:
                    return httpx.Response(
                        200,
                        json=availability_payload(pair[0], pair[1], [train_entry]),
                        headers=usage_headers(f"req_test_avail_{pair[0]}_{pair[1]}"),
                    )
            responder = (availability_responses or {}).get(pair)
            if responder is None:
                return default_availability_response(query)
            if callable(responder):
                return responder(query)
            if isinstance(responder, list):
                return httpx.Response(
                    200,
                    json=availability_payload(pair[0], pair[1], responder),
                    headers=usage_headers(f"req_test_avail_{pair[0]}_{pair[1]}"),
                )
            return httpx.Response(
                200,
                json=availability_payload(pair[0], pair[1], [responder]),
                headers=usage_headers(f"req_test_avail_{pair[0]}_{pair[1]}"),
            )
        return httpx.Response(404, json=error_envelope("NOT_FOUND", "unknown endpoint", "CLIENT"))

    return handler


def make_client(handler, *, api_key: str = "test-key", **kwargs) -> RailCoreClient:
    kwargs.setdefault("max_retries", 0)
    kwargs.setdefault("backoff_base_seconds", 0.0)
    return RailCoreClient(
        api_key=api_key,
        base_url=MOCK_BASE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def request_body(**overrides) -> dict:
    base = {
        "train_number": MOCK_TRAIN,
        "from_station_1": "MYS",
        "to_station_1": "ASK",
        "from_station_2": "ASK",
        "to_station_2": "DWR",
        "journey_date": tomorrow_ist(),
        "travel_class": "SL",
        "quota": "GN",
    }
    base.update(overrides)
    return base


# ============================================================================
# Phase 2: same-train combination search mocks (also fully synthetic).
# The route/trains below mirror the real MYS -> DWR scenario but use
# fictional train numbers (999xx) and configurable availability.
# ============================================================================

ALL_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

# Synthetic candidate trains for MYS -> DWR (mirrors the real scenario):
#   99901 - every segment pair available (overnight, all 15 window pairs)
#   99902 - mixed: some pairs available, ASK->DWR NOT AVAILABLE
#   99903 - everything RAC / waitlist (must yield nothing)
#   99904 - does not carry SL (skipped before any availability query)
#   99905 - does not run on the journey weekday (skipped after schedule)
#   99906 - only MYS->ASK and ASK->DWR available (single combination)
TRAIN_ALL_AVAILABLE = "99901"
TRAIN_ONE_LEG_UNAVAILABLE = "99902"
TRAIN_ALL_WAITLIST = "99903"
TRAIN_NO_SL_CLASS = "99904"
TRAIN_NOT_RUNNING = "99905"
TRAIN_SINGLE_COMBO = "99906"

# Fares for 99901 (SL) by station pair; chosen so ranking orders differ.
FARES_ALL_AVAILABLE: dict[tuple[str, str], float] = {
    ("MYS", "KRNR"): 60, ("MYS", "HAS"): 120, ("MYS", "ASK"): 165,
    ("MYS", "UBL"): 340, ("MYS", "DWR"): 400,
    ("KRNR", "HAS"): 70, ("KRNR", "ASK"): 110, ("KRNR", "UBL"): 290,
    ("KRNR", "DWR"): 350,
    ("HAS", "ASK"): 55, ("HAS", "UBL"): 230, ("HAS", "DWR"): 290,
    ("ASK", "UBL"): 180, ("ASK", "DWR"): 250,
    ("UBL", "DWR"): 75,
}


def route_train_entry(
    number: str,
    *,
    name: str | None = None,
    classes: tuple[str, ...] = ("SL", "3A", "2A"),
    running_days: list[str] | None = None,
) -> dict:
    return {
        "train_number": number,
        "train_name": name or f"MOCK {number} EXPRESS",
        "extended_train_name": name or f"Mock {number} Express",
        "from_station_code": "MYS",
        "from_station_name": "Mysuru Junction",
        "to_station_code": "DWR",
        "to_station_name": "Dharwad",
        "departure_time": "22:45",
        "arrival_time": "06:43",
        "duration_minutes": 478,
        "distance_km": 436,
        "train_type": "MAIL_EXPRESS",
        "classes": list(classes),
        "running_days": running_days if running_days is not None else list(ALL_DAYS),
        "has_pantry": False,
        "availability_summary": [],
    }


def route_trains_payload(
    numbers: list[str], *, journey_date: str | None = None
) -> dict:
    return {
        "success": True,
        "data": {
            "from_station_code": "MYS",
            "to_station_code": "DWR",
            "journey_date": journey_date or tomorrow_ist(),
            "quota": "GN",
            "matched_by": "station",
            "trains": [route_train_entry(number) for number in numbers],
        },
        "meta": {
            "api_version": "v1",
            "request_id": "req_test_routes",
            "trace_id": "trc_test_routes",
            "timestamp": "2026-11-23T12:00:00+05:30",
        },
    }


def schedule_payload_for(
    train_number: str,
    *,
    running_days: list[str] | None = None,
    classes: tuple[str, ...] = ("SL", "3A", "2A"),
) -> dict:
    """Schedule for a synthetic 999xx train on the overnight MYS->BGM route."""
    return {
        "success": True,
        "data": {
            "train_number": train_number,
            "train_name": f"MOCK {train_number} EXPRESS",
            "source_station_code": "MYS",
            "destination_station_code": "BGM",
            "running_days": running_days if running_days is not None else list(ALL_DAYS),
            "classes": list(classes),
            "total_duration_minutes": 670,
            "distance_km": 491,
            "stops": MOCK_STOPS,
        },
        "meta": {
            "api_version": "v1",
            "request_id": f"req_test_sched_{train_number}",
            "trace_id": f"trc_test_sched_{train_number}",
            "timestamp": "2026-11-23T12:00:00+05:30",
            "freshness": {"mode": "live", "retrieved_at": "2026-11-23T06:30:00Z"},
        },
    }


def avail_entry_for(
    pair: tuple[str, str],
    *,
    status: str = "AVAILABLE",
    class_code: str = "SL",
    fare: float | None = None,
    no_fare: bool = False,
) -> dict:
    """A classes-entry for one station pair with a deterministic count."""
    count = int(abs(hash(pair)) % 90) + 5 if status == "AVAILABLE" else None
    text = (
        f"AVAILABLE-{count:04d}"
        if status == "AVAILABLE"
        else {"RAC": "RAC 12", "WAITLIST": "GNWL45/WL12", "NOT_AVAILABLE": "NOT AVAILABLE"}.get(status, status)
    )
    return avail_entry(
        status=status,
        class_code=class_code,
        text=text,
        available_count=count,
        waitlist_count=12 if status == "WAITLIST" else None,
        rac_count=12 if status == "RAC" else None,
        fare=None if no_fare else (fare if fare is not None else 100),
    )


def all_available_map(train: str, fares: dict[tuple[str, str], float]) -> dict:
    """Availability map with every window pair AVAILABLE at the given fares."""
    return {
        (train, frm, to): avail_entry_for((frm, to), fare=fare)
        for (frm, to), fare in fares.items()
    }


def journey_body(**overrides) -> dict:
    base = {
        "from_station": "MYS",
        "to_station": "DWR",
        "journey_date": tomorrow_ist(),
        "travel_class": "SL",
        "quota": "GN",
    }
    base.update(overrides)
    return base


@pytest.fixture
def make_test_client():
    """Factory: (handler, **client_kwargs) -> TestClient with the provider overridden."""
    created: list[RailCoreClient] = []

    def _make(handler, *, api_key: str = "test-key", **client_kwargs) -> TestClient:
        client = make_client(handler, api_key=api_key, **client_kwargs)
        created.append(client)
        fastapi_app.dependency_overrides[get_railcore_client] = lambda: client
        return TestClient(fastapi_app)

    yield _make
    fastapi_app.dependency_overrides.clear()
    for client in created:
        client.close()


# ============================================================================
# Phase 3: train-change connection engine mocks (fully synthetic 998xx trains).
# The builders below are generic primitives: schedules with arbitrary stops
# (day offsets included), per-pair route discovery payloads, and a pair-keyed
# discovery responder. Availability entries are (train, from, to) keyed.
# Mock data appears only in automated tests; the production search flow is
# never faked.
# ============================================================================


def tc_stop(
    seq: int,
    code: str,
    name: str,
    arr: str | None,
    dep: str | None,
    *,
    day: int = 1,
    dist: float | None = None,
    is_stop: bool = True,
    halt: int | None = None,
) -> dict:
    """One synthetic schedule stop (day = journey day number, 1-based)."""
    return {
        "sequence": seq,
        "station_code": code,
        "station_name": name,
        "stop_type": "ORIGIN" if seq == 1 and dep else "HALT",
        "arrival_time": arr,
        "departure_time": dep,
        "halt_minutes": halt if halt is not None else (0 if seq == 1 else 2),
        "day": day,
        "distance_km": dist,
        "is_stop": is_stop,
    }


def tc_schedule(
    train_number: str,
    stops: list[dict],
    *,
    name: str | None = None,
    classes: tuple[str, ...] = ("SL", "3A"),
    running_days: list[str] | None = None,
) -> dict:
    """A synthetic schedule payload for a 998xx train."""
    return {
        "success": True,
        "data": {
            "train_number": train_number,
            "train_name": name or f"MOCK {train_number} EXPRESS",
            "source_station_code": stops[0]["station_code"],
            "destination_station_code": stops[-1]["station_code"],
            "running_days": running_days if running_days is not None else list(ALL_DAYS),
            "classes": list(classes),
            "total_duration_minutes": 600,
            "distance_km": stops[-1].get("distance_km"),
            "stops": stops,
        },
        "meta": {
            "api_version": "v1",
            "request_id": f"req_test_sched_{train_number}",
            "trace_id": f"trc_test_sched_{train_number}",
            "timestamp": "2026-11-23T12:00:00+05:30",
            "freshness": {"mode": "live", "retrieved_at": "2026-11-23T06:30:00Z"},
        },
    }


def tc_route_entry(
    number: str,
    *,
    frm: str,
    to: str,
    dep: str,
    arr: str,
    name: str | None = None,
    classes: tuple[str, ...] = ("SL", "3A"),
    running_days: list[str] | None = None,
) -> dict:
    """One train entry in a /routes/trains payload (pair-parametric)."""
    return {
        "train_number": number,
        "train_name": name or f"MOCK {number} EXPRESS",
        "extended_train_name": name or f"Mock {number} Express",
        "from_station_code": frm,
        "from_station_name": frm,
        "to_station_code": to,
        "to_station_name": to,
        "departure_time": dep,
        "arrival_time": arr,
        "duration_minutes": 240,
        "distance_km": 300,
        "train_type": "MAIL_EXPRESS",
        "classes": list(classes),
        "running_days": running_days if running_days is not None else list(ALL_DAYS),
        "has_pantry": False,
        "availability_summary": [],
    }


def tc_routes(frm: str, to: str, entries: list[dict], *, journey_date: str | None = None) -> dict:
    """A full /routes/trains payload for one station pair."""
    return {
        "success": True,
        "data": {
            "from_station_code": frm,
            "to_station_code": to,
            "journey_date": journey_date or tomorrow_ist(),
            "quota": "GN",
            "matched_by": "station",
            "trains": entries,
        },
        "meta": {
            "api_version": "v1",
            "request_id": f"req_test_routes_{frm}_{to}",
            "trace_id": f"trc_test_routes_{frm}_{to}",
            "timestamp": "2026-11-23T12:00:00+05:30",
        },
    }


def tc_routes_responder(pairs: dict[tuple[str, str], dict]):
    """Discovery responder keyed by (from, to); unknown pairs answer 404
    NO_TRAINS_FOUND exactly like the real provider (a failed, uncounted,
    honest dead end)."""

    def responder(query: dict) -> httpx.Response:
        key = (query.get("from", ""), query.get("to", ""))
        payload = pairs.get(key)
        if payload is None:
            return httpx.Response(
                404,
                json=error_envelope(
                    "NO_TRAINS_FOUND",
                    f"no trains between {key[0]} and {key[1]}",
                    "CLIENT",
                ),
            )
        return httpx.Response(200, json=payload, headers=usage_headers(f"req_test_routes_{key[0]}_{key[1]}"))

    return responder


def tc_avail(
    *,
    status: str = "AVAILABLE",
    class_code: str = "SL",
    fare: float | None = 100,
    count: int = 40,
    no_fare: bool = False,
) -> dict:
    """A deterministic availability classes-entry for one train leg."""
    text = {
        "AVAILABLE": f"AVAILABLE-{count:04d}",
        "RAC": "RAC 12",
        "WAITLIST": "GNWL45/WL12",
        "NOT_AVAILABLE": "NOT AVAILABLE",
        "UNKNOWN": "UNKNOWN",
    }.get(status, status)
    return avail_entry(
        status=status,
        class_code=class_code,
        text=text,
        available_count=count if status == "AVAILABLE" else None,
        waitlist_count=12 if status == "WAITLIST" else None,
        rac_count=12 if status == "RAC" else None,
        fare=None if no_fare else fare,
    )
