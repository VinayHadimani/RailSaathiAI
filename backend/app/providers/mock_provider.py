"""Synthetic Mock RailCore Provider for local demo and offline verification.

Provides a synthetic rail network (trains 99701, 99702, 99703) on the
MYS -> ASK -> SBC -> UBL -> DWR -> BGM corridor. Zero external network calls
are made, zero API credits are consumed, and no real booking or railway
credentials are required.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .railcore import RailCoreClient

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

ALL_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# Train Numbers
T_EXP_1 = "99701"  # MYS 08:00 -> KRNR 09:10 -> ASK 10:30 -> SBC 12:30 -> UBL 15:00 -> DWR 17:30 -> BGM 19:30
T_EXP_2 = "99702"  # ASK 11:35 -> SBC 13:30 -> UBL 16:00 -> DWR 18:30 -> BGM 20:30
T_EXP_3 = "99703"  # SBC 14:05 -> UBL 16:30 -> DWR 19:00 -> BGM 21:00


def _usage_headers(request_id: str) -> dict[str, str]:
    return {
        "x-railcore-request-id": request_id,
        "x-railcore-credits-cost": "1",
        "x-railcore-credits-remaining": "500",
        "x-railcore-ratelimit-remaining": "10",
        "x-railcore-ratelimit-day-remaining": "100",
    }


def _error_envelope(code: str, message: str, category: str = "CLIENT") -> dict:
    return {
        "success": False,
        "error": {"code": code, "message": message, "category": category, "retryable": False},
        "meta": {"api_version": "v1", "request_id": "req_mock_error", "timestamp": "2026-11-24T12:00:00+05:30"},
    }


def _stop(
    seq: int,
    code: str,
    name: str,
    arr: str | None,
    dep: str | None,
    *,
    dist: int = 0,
    halt: int = 0,
    day: int = 1,
) -> dict:
    return {
        "sequence": seq,
        "station_code": code,
        "station_name": name,
        "stop_type": "ORIGIN" if arr is None else ("DESTINATION" if dep is None else "HALT"),
        "arrival_time": arr,
        "departure_time": dep,
        "halt_minutes": halt,
        "day": day,
        "distance_km": dist,
        "is_stop": True,
    }


def _schedule(train_number: str, name: str, stops: list[dict], classes=("SL", "3A", "2A")) -> dict:
    return {
        "success": True,
        "data": {
            "train_number": train_number,
            "train_name": name,
            "source_station_code": stops[0]["station_code"],
            "destination_station_code": stops[-1]["station_code"],
            "running_days": list(ALL_DAYS),
            "classes": list(classes),
            "total_duration_minutes": 690,
            "distance_km": stops[-1]["distance_km"],
            "stops": stops,
        },
        "meta": {
            "api_version": "v1",
            "request_id": f"req_sched_{train_number}",
            "trace_id": f"trc_sched_{train_number}",
            "timestamp": "2026-11-24T12:00:00+05:30",
            "freshness": {"mode": "live", "retrieved_at": "2026-11-24T06:00:00Z"},
        },
    }


def _avail(status: str = "AVAILABLE", count: int = 40, fare: float = 180.0, class_code: str = "SL") -> dict:
    text = f"AVAILABLE-{count:04d}" if status == "AVAILABLE" else ("GNWL45/WL12" if status == "WAITLIST" else "NOT AVAILABLE")
    return {
        "class_code": class_code,
        "class_name": "Sleeper Class" if class_code == "SL" else "AC 3 Tier",
        "status": status,
        "status_text": text,
        "available_count": count if status == "AVAILABLE" else None,
        "fare": fare,
        "quota": "GN",
        "updated_at": "2026-11-24T06:00:00Z",
    }


def get_mock_schedules() -> dict[str, dict]:
    return {
        T_EXP_1: _schedule(
            T_EXP_1,
            "MOCK 99701 KARNATAKA EXP",
            [
                _stop(1, "MYS", "Mysuru Junction", None, "08:00", dist=0),
                _stop(2, "KRNR", "Krishnarajanagar", "09:10", "09:15", dist=35, halt=5),
                _stop(3, "ASK", "Arsikere Junction", "10:30", "10:35", dist=160, halt=5),
                _stop(4, "SBC", "KSR Bengaluru City", "12:30", "12:35", dist=320, halt=5),
                _stop(5, "UBL", "SSS Hubballi Junction", "15:00", "15:05", dist=410, halt=5),
                _stop(6, "DWR", "Dharwad", "17:30", "17:35", dist=440, halt=5),
                _stop(7, "BGM", "Belagavi", "19:30", None, dist=520),
            ],
        ),
        T_EXP_2: _schedule(
            T_EXP_2,
            "MOCK 99702 RANI CHENNAMMA",
            [
                _stop(1, "ASK", "Arsikere Junction", None, "11:35", dist=0),
                _stop(2, "SBC", "KSR Bengaluru City", "13:30", "13:35", dist=160, halt=5),
                _stop(3, "UBL", "SSS Hubballi Junction", "16:00", "16:05", dist=250, halt=5),
                _stop(4, "DWR", "Dharwad", "18:30", "18:35", dist=280, halt=5),
                _stop(5, "BGM", "Belagavi", "20:30", None, dist=360),
            ],
        ),
        T_EXP_3: _schedule(
            T_EXP_3,
            "MOCK 99703 GOL GUMBAZ EXP",
            [
                _stop(1, "SBC", "KSR Bengaluru City", None, "14:05", dist=0),
                _stop(2, "UBL", "SSS Hubballi Junction", "16:30", "16:35", dist=90, halt=5),
                _stop(3, "DWR", "Dharwad", "19:00", "19:05", dist=120, halt=5),
                _stop(4, "BGM", "Belagavi", "21:00", None, dist=200),
            ],
        ),
    }


def get_mock_availability() -> dict[tuple[str, str, str], dict]:
    # (train_number, from, to) -> availability entry
    # Distinct fares so rankings differ (FASTEST vs CHEAPEST)
    return {
        # 99701
        (T_EXP_1, "MYS", "KRNR"): _avail(fare=60, count=45),
        (T_EXP_1, "KRNR", "ASK"): _avail(fare=55, count=42),
        (T_EXP_1, "MYS", "ASK"): _avail(fare=165, count=38),
        (T_EXP_1, "ASK", "SBC"): _avail(fare=45, count=50),
        (T_EXP_1, "SBC", "UBL"): _avail(fare=180, count=40),
        (T_EXP_1, "UBL", "DWR"): _avail(fare=75, count=35),
        (T_EXP_1, "DWR", "BGM"): _avail(fare=85, count=30),
        (T_EXP_1, "SBC", "DWR"): _avail(fare=255, count=28),
        (T_EXP_1, "SBC", "BGM"): _avail(fare=340, count=25),
        (T_EXP_1, "MYS", "DWR"): _avail(fare=420, count=20),
        (T_EXP_1, "MYS", "BGM"): _avail(fare=510, count=18),
        (T_EXP_1, "UBL", "BGM"): _avail(fare=160, count=32),
        # 99702
        (T_EXP_2, "ASK", "SBC"): _avail(fare=45, count=35),
        (T_EXP_2, "SBC", "UBL"): _avail(fare=160, count=45),
        (T_EXP_2, "UBL", "DWR"): _avail(fare=65, count=40),
        (T_EXP_2, "DWR", "BGM"): _avail(fare=75, count=38),
        (T_EXP_2, "SBC", "DWR"): _avail(fare=225, count=30),
        (T_EXP_2, "SBC", "BGM"): _avail(fare=300, count=25),
        (T_EXP_2, "UBL", "BGM"): _avail(fare=140, count=35),
        # 99703
        (T_EXP_3, "SBC", "UBL"): _avail(fare=150, count=22),
        (T_EXP_3, "UBL", "DWR"): _avail(fare=60, count=20),
        (T_EXP_3, "DWR", "BGM"): _avail(fare=70, count=18),
        (T_EXP_3, "SBC", "DWR"): _avail(fare=210, count=15),
        (T_EXP_3, "SBC", "BGM"): _avail(fare=280, count=14),
        (T_EXP_3, "UBL", "BGM"): _avail(fare=130, count=25),
        # Also provide a WAITLIST segment to generate honest unverified candidates
        (T_EXP_2, "ASK", "DWR"): _avail(status="WAITLIST", fare=230),
    }


def create_mock_transport_handler() -> Any:
    schedules = get_mock_schedules()
    availability = get_mock_availability()

    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path

        # 1. Batch seat availability
        if url_path.endswith("/availability/seats/batch"):
            body = json.loads(request.read().decode("utf-8"))
            results = []
            for item in body.get("items", []):
                t_num = str(item.get("train_number", ""))
                frm = item.get("from", "")
                to = item.get("to", "")
                entry = availability.get((t_num, frm, to), _avail(status="NOT_AVAILABLE"))
                results.append({
                    "train_number": t_num,
                    "from_station_code": frm,
                    "to_station_code": to,
                    "journey_date": item.get("date"),
                    "quota": item.get("quota", "GN"),
                    "classes": [entry],
                })
            return httpx.Response(
                200,
                json={"success": True, "data": {"results": results}, "meta": {"api_version": "v1", "request_id": "req_mock_batch"}},
                headers=_usage_headers("req_mock_batch"),
            )

        # 2. Routes / trains discovery
        if url_path.endswith("/routes/trains"):
            query = dict(request.url.params)
            frm = query.get("from", "")
            to = query.get("to", "")
            entries = []
            for t_num, payload in schedules.items():
                stops = payload["data"]["stops"]
                codes = [s["station_code"] for s in stops]
                if frm in codes and to in codes and codes.index(frm) < codes.index(to):
                    dep = next(s["departure_time"] for s in stops if s["station_code"] == frm and s["departure_time"])
                    arr = next(s["arrival_time"] for s in stops if s["station_code"] == to and s["arrival_time"])
                    entries.append({
                        "train_number": t_num,
                        "train_name": payload["data"]["train_name"],
                        "extended_train_name": payload["data"]["train_name"],
                        "from_station_code": frm,
                        "from_station_name": frm,
                        "to_station_code": to,
                        "to_station_name": to,
                        "departure_time": dep,
                        "arrival_time": arr,
                        "duration_minutes": 240,
                        "distance_km": 300,
                        "train_type": "MAIL_EXPRESS",
                        "classes": ["SL", "3A", "2A"],
                        "running_days": list(ALL_DAYS),
                        "has_pantry": False,
                        "availability_summary": [],
                    })
            if not entries:
                return httpx.Response(404, json=_error_envelope("NO_TRAINS_FOUND", f"no trains between {frm} and {to}"))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "from_station_code": frm,
                        "to_station_code": to,
                        "journey_date": query.get("date", "2026-11-25"),
                        "quota": "GN",
                        "matched_by": "station",
                        "trains": entries,
                    },
                    "meta": {"api_version": "v1", "request_id": "req_mock_routes"},
                },
                headers=_usage_headers("req_mock_routes"),
            )

        # 3. Schedule endpoint
        if url_path.endswith("/schedule"):
            t_num = url_path.split("/")[-2]
            if t_num in schedules:
                return httpx.Response(200, json=schedules[t_num], headers=_usage_headers(f"req_mock_sched_{t_num}"))
            return httpx.Response(404, json=_error_envelope("TRAIN_NOT_FOUND", f"no train {t_num}"))

        # 4. Single seat availability
        if url_path.endswith("/availability/seats"):
            query = dict(request.url.params)
            frm = query.get("from", "")
            to = query.get("to", "")
            t_num = query.get("train_number", "")
            entry = availability.get((t_num, frm, to), _avail(status="NOT_AVAILABLE"))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "train_number": t_num,
                        "from_station_code": frm,
                        "to_station_code": to,
                        "journey_date": query.get("date", "2026-11-25"),
                        "classes": [entry],
                    },
                    "meta": {"api_version": "v1", "request_id": "req_mock_avail"},
                },
                headers=_usage_headers("req_mock_avail"),
            )

        return httpx.Response(404, json=_error_envelope("ENDPOINT_NOT_FOUND", "Endpoint not found"))

    return handler


def create_mock_railcore_client() -> RailCoreClient:
    """Build a deterministic RailCoreClient over synthetic mock transport."""
    handler = create_mock_transport_handler()
    return RailCoreClient(
        api_key="mock-key-synthetic",
        base_url="https://railcore.mock/v1",
        timeout_seconds=10.0,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
