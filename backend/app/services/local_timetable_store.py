"""Local SQLite timetable store for zero-credit schedule and route lookups.

Provides fast, persistent pan-India train schedules and station-to-station
route discovery from a prebuilt SQLite database (`backend/data/trains.db`),
with optional write-through caching of live RailCore schedules into a writable
overlay DB (`/tmp/trains_overlay.db` on read-only serverless filesystems, or
in-place locally).

This keeps RailCore API credits and 10 req/min rate limits reserved strictly
for live seat availability queries (`/availability/seats/batch`).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

from ..models.route_search import RouteTrainSummary, RouteTrainsResult
from ..models.schedule import ScheduleStop, TrainSchedule

logger = logging.getLogger("railroute.services.local_timetable")

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "trains.db"

_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trains (
    train_number TEXT PRIMARY KEY,
    train_name TEXT,
    train_type TEXT,
    source_station_code TEXT,
    destination_station_code TEXT,
    running_days_json TEXT NOT NULL DEFAULT '[]',
    classes_json TEXT NOT NULL DEFAULT '[]',
    total_duration_minutes INTEGER,
    total_distance_km REAL,
    stops_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'ntes',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS station_stops (
    station_code TEXT NOT NULL,
    train_number TEXT NOT NULL,
    seq INTEGER NOT NULL,
    day INTEGER,
    arrival_time TEXT,
    departure_time TEXT,
    distance_km REAL,
    PRIMARY KEY (station_code, train_number, seq)
);

CREATE INDEX IF NOT EXISTS idx_station_stops_station_train
    ON station_stops (station_code, train_number, seq);

CREATE TABLE IF NOT EXISTS stations (
    code TEXT PRIMARY KEY,
    name TEXT,
    state TEXT,
    zone TEXT
);
"""


def _hhmm_to_minutes(hhmm: str | None) -> int | None:
    if not hhmm or ":" not in hhmm:
        return None
    parts = hhmm.strip().split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def _compute_duration_minutes(
    dep_time: str | None,
    dep_day: int | None,
    arr_time: str | None,
    arr_day: int | None,
) -> int | None:
    dep_m = _hhmm_to_minutes(dep_time)
    arr_m = _hhmm_to_minutes(arr_time)
    if dep_m is None or arr_m is None:
        return None
    d1 = dep_day if dep_day is not None else 1
    d2 = arr_day if arr_day is not None else d1
    diff = (d2 - d1) * 1440 + (arr_m - dep_m)
    if diff < 0:
        diff += 1440
    return diff if diff >= 0 else None


