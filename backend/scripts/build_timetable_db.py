"""Build and refresh the pan-India local timetable SQLite database (trains.db).

Stage 1 (optional): Download/import the open-source `datameet/railways`
dataset (`stations.json`, `trains.json`, `schedules.json`) as a pan-India
baseline (~5,000+ trains, `source="datameet"`).

Stage 2: Query live 2026 Indian Railways NTES via `ntes-client` across a
bidirectional pan-India corridor matrix (~76 station pairs covering trunk
routes and regional hubs), then fetch full per-stop schedules (`STA`, `STD`,
`Day`, `Distance`, `Halt`, `prsClassOfTravel`, `vStartDateList`) and upsert
them with `source="ntes"`.

Finally switches SQLite back to `DELETE` journal mode and runs `VACUUM` so
`backend/data/trains.db` is a compact single-file database ready for Git
and Vercel serverless deployment.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models.schedule import ScheduleStop, TrainSchedule  # noqa: E402
from app.services.local_timetable_store import (  # noqa: E402
    DEFAULT_DB_PATH,
    LocalTimetableStore,
)

DATAMEET_URLS = {
    "stations.json": "https://raw.githubusercontent.com/datameet/railways/master/stations.json",
    "trains.json": "https://raw.githubusercontent.com/datameet/railways/master/trains.json",
    "schedules.json": "https://raw.githubusercontent.com/datameet/railways/master/schedules.json",
}

WEEKDAY_ORDER = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

# Bidirectional pan-India corridors covering major metros, state capitals,
# trunk lines, and requested verification routes (PUNE-CSMT, SBC-MAS,
# NDLS-CSMT, MYS-BGM).
PAN_INDIA_CORRIDORS: list[tuple[str, str]] = [
    # Requested & West-South core
    ("PUNE", "CSMT"),
    ("CSMT", "PUNE"),
    ("SBC", "MAS"),
    ("MAS", "SBC"),
    ("NDLS", "CSMT"),
    ("CSMT", "NDLS"),
    ("MYS", "BGM"),
    ("BGM", "MYS"),
    # Mumbai / Pune / Maharashtra / Konkan / Gujarat
    ("PUNE", "LTT"),
    ("LTT", "PUNE"),
    ("PUNE", "MMCT"),
    ("MMCT", "PUNE"),
    ("PUNE", "KYN"),
    ("KYN", "PUNE"),
    ("PUNE", "SUR"),
    ("SUR", "PUNE"),
    ("PUNE", "NGP"),
    ("NGP", "PUNE"),
    ("PUNE", "ADI"),
    ("ADI", "PUNE"),
    ("CSMT", "MAO"),
    ("MAO", "CSMT"),
    ("CSMT", "NGP"),
    ("NGP", "CSMT"),
    ("CSMT", "BPL"),
    ("BPL", "CSMT"),
    ("MMCT", "ADI"),
    ("ADI", "MMCT"),
    ("MMCT", "BRC"),
    ("BRC", "MMCT"),
    ("MMCT", "ST"),
    ("ST", "MMCT"),
    ("MMCT", "NDLS"),
    ("NDLS", "MMCT"),
    # Karnataka / South / Kerala / Andhra / Telangana
    ("MYS", "SBC"),
    ("SBC", "MYS"),
    ("MYS", "DWR"),
    ("DWR", "MYS"),
    ("SBC", "UBL"),
    ("UBL", "SBC"),
    ("SBC", "BGM"),
    ("BGM", "SBC"),
    ("SBC", "DWR"),
    ("DWR", "SBC"),
    ("SBC", "PUNE"),
    ("PUNE", "SBC"),
    ("SBC", "CSMT"),
    ("CSMT", "SBC"),
    ("SBC", "SC"),
    ("SC", "SBC"),
    ("SBC", "ERS"),
    ("ERS", "SBC"),
    ("SBC", "NDLS"),
    ("NDLS", "SBC"),
    ("SBC", "HWH"),
    ("HWH", "SBC"),
    ("YPR", "MAS"),
    ("MAS", "YPR"),
    ("MAS", "ERS"),
    ("ERS", "MAS"),
    ("MAS", "TVC"),
    ("TVC", "MAS"),
    ("MAS", "CBE"),
    ("CBE", "MAS"),
    ("MAS", "BZA"),
    ("BZA", "MAS"),
    ("MAS", "HWH"),
    ("HWH", "MAS"),
    ("MAS", "NDLS"),
    ("NDLS", "MAS"),
    ("SC", "CSMT"),
    ("CSMT", "SC"),
    ("SC", "PUNE"),
    ("PUNE", "SC"),
    ("SC", "NDLS"),
    ("NDLS", "SC"),
    ("SC", "VSKP"),
    ("VSKP", "SC"),
    ("BZA", "VSKP"),
    ("VSKP", "BZA"),
    ("PUNE", "UBL"),
    ("UBL", "PUNE"),
    ("ASK", "BGM"),
    ("BGM", "ASK"),
    ("KLBG", "CSMT"),
    ("CSMT", "KLBG"),
    # North / East / Central Trunk
    ("NDLS", "HWH"),
    ("HWH", "NDLS"),
    ("NDLS", "PNBE"),
    ("PNBE", "NDLS"),
    ("NDLS", "LKO"),
    ("LKO", "NDLS"),
    ("NDLS", "CNB"),
    ("CNB", "NDLS"),
    ("NDLS", "JP"),
    ("JP", "NDLS"),
    ("NDLS", "BPL"),
    ("BPL", "NDLS"),
    ("NDLS", "ADI"),
    ("ADI", "NDLS"),
    ("NDLS", "BRC"),
    ("BRC", "NDLS"),
    ("BPL", "PUNE"),
    ("PUNE", "BPL"),
    ("NGP", "HWH"),
    ("HWH", "NGP"),
    ("JHS", "CSMT"),
    ("CSMT", "JHS"),
]


def _download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    print(f"Downloading {url} -> {dest} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "RailSaathiAI-TimetableBuilder/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fh:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    print(f"  Saved {dest.name} ({dest.stat().st_size / (1024 * 1024):.2f} MB)")


def _norm_hhmm(raw: object) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"none", "null", "na", "n/a", "-", "--", "source", "src", "destination", "dstn"}:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def _hhmm_to_sec(hhmm: str | None) -> int:
    if not hhmm:
        return 0
    try:
        parts = hhmm.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + (int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return 0


def _parse_int(val: object) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    s = str(val).strip()
    if not s or s.lower() in {"none", "null", "-", "--"}:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _parse_running_days(payload: dict) -> list[str]:
    found: set[str] = set()
    dates = payload.get("vStartDateList") or []
    if isinstance(dates, list):
        for item in dates:
            if not isinstance(item, str):
                continue
            try:
                dt = datetime.strptime(item.strip(), "%d-%b-%Y")
                found.add(WEEKDAY_ORDER[dt.weekday()])
            except ValueError:
                continue
    if found:
        return [d for d in WEEKDAY_ORDER if d in found]

    dor = payload.get("DaysOfRun")
    if isinstance(dor, str):
        upper = dor.upper()
        if "DAILY" in upper or "ALL" in upper:
            return list(WEEKDAY_ORDER)
        for day in WEEKDAY_ORDER:
            if day in upper:
                found.add(day)
        if found:
            return [d for d in WEEKDAY_ORDER if d in found]
    return []


def _parse_classes(payload: dict) -> list[str]:
    raw = payload.get("prsClassOfTravel") or payload.get("ClassOfTravel") or ""
    if isinstance(raw, list):
        tokens = [str(x).strip().upper() for x in raw]
    else:
        tokens = [t.strip().upper() for t in re.split(r"[,;\s]+", str(raw))]
    seen: list[str] = []
    for tok in tokens:
        if not tok or tok in {"NONE", "NULL", "N/A", "-"}:
            continue
        if tok not in seen:
            seen.append(tok)
    return seen


def _parse_travel_time_minutes(raw: object) -> int | None:
    if not raw:
        return None
    m = re.match(r"^\s*(\d+)\s*:\s*(\d{1,2})\s*$", str(raw))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _datameet_classes(props: dict) -> list[str]:
    mapping = (
        ("first_ac", "1A"),
        ("second_ac", "2A"),
        ("third_ac", "3A"),
        ("chair_car", "CC"),
        ("sleeper", "SL"),
        ("first_class", "FC"),
    )
    out: list[str] = []
    for key, code in mapping:
        val = props.get(key)
        if val in (1, True, "1", "true", "True"):
            out.append(code)
    return out


def import_datameet(conn: sqlite3.Connection, cache_dir: Path) -> int:
    for fname, url in DATAMEET_URLS.items():
        _download_file(url, cache_dir / fname)

    print("Loading DataMeet stations.json ...")
    stations_geo = json.loads((cache_dir / "stations.json").read_text(encoding="utf-8"))
    station_rows: list[tuple[str, str]] = []
    for feat in stations_geo.get("features") or []:
        props = feat.get("properties") or {}
        code = str(props.get("code") or "").strip().upper()
        name = str(props.get("name") or "").strip()
        if code and name:
            station_rows.append((code, name))

    print(f"Loading DataMeet trains.json ({len(station_rows)} stations) ...")
    trains_geo = json.loads((cache_dir / "trains.json").read_text(encoding="utf-8"))
    train_meta: dict[str, dict] = {}
    for feat in trains_geo.get("features") or []:
        props = feat.get("properties") or {}
        tno = str(props.get("number") or "").strip()
        if tno:
            train_meta[tno] = props

    print(f"Loading DataMeet schedules.json ({len(train_meta)} trains) ...")
    raw_stops = json.loads((cache_dir / "schedules.json").read_text(encoding="utf-8"))
    by_train: dict[str, list[dict]] = defaultdict(list)
    for row in raw_stops:
        tno = str(row.get("train_number") or "").strip()
        if tno:
            by_train[tno].append(row)

    print(f"Importing {len(by_train)} DataMeet trains into SQLite ...")
    imported = 0
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO stations (code, name) VALUES (?, ?)",
            station_rows,
        )
        for tno, rows in by_train.items():
            rows_sorted = sorted(
                rows,
                key=lambda r: (
                    _parse_int(r.get("day")) or 1,
                    _hhmm_to_sec(_norm_hhmm(r.get("arrival")) or _norm_hhmm(r.get("departure"))),
                    _parse_int(r.get("id")) or 0,
                ),
            )
            if len(rows_sorted) < 2:
                continue
            meta = train_meta.get(tno) or {}
            train_name = str(meta.get("name") or rows_sorted[0].get("train_name") or tno).strip()
            src_code = str(meta.get("from_station_code") or rows_sorted[0].get("station_code") or "").strip().upper()
            dst_code = str(meta.get("to_station_code") or rows_sorted[-1].get("station_code") or "").strip().upper()
            dur_h = _parse_int(meta.get("duration_h"))
            dur_m = _parse_int(meta.get("duration_m"))
            total_dur = (dur_h or 0) * 60 + (dur_m or 0) if (dur_h is not None or dur_m is not None) else None
            total_dist = _parse_int(meta.get("distance"))
            if total_dist is not None and total_dist <= 0:
                total_dist = None

            stops: list[ScheduleStop] = []
            for idx, r in enumerate(rows_sorted, start=1):
                scode = str(r.get("station_code") or "").strip().upper()
                if not scode:
                    continue
                sname = str(r.get("station_name") or "").strip() or None
                arr = _norm_hhmm(r.get("arrival"))
                dep = _norm_hhmm(r.get("departure"))
                day = _parse_int(r.get("day")) or 1
                if idx == 1:
                    stype = "SOURCE"
                    dist_km = 0
                elif idx == len(rows_sorted):
                    stype = "DEST"
                    dist_km = total_dist
                else:
                    stype = "ENROUTE"
                    dist_km = None
                stops.append(
                    ScheduleStop(
                        sequence=idx,
                        station_code=scode,
                        station_name=sname,
                        stop_type=stype,
                        arrival_time=arr,
                        departure_time=dep,
                        halt_minutes=None,
                        day=day,
                        distance_km=dist_km,
                        is_stop=True,
                    )
                )
            if len(stops) < 2:
                continue
            sched = TrainSchedule(
                train_number=tno,
                train_name=train_name,
                source_station_code=src_code or stops[0].station_code,
                destination_station_code=dst_code or stops[-1].station_code,
                running_days=[],
                classes=_datameet_classes(meta),
                total_duration_minutes=total_dur,
                total_distance_km=total_dist,
                stops=stops,
                provider="datameet",
            )
            LocalTimetableStore.write_schedule(
                conn,
                sched,
                source="datameet",
                train_type=(str(meta.get("type")).strip() if meta.get("type") else None),
            )
            imported += 1
    print(f"Imported {imported} DataMeet trains.")
    return imported


def _ntes_payload_to_schedule(train_number: str, payload: dict) -> tuple[TrainSchedule, str | None] | None:
    raw_stations = payload.get("stations") or []
    if not isinstance(raw_stations, list) or len(raw_stations) < 2:
        return None
    raw_stations_sorted = sorted(raw_stations, key=lambda s: _parse_int(s.get("Sr")) or 0)
    stops: list[ScheduleStop] = []
    for idx, st in enumerate(raw_stations_sorted, start=1):
        code = str(st.get("StationCode") or "").strip().upper()
        if not code:
            continue
        name = str(st.get("StationName") or "").strip() or None
        arr = _norm_hhmm(st.get("STA"))
        dep = _norm_hhmm(st.get("STD"))
        halt = _parse_int(st.get("Halt"))
        day = _parse_int(st.get("Day")) or 1
        dist = _parse_int(st.get("Distance"))
        if idx == 1:
            stype = "SOURCE"
        elif idx == len(raw_stations_sorted):
            stype = "DEST"
        else:
            stype = "ENROUTE"
        stops.append(
            ScheduleStop(
                sequence=idx,
                station_code=code,
                station_name=name,
                stop_type=stype,
                arrival_time=arr,
                departure_time=dep,
                halt_minutes=halt if halt and halt > 0 else None,
                day=day,
                distance_km=dist,
                is_stop=True,
            )
        )
    if len(stops) < 2:
        return None

    src = str(payload.get("Source") or stops[0].station_code).strip().upper()
    dst = str(payload.get("Destination") or stops[-1].station_code).strip().upper()
    tname = str(payload.get("TrainName") or train_number).strip()
    total_dist = stops[-1].distance_km
    if total_dist is None:
        dists = [s.distance_km for s in stops if s.distance_km is not None]
        total_dist = max(dists) if dists else None
    total_dur = _parse_travel_time_minutes(payload.get("TravelTime"))
    ttype = str(payload.get("TrainType") or "").strip() or None

    sched = TrainSchedule(
        train_number=str(payload.get("TrainNumber") or train_number).strip(),
        train_name=tname,
        source_station_code=src,
        destination_station_code=dst,
        running_days=_parse_running_days(payload),
        classes=_parse_classes(payload),
        total_duration_minutes=total_dur,
        total_distance_km=total_dist,
        stops=stops,
        provider="ntes",
    )
    return sched, ttype


def import_ntes(
    conn: sqlite3.Connection,
    *,
    workers: int = 6,
    max_trains: int | None = None,
) -> tuple[int, int]:
    from ntes import NTESClient

    thread_local = threading.local()

    def _get_client() -> NTESClient:
        cli = getattr(thread_local, "client", None)
        if cli is None:
            cli = NTESClient()
            thread_local.client = cli
        return cli

    print(f"Discovering trains on {len(PAN_INDIA_CORRIDORS)} pan-India NTES corridors ...")
    discovered_trains: dict[str, None] = {}

    def _discover_pair(pair: tuple[str, str]) -> list[str]:
        frm, to = pair
        for attempt in range(2):
            try:
                res = _get_client().trains_between(frm, to) or {}
                trains = res.get("Trains") or []
                return [
                    str(t.get("TrainNumber") or t.get("TrainNo") or "").strip()
                    for t in trains
                    if (t.get("TrainNumber") or t.get("TrainNo"))
                ]
            except Exception as exc:
                if attempt == 0:
                    time.sleep(0.4)
                else:
                    print(f"  WARN trains_between({frm}->{to}) failed: {exc}")
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as pool:
        for tnos in pool.map(_discover_pair, PAN_INDIA_CORRIDORS):
            for tno in tnos:
                if tno and tno not in discovered_trains:
                    discovered_trains[tno] = None

    train_list = list(discovered_trains.keys())
    if max_trains is not None and max_trains > 0:
        train_list = train_list[:max_trains]
    print(f"Discovered {len(discovered_trains)} unique trains across corridors; fetching {len(train_list)} schedules ...")

    def _fetch_schedule(tno: str) -> tuple[str, tuple[TrainSchedule, str | None] | None]:
        for attempt in range(2):
            try:
                time.sleep(0.05)
                payload = _get_client().schedule(tno)
                if isinstance(payload, dict):
                    parsed = _ntes_payload_to_schedule(tno, payload)
                    if parsed is not None:
                        return tno, parsed
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
        return tno, None

    ok = 0
    failed = 0
    batch: list[tuple[TrainSchedule, str | None]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for idx, (_tno, parsed) in enumerate(pool.map(_fetch_schedule, train_list), start=1):
            if parsed is None:
                failed += 1
            else:
                ok += 1
                batch.append(parsed)
            if len(batch) >= 100:
                with conn:
                    for sched, ttype in batch:
                        LocalTimetableStore.write_schedule(conn, sched, source="ntes", train_type=ttype)
                batch.clear()
            if idx % 100 == 0 or idx == len(train_list):
                print(f"  NTES progress: {idx}/{len(train_list)} (ok={ok}, failed={failed})")

    if batch:
        with conn:
            for sched, ttype in batch:
                LocalTimetableStore.write_schedule(conn, sched, source="ntes", train_type=ttype)
    return ok, failed


def print_stats_and_verify(db_path: Path) -> None:
    store = LocalTimetableStore(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        total_trains = conn.execute("SELECT COUNT(*) FROM trains").fetchone()[0]
        by_source = conn.execute("SELECT source, COUNT(*) FROM trains GROUP BY source").fetchall()
        total_stations = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
        total_stops = conn.execute("SELECT COUNT(*) FROM station_stops").fetchone()[0]
    finally:
        conn.close()
    size_mb = db_path.stat().st_size / (1024 * 1024)
    print("\n=== Timetable DB Summary ===")
    print(f"Path: {db_path}")
    print(f"Size: {size_mb:.2f} MB")
    print(f"Total trains: {total_trains} ({dict(by_source)})")
    print(f"Stations: {total_stations}, Stop rows: {total_stops}")

    sample_routes = [
        ("PUNE", "CSMT", "2026-12-01"),
        ("SBC", "MAS", "2026-12-01"),
        ("NDLS", "CSMT", "2026-12-01"),
        ("MYS", "BGM", "2026-12-01"),
    ]
    print("\n=== Corridor Verification (LocalTimetableStore) ===")
    for frm, to, jdate in sample_routes:
        res_fresh = store.get_trains_between(
            from_station=frm, to_station=to, journey_date=jdate, require_fresh_source=True
        )
        res_all = store.get_trains_between(
            from_station=frm, to_station=to, journey_date=jdate, require_fresh_source=False
        )
        fresh_cnt = len(res_fresh.trains) if res_fresh else 0
        all_cnt = len(res_all.trains) if res_all else 0
        sample = res_fresh.trains[0] if (res_fresh and res_fresh.trains) else (res_all.trains[0] if (res_all and res_all.trains) else None)
        sample_desc = (
            f"sample={sample.train_number} {sample.train_name} "
            f"dep={sample.departure_time} arr={sample.arrival_time} dist={sample.distance_km}km classes={sample.classes}"
            if sample
            else "none"
        )
        print(f"  {frm}->{to} ({jdate}): fresh={fresh_cnt}, all={all_cnt} | {sample_desc}")


def compress_existing_stops_json(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT train_number, stops_json FROM trains WHERE typeof(stops_json) = 'text'"
    ).fetchall()
    if not rows:
        return 0
    with conn:
        for tno, raw_text in rows:
            blob = zlib.compress((raw_text or "[]").encode("utf-8"), level=6)
            conn.execute(
                "UPDATE trains SET stops_json = ? WHERE train_number = ?",
                (blob, tno),
            )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build pan-India SQLite timetable DB (trains.db).")
    parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "railsaathi_datameet",
    )
    parser.add_argument("--skip-datameet", action="store_true")
    parser.add_argument("--skip-ntes", action="store_true")
    parser.add_argument("--compact-only", action="store_true", help="Compress stops_json and VACUUM existing DB.")
    parser.add_argument("--ntes-workers", type=int, default=6)
    parser.add_argument("--max-ntes", type=int, default=None)
    parser.add_argument("--fresh", action="store_true", help="Delete existing DB before building.")
    args = parser.parse_args()

    out_path: Path = args.output
    if args.fresh and not args.compact_only and out_path.exists():
        out_path.unlink()
        for ext in ("-wal", "-shm"):
            side = Path(f"{out_path}{ext}")
            if side.exists():
                side.unlink()

    store = LocalTimetableStore(out_path)
    conn = store._connect_rw(out_path)
    try:
        if not args.compact_only:
            if not args.skip_datameet:
                import_datameet(conn, args.cache_dir)
            if not args.skip_ntes:
                import_ntes(conn, workers=args.ntes_workers, max_trains=args.max_ntes)

        compressed = compress_existing_stops_json(conn)
        if compressed:
            print(f"Compressed stops_json for {compressed} trains.")
        print("Compacting SQLite DB (journal_mode=DELETE + VACUUM) ...")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("VACUUM")
    finally:
        conn.close()

    print_stats_and_verify(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
