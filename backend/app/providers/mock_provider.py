"""Synthetic Mock RailCore Provider for local demo and offline verification.

ZERO external network calls, ZERO API credits consumed, no real booking or
railway credentials required — while the full RailCoreClient request/response
contract, the normalizers and all journey engines run exactly as they do live.

Data provenance (2026-11-26/27 recorded live RailCore runs — see
railroute_same_train_search_real_*.json in the repo root):
- The corridor station order MYS -> HLN -> HAS -> ASK -> RRB -> DVG -> HRR ->
  RNR -> HVR -> UBL -> DWR -> BGM is the REAL station sequence captured from
  the live provider for train 12781 (Swarna Jayanthi) during its recorded
  live run.
- Train numbers and names (17301 MYS BGM EXP, 12781 SWARNA JAYANTHI,
  20676 VISHWAMANAV EXP, 16210 AJMER EXPRESS, 11005 PUNE BGM BENGALURU
  EXPRESS, 17309 YESVANTPUR BGM EXPRESS) come from the live provider and the
  repo's own collected corridor dataset.
- 3A fares such as MYS->ASK 565, MYS->DVG 650, MYS->HRR 675, MYS->RNR 695,
  MYS->HVR 735, MYS->UBL 835, UBL->DWR 565 are the REAL fares returned by
  the live provider during those runs.
- 12781's MYS 19:30 departure and 03:30 UBL arrival are the real captured
  times; the remaining timetable times, the Bengaluru (SBC/YPR) branch and
  all availability counts are simulated deterministically to exercise every
  engine (same-train seat-change combinations, train-change connections,
  mixed journeys, unverified waitlist candidates).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .railcore import RailCoreClient

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

ALL_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# Real corridor stations (codes + names from live RailCore schedule capture),
# with approximate rail kilometres measured along the Mysuru line.
CORRIDOR = [
    ("MYS", "Mysuru Junction", 0),
    ("HLN", "Hole Narsipur", 52),
    ("HAS", "Hassan Junction", 107),
    ("ASK", "Arsikere Junction", 150),
    ("RRB", "Birur Junction", 184),
    ("DVG", "Davangere", 284),
    ("HRR", "Harihar", 299),
    ("RNR", "Ranibennur", 329),
    ("HVR", "Mmailara Haveri", 374),
    ("UBL", "SSS Hubballi Junction", 444),
    ("DWR", "Dharwad", 464),
    ("BGM", "Belagavi", 574),
]
CORRIDOR_DIST = {code: km for code, _, km in CORRIDOR}
CORRIDOR_NAME = {code: name for code, name, _ in CORRIDOR}

# Bengaluru branch: SBC / YPR hang off Arsikere (ASK) by ~140 km of rail.
BRANCH_STATIONS = {"SBC": ("KSR Bengaluru City", 140), "YPR": ("Yesvantpur Junction", 140)}
BRANCH_KM = 140
BRANCH_AT = "ASK"

# Real 3A fares captured during the recorded live runs (MYS corridor anchors).
REAL_3A_FARES: dict[tuple[str, str], float] = {
    ("MYS", "HLN"): 565.0,
    ("MYS", "HAS"): 565.0,
    ("MYS", "ASK"): 565.0,
    ("MYS", "RRB"): 565.0,
    ("MYS", "DVG"): 650.0,
    ("MYS", "HRR"): 675.0,
    ("MYS", "RNR"): 695.0,
    ("MYS", "HVR"): 735.0,
    ("MYS", "UBL"): 835.0,
    ("HRR", "UBL"): 565.0,
    ("HVR", "UBL"): 565.0,
    ("RNR", "UBL"): 565.0,
    ("RRB", "UBL"): 565.0,
    ("ASK", "UBL"): 565.0,
    ("UBL", "DWR"): 565.0,
}

# Demo-only anchors that make seat-change splits competitive against the
# through fare (clearly simulated, no live provenance).
DEMO_3A_FARES: dict[tuple[str, str], float] = {
    ("MYS", "DWR"): 565.0,
    ("MYS", "BGM"): 1200.0,
    ("DWR", "BGM"): 565.0,
    ("UBL", "BGM"): 565.0,
    ("SBC", "ASK"): 565.0,
    ("SBC", "UBL"): 835.0,
    ("SBC", "DWR"): 870.0,
    ("SBC", "BGM"): 1200.0,
    ("YPR", "ASK"): 565.0,
    ("YPR", "UBL"): 835.0,
    ("YPR", "BGM"): 1200.0,
}

CLASS_FACTORS = {"SL": 0.37, "2S": 0.30, "CC": 0.9, "3A": 1.0, "3E": 1.0, "2A": 1.6, "1A": 2.6}

# Trains: real train numbers + names from the live provider / repo dataset.
T_MYS_BGM = "17301"     # MYS BGM EXP
T_SWARNA = "12781"       # SWARNA JAYANTHI (terminates at Dharwad in this capture)
T_VISHWA = "20676"       # VISHWAMANAV EXP (Bengaluru side of the corridor)
T_AJMER = "16210"        # AJMER EXPRESS (continues north via Belagavi)
T_PUNE_BGM = "11005"     # PUNE BGM BENGALURU EXPRESS (SBC side through train)
T_YPR_BGM = "17309"      # YESVANTPUR BGM EXPRESS (YPR side through train)


def _rail_km(code: str) -> int:
    if code in CORRIDOR_DIST:
        return CORRIDOR_DIST[code]
    if code in BRANCH_STATIONS:
        return CORRIDOR_DIST[BRANCH_AT] + BRANCH_KM
    return 0


def _dist(frm: str, to: str) -> int:
    a, b = _rail_km(frm), _rail_km(to)
    if frm in BRANCH_STATIONS and to in CORRIDOR_DIST:
        return BRANCH_KM + abs(CORRIDOR_DIST[to] - CORRIDOR_DIST[BRANCH_AT])
    if to in BRANCH_STATIONS and frm in CORRIDOR_DIST:
        return BRANCH_KM + abs(CORRIDOR_DIST[frm] - CORRIDOR_DIST[BRANCH_AT])
    return abs(a - b)


def _stop(
    seq: int,
    code: str,
    arr: str | None,
    dep: str | None,
    *,
    day: int = 1,
    halt: int = 2,
    train_origin: str | None = None,
) -> dict:
    dist = _dist(train_origin, code) if train_origin else CORRIDOR_DIST.get(code, _rail_km(code))
    name = CORRIDOR_NAME.get(code) or BRANCH_STATIONS.get(code, (code, 0))[0]
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


# Overnight corridor runs (real order; 12781's outer times are the captured live times).
_SWARNA_STOPS = [
    _stop(1, "MYS", None, "19:30", halt=0, train_origin="MYS"),
    _stop(2, "HLN", "20:35", "20:40", train_origin="MYS"),
    _stop(3, "HAS", "21:25", "21:30", train_origin="MYS"),
    _stop(4, "ASK", "22:35", "22:40", train_origin="MYS"),
    _stop(5, "RRB", "23:15", "23:20", train_origin="MYS"),
    _stop(6, "DVG", "00:50", "00:55", day=2, train_origin="MYS"),
    _stop(7, "HRR", "01:10", "01:15", day=2, train_origin="MYS"),
    _stop(8, "RNR", "01:45", "01:50", day=2, train_origin="MYS"),
    _stop(9, "HVR", "02:25", "02:30", day=2, train_origin="MYS"),
    _stop(10, "UBL", "03:30", "03:35", day=2, train_origin="MYS"),
    _stop(11, "DWR", "04:25", None, day=2, halt=0, train_origin="MYS"),
]

_MYSBGM_STOPS = [
    _stop(1, "MYS", None, "20:30", halt=0, train_origin="MYS"),
    _stop(2, "HLN", "21:35", "21:40", train_origin="MYS"),
    _stop(3, "HAS", "22:25", "22:30", train_origin="MYS"),
    _stop(4, "ASK", "23:35", "23:40", train_origin="MYS"),
    _stop(5, "RRB", "00:15", "00:20", day=2, train_origin="MYS"),
    _stop(6, "DVG", "01:50", "01:55", day=2, train_origin="MYS"),
    _stop(7, "HRR", "02:10", "02:15", day=2, train_origin="MYS"),
    _stop(8, "RNR", "02:45", "02:50", day=2, train_origin="MYS"),
    _stop(9, "HVR", "03:25", "03:30", day=2, train_origin="MYS"),
    _stop(10, "UBL", "04:35", "04:40", day=2, train_origin="MYS"),
    _stop(11, "DWR", "05:25", "05:30", day=2, train_origin="MYS"),
    _stop(12, "BGM", "06:15", None, day=2, halt=0, train_origin="MYS"),
]

_AJMER_STOPS = [
    _stop(1, "MYS", None, "09:30", halt=0, train_origin="MYS"),
    _stop(2, "HLN", "10:35", "10:40", train_origin="MYS"),
    _stop(3, "HAS", "11:25", "11:30", train_origin="MYS"),
    _stop(4, "ASK", "12:35", "12:40", train_origin="MYS"),
    _stop(5, "RRB", "13:15", "13:20", train_origin="MYS"),
    _stop(6, "DVG", "14:50", "14:55", train_origin="MYS"),
    _stop(7, "HRR", "15:10", "15:15", train_origin="MYS"),
    _stop(8, "RNR", "15:45", "15:50", train_origin="MYS"),
    _stop(9, "HVR", "16:25", "16:30", train_origin="MYS"),
    _stop(10, "UBL", "17:35", "17:40", train_origin="MYS"),
    _stop(11, "DWR", "18:25", "18:30", train_origin="MYS"),
    _stop(12, "BGM", "20:45", None, halt=0, train_origin="MYS"),
]

_VISHWA_STOPS = [
    _stop(1, "SBC", None, "06:15", halt=0, train_origin="SBC"),
    _stop(2, "ASK", "09:35", "09:40", train_origin="SBC"),
    _stop(3, "RRB", "10:15", "10:20", train_origin="SBC"),
    _stop(4, "DVG", "11:50", "11:55", train_origin="SBC"),
    _stop(5, "HRR", "12:10", "12:15", train_origin="SBC"),
    _stop(6, "RNR", "12:45", "12:50", train_origin="SBC"),
    _stop(7, "HVR", "13:25", "13:30", train_origin="SBC"),
    _stop(8, "UBL", "14:30", None, halt=0, train_origin="SBC"),
]

_PUNEBGM_STOPS = [
    _stop(1, "SBC", None, "09:00", halt=0, train_origin="SBC"),
    _stop(2, "ASK", "12:20", "12:25", train_origin="SBC"),
    _stop(3, "RRB", "13:05", "13:10", train_origin="SBC"),
    _stop(4, "DVG", "14:35", "14:40", train_origin="SBC"),
    _stop(5, "HRR", "14:55", "15:00", train_origin="SBC"),
    _stop(6, "RNR", "15:30", "15:35", train_origin="SBC"),
    _stop(7, "HVR", "16:05", "16:10", train_origin="SBC"),
    _stop(8, "UBL", "17:15", "17:20", train_origin="SBC"),
    _stop(9, "DWR", "18:10", "18:15", train_origin="SBC"),
    _stop(10, "BGM", "20:30", None, halt=0, train_origin="SBC"),
]

_YPRBGM_STOPS = [
    _stop(1, "YPR", None, "20:30", halt=0, train_origin="YPR"),
    _stop(2, "ASK", "01:20", "01:25", day=2, train_origin="YPR"),
    _stop(3, "RRB", "02:00", "02:05", day=2, train_origin="YPR"),
    _stop(4, "DVG", "03:35", "03:40", day=2, train_origin="YPR"),
    _stop(5, "HRR", "03:55", "04:00", day=2, train_origin="YPR"),
    _stop(6, "RNR", "04:25", "04:30", day=2, train_origin="YPR"),
    _stop(7, "HVR", "05:05", "05:10", day=2, train_origin="YPR"),
    _stop(8, "UBL", "06:15", "06:20", day=2, train_origin="YPR"),
    _stop(9, "DWR", "07:05", "07:10", day=2, train_origin="YPR"),
    _stop(10, "BGM", "08:45", None, day=2, halt=0, train_origin="YPR"),
]


def get_mock_schedules() -> dict[str, dict]:
    def _schedule(train_number: str, name: str, stops: list[dict]) -> dict:
        return {
            "success": True,
            "data": {
                "train_number": train_number,
                "train_name": name,
                "source_station_code": stops[0]["station_code"],
                "destination_station_code": stops[-1]["station_code"],
                "running_days": list(ALL_DAYS),
                "classes": ["SL", "3A", "2A"],
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

    return {
        T_MYS_BGM: _schedule(T_MYS_BGM, "MYS BGM EXP", _MYSBGM_STOPS),
        T_SWARNA: _schedule(T_SWARNA, "SWARNA JAYANTHI", _SWARNA_STOPS),
        T_VISHWA: _schedule(T_VISHWA, "VISHWAMANAV EXP", _VISHWA_STOPS),
        T_AJMER: _schedule(T_AJMER, "AJMER EXPRESS", _AJMER_STOPS),
        T_PUNE_BGM: _schedule(T_PUNE_BGM, "PUNE BGM BENGALURU EXPRESS", _PUNEBGM_STOPS),
        T_YPR_BGM: _schedule(T_YPR_BGM, "YESVANTPUR BGM EXPRESS", _YPRBGM_STOPS),
    }


def _stops_index(stops: list[dict]) -> dict[str, int]:
    return {s["station_code"]: i for i, s in enumerate(stops)}


def _fare_for(train_number: str, frm: str, to: str, travel_class: str) -> float:
    """Fare for any pair on a train's route.

    Real captured 3A anchors first, demo anchors second, then distance-based
    telescopic fallback (clearly simulated, calibrated to the real anchors).
    """
    key = (frm, to)
    base_3a = REAL_3A_FARES.get(key, DEMO_3A_FARES.get(key))
    if base_3a is None:
        base_3a = max(565.0, round(180.0 + 1.5 * _dist(frm, to), -1))
    factor = CLASS_FACTORS.get(travel_class, 1.0)
    return round(base_3a * factor / 5.0) * 5


# Deterministic availability model (simulated; exercises every engine +
# honest unverified candidates). Mirrors realistic long-haul behaviour where
# only specific ticket boundaries are available: pairs in the whitelist are
# AVAILABLE, the explicit WAITLIST pairs are waitlisted candidates, and every
# other pair is NOT AVAILABLE (sold out / not offered).
WAITLIST_PATTERNS: set[tuple[str, str, str]] = {
    (T_AJMER, "MYS", "BGM"),   # direct through ticket waitlisted -> unverified candidate
    (T_SWARNA, "MYS", "HVR"),  # long direct pair waitlisted -> split still confirmed
}

AVAILABILITY_WHITELIST: set[tuple[str, str, str]] = {
    # 17301 MYS BGM EXP: through ticket + classic split boundaries
    (T_MYS_BGM, "MYS", "BGM"),
    (T_MYS_BGM, "MYS", "DWR"), (T_MYS_BGM, "DWR", "BGM"),
    (T_MYS_BGM, "MYS", "UBL"), (T_MYS_BGM, "UBL", "BGM"), (T_MYS_BGM, "UBL", "DWR"),
    (T_MYS_BGM, "MYS", "ASK"), (T_MYS_BGM, "ASK", "UBL"),
    # 12781 SWARNA JAYANTHI (MYS -> DWR): leg tickets + boundary splits
    (T_SWARNA, "MYS", "DWR"), (T_SWARNA, "MYS", "UBL"), (T_SWARNA, "UBL", "DWR"),
    (T_SWARNA, "MYS", "ASK"), (T_SWARNA, "ASK", "UBL"),
    # 16210 AJMER EXPRESS: through ticket waitlisted, splits confirmed
    (T_AJMER, "MYS", "DWR"), (T_AJMER, "DWR", "BGM"),
    (T_AJMER, "MYS", "UBL"), (T_AJMER, "UBL", "BGM"), (T_AJMER, "UBL", "DWR"),
    (T_AJMER, "MYS", "ASK"), (T_AJMER, "ASK", "UBL"),
    # 20676 VISHWAMANAV EXP (SBC -> UBL)
    (T_VISHWA, "SBC", "UBL"), (T_VISHWA, "SBC", "ASK"), (T_VISHWA, "ASK", "UBL"),
    # 11005 PUNE BGM BENGALURU EXPRESS: direct sold out, splits confirmed
    (T_PUNE_BGM, "SBC", "UBL"), (T_PUNE_BGM, "SBC", "DWR"), (T_PUNE_BGM, "SBC", "ASK"),
    (T_PUNE_BGM, "UBL", "DWR"), (T_PUNE_BGM, "UBL", "BGM"), (T_PUNE_BGM, "DWR", "BGM"),
    (T_PUNE_BGM, "ASK", "UBL"),
    # 17309 YESVANTPUR BGM EXPRESS: through + boundary splits
    (T_YPR_BGM, "YPR", "BGM"),
    (T_YPR_BGM, "YPR", "UBL"), (T_YPR_BGM, "UBL", "BGM"), (T_YPR_BGM, "UBL", "DWR"), (T_YPR_BGM, "DWR", "BGM"),
    (T_YPR_BGM, "YPR", "ASK"), (T_YPR_BGM, "ASK", "UBL"),
}


def _availability_entry(train_number: str, frm: str, to: str, travel_class: str) -> dict:
    key = (train_number, frm, to)
    fare = _fare_for(train_number, frm, to, travel_class)
    if key in WAITLIST_PATTERNS:
        return {
            "class_code": travel_class,
            "class_name": _class_name(travel_class),
            "status": "WAITLIST",
            "status_text": "GNWL23/WL7",
            "available_count": None,
            "fare": fare,
            "quota": "GN",
            "updated_at": "2026-11-24T06:00:00Z",
        }
    if key not in AVAILABILITY_WHITELIST:
        return {
            "class_code": travel_class,
            "class_name": _class_name(travel_class),
            "status": "NOT_AVAILABLE",
            "status_text": "NOT AVAILABLE",
            "available_count": None,
            "fare": fare,
            "quota": "GN",
            "updated_at": "2026-11-24T06:00:00Z",
        }
    # AVAILABLE: deterministic pseudo-count varying by pair so text looks real.
    seed = sum(ord(c) for c in (train_number + frm + to)) % 31
    count = 12 + seed
    return {
        "class_code": travel_class,
        "class_name": _class_name(travel_class),
        "status": "AVAILABLE",
        "status_text": f"AVAILABLE-{count:04d}",
        "available_count": count,
        "fare": fare,
        "quota": "GN",
        "updated_at": "2026-11-24T06:00:00Z",
    }


def _class_name(code: str) -> str:
    return {
        "SL": "Sleeper Class",
        "2S": "Second Sitting",
        "CC": "AC Chair Car",
        "3A": "AC 3 Tier",
        "3E": "AC 3 Economy",
        "2A": "AC 2 Tier",
        "1A": "AC First Class",
    }.get(code, code)


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


def create_mock_transport_handler() -> Any:
    schedules = get_mock_schedules()

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
                classes = item.get("classes") or ["SL"]
                entry = _availability_entry(t_num, frm, to, classes[0] if classes else "SL")
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
                idx = _stops_index(stops)
                if frm in idx and to in idx and idx[frm] < idx[to]:
                    dep = next(s["departure_time"] for s in stops if s["station_code"] == frm)
                    arr = next(s["arrival_time"] for s in stops if s["station_code"] == to)
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
                        "distance_km": _dist(frm, to),
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
            travel_class = (query.get("class", "SL") or "SL").upper()
            entry = _availability_entry(t_num, frm, to, travel_class)
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
    """Build a deterministic RailCoreClient over the offline demo transport."""
    handler = create_mock_transport_handler()
    return RailCoreClient(
        api_key="mock-key-synthetic",
        base_url="https://railcore.mock/v1",
        timeout_seconds=10.0,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