class LocalTimetableStore:
    """Thread-safe SQLite store for Indian Railways schedules and route pairs."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        overlay_path: str | Path | None = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        if overlay_path is not None:
            self._overlay_path: Path | None = Path(overlay_path)
        elif os.environ.get("VERCEL"):
            self._overlay_path = Path(tempfile.gettempdir()) / "railsaathi_trains_overlay.db"
        else:
            self._overlay_path = None
        self._lock = threading.Lock()
        self._overlay_initialized = False

    @property
    def is_available(self) -> bool:
        return self._db_path.is_file() or (
            self._overlay_path is not None and self._overlay_path.is_file()
        )

    def _connect_ro(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_rw(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        return conn

    def _read_connections(self) -> list[sqlite3.Connection]:
        conns: list[sqlite3.Connection] = []
        # Overlay wins when present (freshly learned schedules from live RailCore)
        if self._overlay_path is not None and self._overlay_path.is_file():
            try:
                conns.append(self._connect_ro(self._overlay_path))
            except sqlite3.Error:
                pass
        if self._db_path.is_file():
            try:
                conns.append(self._connect_ro(self._db_path))
            except sqlite3.Error as exc:
                logger.warning("Failed to open local timetable DB %s: %s", self._db_path, exc)
        return conns

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> tuple[TrainSchedule, str]:
        raw_stops_val = row["stops_json"]
        if isinstance(raw_stops_val, (bytes, bytearray, memoryview)):
            stops_text = zlib.decompress(bytes(raw_stops_val)).decode("utf-8")
        else:
            stops_text = raw_stops_val or "[]"
        stops_raw = json.loads(stops_text)
        stops = [ScheduleStop(**s) for s in stops_raw]
        running_days = json.loads(row["running_days_json"] or "[]")
        classes = json.loads(row["classes_json"] or "[]")
        sched = TrainSchedule(
            train_number=row["train_number"],
            train_name=row["train_name"],
            source_station_code=row["source_station_code"],
            destination_station_code=row["destination_station_code"],
            running_days=running_days,
            classes=classes,
            total_duration_minutes=row["total_duration_minutes"],
            total_distance_km=row["total_distance_km"],
            stops=stops,
            provider=f"local_{row['source'] or 'timetable'}",
        )
        return sched, str(row["source"] or "datameet")

    def get_schedule(
        self,
        train_number: str,
        *,
        require_fresh_source: bool = False,
    ) -> TrainSchedule | None:
        """Look up a train schedule locally. Returns None on miss."""
        tno = str(train_number).strip()
        if not tno:
            return None
        conns = self._read_connections()
        try:
            for conn in conns:
                row = conn.execute(
                    "SELECT * FROM trains WHERE train_number = ?",
                    (tno,),
                ).fetchone()
                if row is not None:
                    sched, src = self._row_to_schedule(row)
                    if require_fresh_source and src not in ("ntes", "railcore"):
                        continue
                    if sched.stops:
                        return sched
            return None
        finally:
            for conn in conns:
                conn.close()

    def get_trains_between(
        self,
        *,
        from_station: str,
        to_station: str,
        journey_date: str,
        quota: str | None = "GN",
        require_fresh_source: bool = False,
    ) -> RouteTrainsResult | None:
        """Discover candidate trains running from_station -> to_station on journey_date.

        Returns None when the local DB does not have reliable coverage for this
        pair (so caller can fall back to live RailCore).
        """
        frm = str(from_station).strip().upper()
        to = str(to_station).strip().upper()
        if not frm or not to or frm == to:
            return None

        try:
            jdate = date.fromisoformat(journey_date)
            query_weekday = _WEEKDAYS[jdate.weekday()]
        except ValueError:
            query_weekday = None

        conns = self._read_connections()
        if not conns:
            return None

        summaries_by_train: dict[str, RouteTrainSummary] = {}
        try:
            for conn in conns:
                rows = conn.execute(
                    """
                    SELECT
                        t.*,
                        a.seq AS from_seq,
                        a.day AS from_day,
                        a.departure_time AS from_dep,
                        a.distance_km AS from_dist,
                        b.seq AS to_seq,
                        b.day AS to_day,
                        b.arrival_time AS to_arr,
                        b.distance_km AS to_dist
                    FROM station_stops a
                    JOIN station_stops b
                        ON a.train_number = b.train_number AND a.seq < b.seq
                    JOIN trains t
                        ON t.train_number = a.train_number
                    WHERE a.station_code = ? AND b.station_code = ?
                    ORDER BY COALESCE(a.departure_time, '99:99'), t.train_number
                    """,
                    (frm, to),
                ).fetchall()

                for row in rows:
                    tno = row["train_number"]
                    if tno in summaries_by_train:
                        continue  # overlay takes precedence
                    src = str(row["source"] or "datameet")
                    if require_fresh_source and src not in ("ntes", "railcore"):
                        continue

                    running_days = [str(d).strip().upper() for d in json.loads(row["running_days_json"] or "[]")]
                    # Check weekday when running_days is known and non-empty.
                    # Note: running_days reflects origin departure day; adjust for
                    # train boarding day offset (from_day - 1) when from_day >= 1.
                    if query_weekday and running_days and len(running_days) < 7:
                        day_offset = max(0, (int(row["from_day"]) - 1) if row["from_day"] else 0)
                        origin_weekday = _WEEKDAYS[(date.fromisoformat(journey_date).weekday() - day_offset) % 7]
                        if origin_weekday not in running_days:
                            continue

                    classes = [str(c).strip().upper() for c in json.loads(row["classes_json"] or "[]") if c]
                    dep_time = row["from_dep"]
                    arr_time = row["to_arr"]
                    dur = _compute_duration_minutes(dep_time, row["from_day"], arr_time, row["to_day"])
                    seg_dist = (
                        round(float(row["to_dist"]) - float(row["from_dist"]), 2)
                        if row["from_dist"] is not None
                        and row["to_dist"] is not None
                        and float(row["to_dist"]) >= float(row["from_dist"])
                        else None
                    )
                    summaries_by_train[tno] = RouteTrainSummary(
                        train_number=tno,
                        train_name=row["train_name"],
                        from_station_code=frm,
                        to_station_code=to,
                        departure_time=dep_time,
                        arrival_time=arr_time,
                        duration_minutes=dur,
                        distance_km=seg_dist,
                        train_type=row["train_type"],
                        classes=classes,
                        running_days=running_days,
                    )
        finally:
            for conn in conns:
                conn.close()

        if not summaries_by_train:
            return None

        trains_sorted = sorted(
            summaries_by_train.values(),
            key=lambda t: (t.departure_time or "99:99", t.train_number),
        )
        return RouteTrainsResult(
            from_station_code=frm,
            to_station_code=to,
            journey_date=journey_date,
            quota=quota,
            matched_by="local_timetable",
            trains=trains_sorted,
            provider="local_timetable",
        )

    def upsert_schedule(self, schedule: TrainSchedule, *, source: str = "railcore") -> None:
        """Persist a live-fetched schedule into the writable local/overlay SQLite DB."""
        target = self._overlay_path if self._overlay_path is not None else self._db_path
        if target is None:
            return
        with self._lock:
            try:
                conn = self._connect_rw(target)
            except (sqlite3.Error, OSError) as exc:
                logger.debug("Skipping timetable write-through (%s): %s", target, exc)
                return
            try:
                with conn:
                    self.write_schedule(conn, schedule, source=source)
            except sqlite3.Error as exc:
                logger.debug("Timetable write-through failed: %s", exc)
            finally:
                conn.close()

    @staticmethod
    def write_schedule(
        conn: sqlite3.Connection,
        schedule: TrainSchedule,
        *,
        source: str = "ntes",
        train_type: str | None = None,
    ) -> None:
        """Write one TrainSchedule into an open SQLite connection."""
        tno = schedule.train_number.strip()
        stops_payload = [s.model_dump(exclude_none=True) for s in schedule.stops]
        stops_blob = zlib.compress(
            json.dumps(stops_payload, separators=(",", ":")).encode("utf-8"),
            level=6,
        )
        now_iso = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        conn.execute(
            """
            INSERT INTO trains (
                train_number, train_name, train_type,
                source_station_code, destination_station_code,
                running_days_json, classes_json,
                total_duration_minutes, total_distance_km,
                stops_json, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(train_number) DO UPDATE SET
                train_name = COALESCE(excluded.train_name, trains.train_name),
                train_type = COALESCE(excluded.train_type, trains.train_type),
                source_station_code = COALESCE(excluded.source_station_code, trains.source_station_code),
                destination_station_code = COALESCE(excluded.destination_station_code, trains.destination_station_code),
                running_days_json = CASE
                    WHEN excluded.running_days_json != '[]' THEN excluded.running_days_json
                    ELSE trains.running_days_json
                END,
                classes_json = CASE
                    WHEN excluded.classes_json != '[]' THEN excluded.classes_json
                    ELSE trains.classes_json
                END,
                total_duration_minutes = COALESCE(excluded.total_duration_minutes, trains.total_duration_minutes),
                total_distance_km = COALESCE(excluded.total_distance_km, trains.total_distance_km),
                stops_json = excluded.stops_json,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                tno,
                schedule.train_name,
                train_type,
                schedule.source_station_code,
                schedule.destination_station_code,
                json.dumps(schedule.running_days),
                json.dumps(schedule.classes),
                schedule.total_duration_minutes,
                schedule.total_distance_km,
                stops_blob,
                source,
                now_iso,
            ),
        )
        conn.execute("DELETE FROM station_stops WHERE train_number = ?", (tno,))
        for stop in schedule.stops:
            if stop.is_stop is False:
                continue
            code = stop.station_code.strip().upper()
            if not code:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO station_stops (
                    station_code, train_number, seq, day,
                    arrival_time, departure_time, distance_km
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    tno,
                    int(stop.sequence),
                    stop.day,
                    stop.arrival_time,
                    stop.departure_time,
                    stop.distance_km,
                ),
            )
            if stop.station_name:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO stations (code, name)
                    VALUES (?, ?)
                    """,
                    (code, stop.station_name),
                )


_DEFAULT_STORE: LocalTimetableStore | None = None
_STORE_LOCK = threading.Lock()


def get_local_timetable_store() -> LocalTimetableStore:
    global _DEFAULT_STORE
    with _STORE_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = LocalTimetableStore()
        return _DEFAULT_STORE
