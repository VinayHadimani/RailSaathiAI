"""Unit tests for LocalTimetableStore and SegmentQueryPlanner local hybrid mode."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.models.schedule import ScheduleStop, TrainSchedule
from app.providers.base import ProviderServerError
from app.providers.railcore import RailCoreClient
from app.services.local_timetable_store import LocalTimetableStore
from app.services.search_budgets import SearchBudgets
from app.services.segment_query_planner import SegmentQueryPlanner


def _make_schedule(
    train_number: str = "12702",
    *,
    running_days: list[str] | None = None,
    classes: list[str] | None = None,
    stops: list[ScheduleStop] | None = None,
    provider: str = "ntes",
) -> TrainSchedule:
    if stops is None:
        stops = [
            ScheduleStop(
                sequence=1,
                station_code="HYB",
                station_name="HYDERABAD DECAN",
                stop_type="SOURCE",
                arrival_time=None,
                departure_time="14:45",
                day=1,
                distance_km=0,
                is_stop=True,
            ),
            ScheduleStop(
                sequence=2,
                station_code="PUNE",
                station_name="PUNE JN",
                stop_type="ENROUTE",
                arrival_time="01:15",
                departure_time="01:20",
                halt_minutes=5,
                day=2,
                distance_km=597,
                is_stop=True,
            ),
            ScheduleStop(
                sequence=3,
                station_code="CSMT",
                station_name="C SHIVAJI MAH T",
                stop_type="DEST",
                arrival_time="05:00",
                departure_time=None,
                day=2,
                distance_km=789,
                is_stop=True,
            ),
        ]
    return TrainSchedule(
        train_number=train_number,
        train_name="HUSSAIN SAGAR EXP",
        source_station_code=stops[0].station_code,
        destination_station_code=stops[-1].station_code,
        running_days=["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        if running_days is None
        else running_days,
        classes=["1A", "2A", "3A", "SL"] if classes is None else classes,
        total_duration_minutes=855,
        total_distance_km=stops[-1].distance_km,
        stops=stops,
        provider=provider,
    )


def _seed_store(
    db_path: Path,
    schedules: list[tuple[TrainSchedule, str]],
    *,
    overlay_path: Path | None = None,
) -> LocalTimetableStore:
    store = LocalTimetableStore(db_path=db_path, overlay_path=overlay_path)
    conn = store._connect_rw(db_path)
    try:
        with conn:
            for sched, src in schedules:
                LocalTimetableStore.write_schedule(conn, sched, source=src)
    finally:
        conn.close()
    return store


def test_store_roundtrip_and_segment_distance(tmp_path: Path) -> None:
    db_path = tmp_path / "trains.db"
    store = _seed_store(db_path, [(_make_schedule("12702"), "ntes")])

    assert store.is_available
    sched = store.get_schedule("12702", require_fresh_source=True)
    assert sched is not None
    assert sched.train_number == "12702"
    assert sched.total_distance_km == 789
    assert len(sched.stops) == 3

    # Query intermediate segment PUNE -> CSMT; stop 2 (597km) -> stop 3 (789km) = 192km
    res = store.get_trains_between(
        from_station="PUNE",
        to_station="CSMT",
        journey_date="2026-12-01",
        quota="GN",
        require_fresh_source=True,
    )
    assert res is not None
    assert len(res.trains) == 1
    tr = res.trains[0]
    assert tr.train_number == "12702"
    assert tr.departure_time == "01:20"
    assert tr.arrival_time == "05:00"
    assert tr.distance_km == 192
    assert tr.classes == ["1A", "2A", "3A", "SL"]

    # Reverse direction CSMT -> PUNE must NOT match
    rev = store.get_trains_between(
        from_station="CSMT",
        to_station="PUNE",
        journey_date="2026-12-01",
        require_fresh_source=True,
    )
    assert rev is None or rev.trains == []


def test_store_multiday_running_day_rollover(tmp_path: Path) -> None:
    db_path = tmp_path / "trains.db"
    # Train starts HYB on Day 1 = MON; reaches PUNE on Day 2 = TUE (2026-12-01 is Tuesday).
    mon_only = _make_schedule("22101", running_days=["MON"])
    tue_only = _make_schedule("22102", running_days=["TUE"])
    store = _seed_store(db_path, [(mon_only, "ntes"), (tue_only, "ntes")])

    res = store.get_trains_between(
        from_station="PUNE",
        to_station="CSMT",
        journey_date="2026-12-01",  # Tuesday
        require_fresh_source=True,
    )
    assert res is not None
    assert [t.train_number for t in res.trains] == ["22101"]


def test_store_fresh_vs_datameet_and_overlay_upsert(tmp_path: Path) -> None:
    db_path = tmp_path / "trains.db"
    overlay_path = tmp_path / "overlay.db"
    base_sched = _make_schedule("11007", running_days=[], classes=["SL"], provider="datameet")
    store = _seed_store(db_path, [(base_sched, "datameet")], overlay_path=overlay_path)

    # Fresh filter excludes datameet initially
    assert store.get_schedule("11007", require_fresh_source=True) is None
    assert store.get_schedule("11007", require_fresh_source=False) is not None

    # Upsert live RailCore schedule -> lands in primary/overlay and passes fresh filter
    live_sched = _make_schedule(
        "11007", running_days=["MON", "TUE"], classes=["EV", "CC", "2S"], provider="railcore"
    )
    store.upsert_schedule(live_sched, source="railcore")

    fresh = store.get_schedule("11007", require_fresh_source=True)
    assert fresh is not None
    assert fresh.classes == ["EV", "CC", "2S"]
    assert fresh.running_days == ["MON", "TUE"]


def test_planner_uses_local_store_with_zero_credits_and_falls_back_to_railcore(
    tmp_path: Path,
) -> None:
    SegmentQueryPlanner._shared_schedule_cache.clear()
    SegmentQueryPlanner._shared_discovery_cache.clear()

    db_path = tmp_path / "trains.db"
    overlay_path = tmp_path / "overlay.db"
    ntes_sched = _make_schedule("12702", provider="ntes")
    datameet_sched = _make_schedule(
        "19999",
        stops=[
            ScheduleStop(
                sequence=1,
                station_code="MYS",
                station_name="MYSURU JN",
                stop_type="SOURCE",
                departure_time="06:00",
                day=1,
                distance_km=0,
            ),
            ScheduleStop(
                sequence=2,
                station_code="BGM",
                station_name="BELAGAVI",
                stop_type="DEST",
                arrival_time="18:00",
                day=1,
                distance_km=612,
            ),
        ],
        provider="datameet",
    )
    store = _seed_store(
        db_path,
        [(ntes_sched, "ntes"), (datameet_sched, "datameet")],
        overlay_path=overlay_path,
    )

    provider_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request.url.path)
        if request.url.path.endswith("/trains/12123/schedule"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "train_number": "12123",
                        "train_name": "DECCAN QUEEN",
                        "source_station_code": "CSMT",
                        "destination_station_code": "PUNE",
                        "running_days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                        "classes": ["EV", "CC", "2S"],
                        "total_duration_minutes": 190,
                        "total_distance_km": 192,
                        "stops": [
                            {
                                "sequence": 1,
                                "station_code": "CSMT",
                                "station_name": "CSMT",
                                "stop_type": "SOURCE",
                                "departure_time": "17:10",
                                "day": 1,
                                "distance_km": 0,
                                "is_stop": True,
                            },
                            {
                                "sequence": 2,
                                "station_code": "PUNE",
                                "station_name": "PUNE JN",
                                "stop_type": "DEST",
                                "arrival_time": "20:25",
                                "day": 1,
                                "distance_km": 192,
                                "is_stop": True,
                            },
                        ],
                    },
                },
            )
        if request.url.path.endswith("/routes/trains"):
            return httpx.Response(
                503,
                json={"success": False, "error": {"code": "DOWN", "message": "down"}},
            )
        return httpx.Response(404, json={"success": False})

    client = RailCoreClient(
        api_key="rk_test",
        base_url="https://api.example.test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    planner = SegmentQueryPlanner(client, SearchBudgets(), timetable_store=store)

    # 1. Fresh NTES route & schedule -> 0 provider calls!
    trains_res = planner.get_trains_between(
        from_station="PUNE", to_station="CSMT", journey_date="2026-12-01", quota="GN"
    )
    sched_res = planner.get_schedule("12702")
    assert len(trains_res.trains) == 1
    assert sched_res.train_name == "HUSSAIN SAGAR EXP"
    assert provider_calls == []
    assert planner.stats["requests_sent"] == 0
    assert planner.stats["local_discovery_hits"] == 1
    assert planner.stats["local_schedule_hits"] == 1

    # 2. Train missing locally -> calls RailCore once and writes through to local store
    live_sched = planner.get_schedule("12123")
    assert live_sched.train_name == "DECCAN QUEEN"
    assert len(provider_calls) == 1

    SegmentQueryPlanner._shared_schedule_cache.clear()
    planner2 = SegmentQueryPlanner(client, SearchBudgets(), timetable_store=store)
    cached_locally = planner2.get_schedule("12123")
    assert cached_locally.train_name == "DECCAN QUEEN"
    assert len(provider_calls) == 1  # No second HTTP call!

    # 3. RailCore unavailable on corridor with only DataMeet baseline -> falls back to DataMeet
    fallback_res = planner2.get_trains_between(
        from_station="MYS", to_station="BGM", journey_date="2026-12-01", quota="GN"
    )
    assert [t.train_number for t in fallback_res.trains] == ["19999"]
    assert fallback_res.trains[0].distance_km == 612

    # 4. Injected mock client without explicit timetable_store keeps store disabled (isolation)
    isolated_planner = SegmentQueryPlanner(client, SearchBudgets())
    assert isolated_planner._timetable_store is None
    with pytest.raises(ProviderServerError):
        isolated_planner.get_trains_between(
            from_station="PUNE", to_station="CSMT", journey_date="2026-12-02", quota="GN"
        )
