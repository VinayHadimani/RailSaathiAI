# RailRoute AI — Backend (Phase 4: Mixed Journey Combination Engine)

The working backend of RailRoute AI, now in four stages:

**Phase 1 (feasibility, kept intact):** proves against the **real RailCore
API** that schedules and arbitrary station-to-station availability exist,
including two independent segments of the **same** train.

**Phase 2 (kept intact):** the **Same-Train Segment Combination Search
Engine**. Given source, destination, date, class and quota it finds trains
whose complete journey can be covered by **multiple consecutive segments on
the same train**, with **every segment explicitly confirmed available**.

**Phase 3 (kept intact):** the **Train-Change Connection Engine**. Finds
complete journeys that require **changing trains at intermediate stations**
— any number of train changes — where **every segment explicitly reports
confirmed availability** (provider status AVAILABLE), transfers are
validated against real stop sequences, and unverified journeys are reported
separately and never labelled confirmed.

**Phase 4 (this phase):** the **Mixed Journey Combination Engine** — an
orchestration layer that reuses the phase 3 engine's discovery, transfer
validation and budget machinery, extends the chronological search with
**same-train seat-change continuations** (phase 2 semantics), and returns
journeys that combine **any number of seat changes and train changes**,
where **every individual ticket segment explicitly reports confirmed
availability**.

**The service never books tickets.** Availability results are point-in-time
checks, not bookings. Multi-ticket journeys require verification of Indian
Railways ticketing and connection rules before relying on them.

---

## Stack

- Python 3.12, FastAPI, Pydantic v2, httpx (sync client), pytest
- No database yet; PostgreSQL/Alembic arrive with the core backend phase

## Folder structure

```
backend/
├── app/
│   ├── main.py                     FastAPI entry point (Swagger at /docs)
│   ├── api/
│   │   ├── deps.py                 RailCoreClient wiring (test override point)
│   │   └── routes.py               /api/v1 routes
│   ├── core/
│   │   ├── config.py               env/.env settings (SecretStr for the key)
│   │   └── logging.py               JSON logs + secret masking
│   ├── models/                     internal domain models
│   │   ├── schedule.py             TrainSchedule, ScheduleStop (overnight day)
│   │   ├── availability.py         SegmentAvailability, ClassAvailability
│   │   └── route_search.py         RouteTrainsResult (train discovery)
│   ├── providers/
│   │   ├── base.py                 provider errors + internal statuses
│   │   ├── railcore.py             RailCoreClient (retries, limits, credits)
│   │   └── normalizer.py           RailCore JSON -> internal models
│   ├── schemas/
│   │   ├── segment_test.py         phase 1 request/response schemas
│   │   ├── journey_search.py       phase 2 request/response schemas
│   │   ├── train_change_search.py  phase 3 request/response schemas
│   │   └── mixed_search.py         phase 4 request/response schemas
│   └── services/
│       ├── segment_availability_service.py   phase 1 test workflow
│       ├── search_budgets.py                 SearchBudgets + TrainChangeBudgets + MixedJourneyBudgets
│       ├── segment_query_planner.py          dedup + batching + budgets + caches
│       ├── same_train_combination_service.py phase 2 engine (untouched)
│       ├── train_change_connection_service.py phase 3 engine (untouched)
│       └── mixed_journey_service.py          phase 4 engine (subclass of phase 3)
├── tests/                          169 pytest tests + 2 opt-in live tests
├── .env                            local secrets (git-ignored, NEVER commit)
├── .env.example                    template
├── .gitignore
├── pytest.ini                      (marks: live_api for opt-in live tests)
├── requirements.txt
└── README.md
```

## Install and run locally

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure credentials (never commit .env):
cp .env.example .env
# edit .env and set RAILCORE_API_KEY=rk_live_...

uvicorn app.main:app --reload
```

- **Swagger UI:** http://127.0.0.1:8000/docs
- Health: `GET http://127.0.0.1:8000/api/v1/health`
- Config status (booleans only, never the key): `GET /api/v1/config/status`

If `RAILCORE_API_KEY` is missing, every provider call returns HTTP 503 with
instructions instead of failing silently.

## Phase 4: the mixed journey search

`POST /api/v1/journeys/mixed/search`

```json
{
  "from_station": "MYS",
  "to_station": "DWR",
  "journey_date": "2026-11-27",
  "travel_class": "3A",
  "quota": "GN",
  "ranking_preference": "fastest",
  "via_stations": [],
  "min_transfer_buffer_minutes": null,
  "max_transfer_wait_minutes": null
}
```

Only the first five fields are required. `ranking_preference` is optional
(`CHEAPEST`, `FASTEST`, `FEWEST_TRAIN_CHANGES` or `FEWEST_SEAT_CHANGES`,
default `FASTEST`; ties break deterministically). `via_stations` (max 3)
hints interchanges the corridor cannot reveal; the transfer-window
overrides must satisfy `wait >= buffer`. Validation is inherited from the
phase 3 request schema (station codes, class, quota, booking window).

### What a mixed journey is

A chronological sequence of **individual ticket segments**:

- consecutive segments on the **same train run** form a seat-change group
  (the passenger stays onboard through the halt while the ticket boundary
  changes) — phase 2 semantics;
- a transition to a **different train** is a validated transfer — phase 3
  semantics;
- a single-segment source → destination journey is a **direct train**,
  excluded from results and listed in `direct_trains` without any
  availability claim.

### How the engine works

`MixedJourneyCombinationService` **subclasses the phase 3 engine** and
reuses its machinery verbatim (no engine was rewritten, no API contract
changed):

1. **Seed/corridor/lazy discovery, schedule pool, class filtering,
   budget-aware deduplicated batched availability queries** — inherited
   from phase 3, including the shared `SegmentQueryPlanner` and the
   process-wide schedule/discovery caches.
2. **First legs** depart the source on the journey date (direct legs to
   the destination are excluded — that is a direct train, not a
   combination).
3. **Chronological earliest-arrival expansion** — identical to phase 3,
   plus ONE new edge type: after alighting at station X on train T (run
   R), the passenger may keep riding **the same run R** onward from X —
   a **same-train seat change**. The continuation is anchored to the
   exact run (the previous leg's real arrival datetime identifies the
   run's origin departure date, so overnight day offsets stay correct and
   the continuation always belongs to the run being ridden). Boarding a
   *different* run of the same train number is not a continuation and is
   excluded.
4. **Availability** is checked per individual segment (continuations
   included) with the per-segment IRCTC boarding date. Only explicit
   `AVAILABLE` extends the search; a provider error is never treated as
   unavailability (unverified candidates are reported separately).
5. **Assembly + ranking** with explicit typing: every segment carries
   `segment_type` (`FIRST_SEGMENT` / `SAME_TRAIN_SEGMENT` /
   `TRAIN_CHANGE`), every gap carries a transition block
   (`SAME_TRAIN_SEAT_CHANGE` — is_train_change false, no buffer/wait
   claims, onboard halt reported — or `TRAIN_CHANGE` — a genuine transfer
   block with wait and buffer compliance). **A seat change is never
   labelled a train transfer.**

### The search algorithm and its limitations

Same time-dependent earliest-arrival search as phase 3, with the addition
of same-run continuation edges. Consequences worth stating explicitly:

- **No fixed maximum** of seat changes or train changes: depth is bounded
  only by cycle prevention, the horizon and *reported* budgets.
- **Cycle prevention**: no station is ever revisited (inherited).
  Journeys that would legitimately revisit a station (rare circular
  services ridden a full lap) are out of scope.
- **Duplicate journey detection**: destination-reaching paths are recorded
  once per physical segment signature (train, stop sequences, boarding
  dates); provider-level duplicate train entries are deduplicated at
  discovery, in the pool and in `direct_trains`.
- **No dominance pruning by design** (inherited): merging states that
  share (station, arrival time) would discard valid alternative journeys
  whose earlier segments differ; explosion is bounded by the reported
  budgets instead.
- **Documented limitation**: journeys that alight from train T and later
  *re-board a different run of the same train number* T are not
  generated (the engine only continues the run it is currently riding);
  such journeys are extremely rare in practice.
- As with phase 3, arrival events at the destination are recorded, not
  expanded; lazy discovery can miss branch stations no fetched schedule
  reveals (use `via_stations` hints).

### Journey construction and validation rules

For every same-train group (consecutive segments on one train run): same
train number and run (anchored by the actual arrival datetime), correct
forward station stop sequence, boarding date and overnight day offsets
(IRCTC semantics — the date the train departs the boarding station), and
every individual segment explicitly confirmed available.

For every train change (inherited phase 3 rules): both trains actually
stop at the interchange (exact same station code), arrival precedes
departure, wait ≥ minimum transfer buffer (default 30 min, env
`MIXED_MIN_TRANSFER_BUFFER_MINUTES`, per-request override), wait ≤
maximum transfer wait (default 720 min, env
`MIXED_MAX_TRANSFER_WAIT_MINUTES`, per-request override), valid station
order, correct calendar dates/overnight handling, arrival within the
search horizon (default journey date + 2 days, env
`MIXED_SEARCH_HORIZON_DAYS`). A completed journey is additionally
re-validated defensively (endpoints, continuity, windows, no revisits)
before it is returned.

### Availability verification rules

Identical to phases 2/3 — the rules are enforced once, in the shared
planner: only an explicit provider `AVAILABLE` extends the search or
appears in `results`. `WAITLIST`, `RAC`, `REGRET`, `NOT_AVAILABLE`,
`TRAIN_CANCELLED`, `TRAIN_DEPARTED`, `UNKNOWN`, class-not-offered,
failed batch items and provider errors never qualify; `PROVIDER_ERROR` /
`UNKNOWN` legs keep the journey alive and surface as
`unverified_candidates` with `journey_status: PARTIALLY_VERIFIED`,
`unverified_segment_numbers` and per-segment error payloads — never as
confirmed results. Availability is deduplicated per search, batched,
budget-enforced before requests, and never cached across searches;
timestamps (`checked_at`, provider `last_updated_at`) are preserved.

### Search-budget behavior

Budgets are computational safeguards, **not** change limits. Defaults (env
`MIXED_*`, see `.env.example`): 20 discovery pairs, 15 train schedules,
40 API requests, 120 credits, 30 s, 300 expanded states, 25 journeys,
20 results, horizon 2 days. When any limit stops the search early the
response reports `incomplete_reasons` (same vocabulary as phase 3) and
the status becomes `SEARCH_LIMIT_REACHED` when nothing fully confirmed
was found — the engine never claims an exhaustive search it did not
perform.

### Ranking

Every journey carries total fare (never estimated — unknown fares are
`null`/absent and listed in `segments_missing_fare`), door-to-door
duration, total transfer waiting time (train-change waits only; seat-change
halts do not count), segment count, train-change count, seat-change
count, and the distinct trains used. The four preferences order by
(fare → duration → changes → seats), (duration → fare → …),
(train changes → …) and (seat changes → …) respectively, each with
deterministic tie-breaks (journey id last).

### Sample response (abridged, synthetic network)

```json
{
  "success": true,
  "status": "AVAILABLE_COMBINATION",
  "ranked_by": "FASTEST",
  "total_results": 8,
  "results": [
    {
      "journey_id": "…-99701@MYS-99701@KRNR-99701@ASK-99702@SBC-DWR",
      "journey_type": "MIXED",
      "journey_status": "AVAILABLE_COMBINATION",
      "segment_count": 4,
      "train_changes": 1,
      "seat_changes": 2,
      "distinct_trains": ["99701", "99702"],
      "total_fare": 280.0,
      "total_duration_minutes": 630,
      "total_transfer_wait_minutes": 65,
      "segments": [
        {
          "segment_number": 1,
          "segment_type": "FIRST_SEGMENT",
          "train_number": "99701",
          "from_station": {"code": "MYS", "name": "Mysuru Junction", "sequence": 1},
          "to_station": {"code": "KRNR", "name": "Krishnarajanagar", "sequence": 2},
          "departure_time": "08:00",
          "arrival_time": "09:10",
          "boarding_query_date": "2026-11-25",
          "status": "CONFIRMED_AVAILABLE",
          "fare": 60.0,
          "checked_at": "…"
        },
        {
          "segment_number": 2,
          "segment_type": "SAME_TRAIN_SEGMENT",
          "train_number": "99701",
          "from_station": {"code": "KRNR"},
          "to_station": {"code": "ASK"},
          "status": "CONFIRMED_AVAILABLE",
          "fare": 55.0
        },
        { "segment_number": 3, "segment_type": "SAME_TRAIN_SEGMENT", "…": "…" },
        { "segment_number": 4, "segment_type": "TRAIN_CHANGE", "train_number": "99702", "…": "…" }
      ],
      "transitions": [
        {
          "transition_type": "SAME_TRAIN_SEAT_CHANGE",
          "is_train_change": false,
          "station": {"code": "KRNR"},
          "onboard_halt_minutes": 5
        },
        { "transition_type": "SAME_TRAIN_SEAT_CHANGE", "station": {"code": "ASK"} },
        {
          "transition_type": "TRAIN_CHANGE",
          "is_train_change": true,
          "station": {"code": "SBC"},
          "from_train": "99701",
          "to_train": "99702",
          "wait_minutes": 65,
          "within_min_buffer": true,
          "within_max_wait": true
        }
      ]
    }
  ],
  "unverified_candidates": [],
  "direct_trains": [{"train_number": "99701", "availability_checked": false}],
  "search_completeness": {"complete": true, "incomplete_reasons": []},
  "notes": ["…"]
}
```

## Phase 3: the train-change connection search

`POST /api/v1/journeys/train-change/search`

```json
{
  "from_station": "MYS",
  "to_station": "DWR",
  "journey_date": "2026-11-27",
  "travel_class": "3A",
  "quota": "GN",
  "ranking_preference": "fastest",
  "via_stations": [],
  "min_transfer_buffer_minutes": null,
  "max_transfer_wait_minutes": null
}
```

Only the first five fields are required. `ranking_preference` is optional
(`CHEAPEST`, `FASTEST` or `FEWEST_CHANGES`, default `FEWEST_CHANGES`;
ties break deterministically by fare → duration → changes → journey id).
`via_stations` (max 3) hints interchanges the corridor cannot reveal;
the two transfer-window overrides must satisfy `wait >= buffer`.

### How the engine works

1. **Seed discovery** — `GET /routes/trains?from=S&to=D&date&quota`
   (1 credit). Through trains become the **direct-train reference list**
   (availability *not* checked — a direct train is not a train-change
   journey) and the first source of corridor knowledge.
2. **Corridor learning** — schedules are fetched for candidates
   (deduplicated, class-filtered, cached process-wide). Every fetched
   schedule teaches the engine which stations exist; **discovery pairs
   are only ever built from stations that appear in fetched schedules** —
   never by blindly enumerating station pairs. `404 NO_TRAINS_FOUND` is a
   free, honest dead end, never an error. Stations that lie beyond the
   destination on every known train are not queried as pair targets.
3. **First-leg pairs** — `(S, Y)` discovery for known stations
   Y, nearest-corridor-first, budget-capped, iterating while new stations
   are learned. Optional `via_stations` anchor this phase when no through
   train exists at all.
4. **Chronological BFS** (the search algorithm — see below) — arrival
   events are expanded in arrival-time order toward the destination.
5. **Availability verification** — every candidate leg is checked through
   the shared `SegmentQueryPlanner` (batched, deduplicated per
   (train, from, to, date, class, quota), budget-enforced **before** any
   request, cached schedules). Only explicit `AVAILABLE` extends the
   search. `WAITLIST` / `RAC` / `NOT AVAILABLE` are definitive dead
   branches. `PROVIDER_ERROR` / `UNKNOWN` legs remain traversable but any
   journey through them is reported as an **unverified candidate**
   (`journey_status: PARTIALLY_VERIFIED`), never as a confirmed result —
   a provider error is never treated as an unavailable seat.
6. **Assembly + ranking** — per-segment availability/fare/timestamps,
   `TRAIN_CHANGE` transfer blocks, totals (fare never estimated,
   door-to-door duration, changes, wait sum), ranked by the preference.

### The search algorithm

A **time-dependent earliest-arrival search** over a schedule-derived leg
graph: a priority queue of arrival events `(station, absolute arrival
timestamp, path)` is popped in chronological order; expanding an arrival
generates schedule-valid legs departing within the transfer window and
pushes one new event per confirmed leg. This is the standard connection
scan / RAPTOR-style approach for timetable networks:

- **No fixed maximum number of train changes.** Depth is bounded only by
  the station set (cycle prevention), the horizon, and *reported*
  computational budgets — never by a change limit.
- **Cycle prevention**: a journey never revisits a station (visited-set);
  consecutive legs are always on different trains (an actual train
  change). Non-consecutive reuse of a train is allowed when schedule-valid.
- **No dominance pruning by design**: dropping "slower" arrivals at the
  same station would silently discard valid alternative journeys (later
  departures, different routes); the engine instead explores all
  schedule-valid paths within its budgets. Explosion is bounded by
  `max_expanded_states`, `max_journeys` and the request/credit/time
  budgets, each reported in `search_completeness` when hit.
- **Absolute timestamps everywhere** (IST): day offsets turn "arrives
  01:25" into a real calendar date, so overnight journeys and transfers
  across midnight are handled natively; per-segment boarding dates follow
  IRCTC semantics (the date the train leaves the boarding station).

### Transfer validation rules (every train change)

For each proposed change at interchange station X:

- the first train **actually stops** at X (`is_stop`), and so does the
  second — pass-through points are rejected before any availability query;
- both stations are the **exact same station code** — nearby stations are
  never assumed interchangeable;
- the second train departs **after** the first arrives;
- the wait ≥ the **minimum transfer buffer** (default 30 min, env
  `TRAIN_CHANGE_MIN_TRANSFER_BUFFER_MINUTES`, per-request override);
- the wait ≤ the **maximum transfer wait** (default 720 min, env
  `TRAIN_CHANGE_MAX_TRANSFER_WAIT_MINUTES`, per-request override);
- both segments follow valid **station order** on their trains;
- **journey dates and day offsets are correct**: each leg's run is derived
  from its boarding date minus the schedule day offset, validated against
  the train's **running days**; the per-segment availability query uses
  the boarding date (which advances for segments boarded after midnight);
- the journey arrives within the **search horizon** (default journey
  date + 2 days, env `TRAIN_CHANGE_SEARCH_HORIZON_DAYS`).

Impossible connections (buffer violations, non-stops, horizon, cycles,
same-train "changes") are rejected **during leg generation** — they never
cost an availability query.

### Availability verification rules

- Only an explicit provider `AVAILABLE` (→ `CONFIRMED_AVAILABLE`) ever
  extends the search or appears in `results`.
- Rejected: `WAITLIST`, `RAC`, `REGRET`, `NOT_AVAILABLE`,
  `TRAIN_CANCELLED`, `TRAIN_DEPARTED`, `UNKNOWN`, class-not-offered,
  failed batch items, malformed data.
- `PROVIDER_ERROR` (API failures) and `UNKNOWN` are **never** interpreted
  as unavailability: those legs keep the journey alive for reporting, and
  complete journeys containing them are returned as `unverified_candidates`
  with `unverified_segment_numbers`, `journey_status: PARTIALLY_VERIFIED`
  and per-segment error payloads. They are never labelled confirmed.
- Budget enforcement happens **before** provider requests (proven by
  tests that assert the exact number of HTTP calls made).
- Availability is deduplicated per search via the planner memo, batched
  (25 items/request, 1 credit per class per item), never cached across
  searches (freshness); schedules and discovery results are cached
  process-wide with a TTL (stable timetable data).

### Search-budget behavior

Budgets are computational safeguards, **not** train-change limits.
Defaults (override via env, see `.env.example`): 20 discovery pairs,
15 train schedules, 40 API requests, 120 credits, 30 s,
300 expanded states, 25 journeys, 20 results. When any budget, the time
limit or the horizon stops the search early:

- `search_completeness.complete = false` plus a precise
  `incomplete_reasons` entry (`TRAIN_LIMIT_REACHED`,
  `DISCOVERY_PAIR_LIMIT_REACHED`, `STATE_LIMIT_REACHED`,
  `JOURNEY_LIMIT_REACHED`, `BUDGET_REQUESTS_EXHAUSTED`,
  `BUDGET_CREDITS_EXHAUSTED`, `TIME_LIMIT_REACHED`,
  `HORIZON_LIMIT_REACHED`, `PROVIDER_ERROR`);
- the response status becomes `SEARCH_LIMIT_REACHED` when nothing fully
  confirmed was found, and the message states that NOT all possibilities
  were checked. The engine never claims an exhaustive search it did not
  perform.

### Response statuses

| Status | Meaning |
|---|---|
| `AVAILABLE_COMBINATION` | ≥ 1 fully confirmed train-change journey (budgets permitting; completeness still reported) |
| `PARTIALLY_VERIFIED` | nothing fully confirmed, but schedule-valid journeys exist whose availability could not be fully determined |
| `NO_COMPLETE_JOURNEY` | search completed within its budgets; nothing confirmed, nothing partial |
| `SEARCH_LIMIT_REACHED` | a configured limit stopped the search before completion and nothing fully confirmed was found |
| `NO_CANDIDATE_TRAINS` | no through train and no `via_stations` hints: no schedule could anchor interchange candidates (honest negative) |
| `PROVIDER_ERROR` | discovery or the search failed at the provider (HTTP 502); no availability claims |
| `CONFIG_ERROR` | API key not configured (HTTP 503) |

`BOOKING_CONFIRMED` is never used — this system does not book tickets.

### Sample response (abridged)

```json
{
  "success": true,
  "status": "AVAILABLE_COMBINATION",
  "message": "2 train-change journey(s) with fully confirmed availability found.",
  "search": {"from_station": "MYS", "to_station": "DWR", "journey_date": "2026-11-27",
              "travel_class": "3A", "quota": "GN", "ranking_preference": "FASTEST", "via_stations": []},
  "ranked_by": "FASTEST",
  "results": [
    {
      "journey_id": "2026-11-27-99801@MYS-99802@UBL-DWR",
      "journey_type": "TRAIN_CHANGE",
      "journey_status": "AVAILABLE_COMBINATION",
      "departure_datetime": "2026-11-27T08:00:00+05:30",
      "arrival_datetime": "2026-11-27T16:30:00+05:30",
      "total_duration_minutes": 510,
      "total_fare": 400.0,
      "fares_complete": true,
      "segment_count": 2,
      "train_changes": 1,
      "total_transfer_wait_minutes": 120,
      "transfers": [
        {"transfer_number": 1, "transfer_type": "TRAIN_CHANGE",
         "station": {"code": "UBL", "name": "Hubballi Junction", "sequence": 3},
         "from_train": "99801", "to_train": "99802",
         "arrival_datetime": "2026-11-27T13:00:00+05:30",
         "departure_datetime": "2026-11-27T15:00:00+05:30",
         "wait_minutes": 120, "within_min_buffer": true, "within_max_wait": true}
      ],
      "segments": [
        {"segment_number": 1, "segment_type": "TRAIN", "train_number": "99801",
         "from_station": {"code": "MYS"}, "to_station": {"code": "UBL"},
         "departure_datetime": "2026-11-27T08:00:00+05:30",
         "arrival_datetime": "2026-11-27T13:00:00+05:30",
         "travel_class": "3A", "quota": "GN",
         "boarding_query_date": "2026-11-27",
         "status": "CONFIRMED_AVAILABLE", "provider_status": "AVAILABLE",
         "availability_text": "AVAILABLE-0040", "fare": 300.0, "total_fare": 300.0,
         "last_updated_at": "2026-11-23T08:15:46.547+05:30",
         "checked_at": "2026-11-23T15:23:15.407647+00:00",
         "booking_url": null}
      ],
      "all_segments_confirmed_available": true,
      "availability_checked_at": "2026-11-23T15:23:15.407647+00:00",
      "oldest_availability_update_at": "2026-11-23T08:15:46.547+05:30"
    }
  ],
  "total_results": 2,
  "unverified_candidates": [],
  "direct_trains": [{"train_number": "99803", "availability_checked": false}],
  "stations": [{"station_code": "UBL", "events_expanded": 4,
                 "pairs": [{"from_station": "UBL", "to_station": "DWR", "trains_found": 4}],
                 "legs_evaluated": 9, "legs_confirmed": 6, "legs_rejected": 0, "legs_unverified": 0}],
  "skipped_trains": [],
  "search_completeness": {
    "complete": true,
    "budget": {"requests_used": 36, "requests_max": 40, "credits_used_estimate": 36,
                "credits_max": 120, "duration_ms": 1420, "time_budget_ms": 30000,
                "discovery_pairs_used": 14, "discovery_pairs_max": 20,
                "trains_processed": 11, "trains_total": 11,
                "states_expanded": 8, "states_max": 300,
                "journeys_found": 13, "journeys_max": 25},
    "segments_checked": 16, "distinct_availability_queries": 16, "deduplicated_queries": 0,
    "schedule_cache_hits": 0, "discovery_cache_hits": 0,
    "incomplete_reasons": [],
    "search_scope": {"min_transfer_buffer_minutes": 30, "max_transfer_wait_minutes": 720,
                      "search_horizon_days": 2, "horizon_end_date": "2026-11-29",
                      "boarding_date_at_source": "2026-11-27", "via_stations": []}
  },
  "api_usage": {"provider": "railcore", "requests_sent": 36, "successful_requests": 36,
                "failed_requests": 0, "credits_cost": 36, "credits_remaining": 464},
  "errors": [],
  "notes": ["...disclaimers: point-in-time checks, never booked, unverified journeys "
             "are not recommendations, fares never estimated, ..."]
}
```

Fields with `null` values are omitted from the JSON (`exclude_none`);
e.g. a journey with an unknown fare has no `total_fare` key at all.

### API-credit protection during development

Phase 3 was developed and verified **entirely against mocked transports**
(synthetic trains `998xx`); the production flow contains no mock data and
the provider integration is unchanged. Normal `python -m pytest` runs never
touch the network. Live-API tests are **opt-in and clearly separated**
(`tests/test_live_train_change.py`, pytest marker `live_api`): they are
skipped unless `RUN_LIVE_API_TESTS=1` and a real API key are set, and run
with deliberately tiny budgets. Tests also prove budget enforcement happens
*before* provider requests.

## Phase 2: the same-train combination search (kept)

`POST /api/v1/journeys/same-train/search` — the phase 2 engine is unchanged:
it finds trains whose complete journey can be covered by multiple
consecutive segments on the SAME train, every segment explicitly confirmed.
See the phase 2 documentation in git history for the full field reference;
the response contract is untouched.

## Phase 1: the feasibility endpoint (kept)

`POST /api/v1/test/segment-availability` — fetch one train's schedule,
validate that segment 1 precedes segment 2, then check both segments
independently with the same train/date/class/quota. Only explicit
`AVAILABLE` counts; API errors surface as `PROVIDER_ERROR`, never as
unavailability. Verified live on train 17301 (2026-11-24): 3A both
segments confirmed (AVAILABLE-69 / CURR_AVBL-27), SL MYS→ASK available but
ASK→DWR not — the segment pools genuinely differ.

## RailCore facts verified (2026-11-23, docs + live calls)

- Base URL `https://ir.railcore.tech/v1`; auth via `X-RailCore-Key`
  (Bearer/X-Api-Key also accepted; HMAC-SHA256 only for Growth+ plans —
  supported automatically when `RAILCORE_HMAC_SECRET` is set).
- `GET /routes/trains` — trains between stations (1 credit); `match=station`
  restricts to the two requested stations; `404 NO_TRAINS_FOUND` when none
  (not counted, honest dead end); the per-train `availability_summary` can
  be days old and is never used as evidence.
- `GET /trains/{train_number}/schedule` — full stop list with `sequence`,
  times, `halt_minutes`, `day` (overnight), running days, classes.
- `GET /availability/seats` — per-segment availability, fares,
  `last_updated_at`. The date refers to when the train **leaves the from
  station** (a wrong date returns `TRAIN_NOT_RUNNING`), which is why both
  engines compute per-segment boarding dates from day offsets.
- `POST /availability/seats/batch` — up to 25 items; **1 credit per
  distinct class per item**; in-place item failures (e.g.
  `BOOKING_NOT_OPEN`) carry no availability information.
- Envelope `{success, data, error, meta}`; rate-limit/credit headers on
  every response. Free plan: **10 req/min, 100/day, 500/month**; `429`
  with `Retry-After`; failed requests are free.
- Booking window: 60 days before journey; past-window items are not asked
  and not counted.
- No provider-side multi-leg journey search exists
  (`GET /journeys/search` returns direct trains only), which is why the
  phase 3 connection engine is built entirely in this backend.

## Tests

```bash
cd backend && python -m pytest            # 169 tests + 2 skipped (live)
# opt-in live tests (consume credits):
RUN_LIVE_API_TESTS=1 python -m pytest -m live_api
```

All provider responses in normal tests are mocked (trains `998xx`, the
phase 2 `999xx` and the phase 4 `997xx` are synthetic; mock data appears
only in automated tests — the real API flow is never faked). Coverage:

- Phase 1 (55): schedule retrieval, status mapping, sequences, provider
  failures (timeout/401/402/429/5xx), malformed payloads, batch checks,
  credential hygiene.
- Phase 2 (34): one/multiple intermediate stations, all-confirmed, one leg
  unavailable, RAC/waitlist rejection, invalid station order, overnight
  boarding dates, duplicate combinations and query deduplication,
  discovery/schedule/batch/item failures, fare calculation, budgets,
  ranking preferences, skip reasons, validation errors, cross-search
  caching, OpenAPI documentation, disclaimers.
- Phase 3 (37): all twenty required scenarios — 1/2/3 train changes,
  several valid connections, transfer-buffer and max-wait rejection plus
  per-request overrides, pass-through interchange rejection, overnight
  midnight-crossing connections with per-segment boarding dates,
  confirmed/WAITLIST/RAC/UNKNOWN statuses, provider errors (never
  unavailable), duplicate candidates, circular-train cycle prevention,
  request/credit budget exhaustion *before any availability call*,
  time-limit and horizon exhaustion, all three ranking preferences,
  missing fares (never estimated), unverified journeys never recommended,
  plus: direct trains never availability-checked, via-stations bootstrap,
  honest no-through-train negative, validation errors, OpenAPI docs,
  settings wiring and cross-search cache reuse.
- Phase 4 (43): all thirty required scenarios — the four mixed shapes
  (same-train group → train change, train change → same-train group, two
  same-train groups via one change, multiple changes of both kinds),
  pure same-train and pure train-change journeys, invalid station order,
  pass-through interchange, transfer-buffer and max-wait rejection plus
  per-request overrides, overnight seat changes with per-segment boarding
  dates (d/d/d+1), overnight train change across midnight, confirmed /
  WAITLIST / RAC / UNKNOWN / provider-error segments, duplicate candidate
  trains and unique journey signatures, circular-train cycle prevention,
  three train changes with no through train (via-stations bootstrap), four
  seat changes, missing fares, all four ranking preferences, request and
  credit budget exhaustion *before any availability call*, horizon
  exhaustion, query deduplication, unverified journeys never fully
  available — plus: API response shape, OpenAPI docs, settings wiring,
  budget clamps, validation errors, config-error 503, provider-error 502,
  honest no-through-train negative, time-limit stop, direct trains never
  availability-checked, seat changes never labelled transfers, and
  cross-engine consistency with the phase 2 and phase 3 engines on the
  same mocked network.

## Security

- The API key lives only in `backend/.env` (git-ignored) and the
  `X-RailCore-Key` header; it is never logged (plus a regex mask as defense
  in depth), never returned by any endpoint, never placed in a URL.
- `.env.example` documents every variable without real values.
- If a key is ever exposed: revoke and rotate it immediately.

## Known limitations (Phase 3)

1. **Not a booking and not a booking permission.** Point-in-time
   availability checks only. Whether Indian Railways permits a
   multi-ticket journey must be verified before relying on it; delays can
   break tight connections even when the schedule validates.
2. **Discovery is schedule-anchored.** Interchange candidates come only
   from fetched schedules, seed pairs and (max 3) `via_stations` hints.
   Routes with **no through train at all** need one hint per interchange
   that no schedule reveals (the provider has no "trains departing a
   station on a date" endpoint); without hints the engine returns an
   honest `NO_CANDIDATE_TRAINS`. Branch stations invisible to every
   fetched schedule are out of reach.
3. **Beyond-destination pruning.** Stations that appear only after the
   destination on every fetched schedule are not queried as discovery
   targets (they would waste the pair budget); if reached as alighting
   stops they still connect onward. Exotic overshoot-and-return journeys
   that need dedicated pairs toward such stations can be missed.
4. **Availability freshness.** The provider may serve figures read earlier
   (`last_updated_at`); each segment carries its provider timestamp and
   each journey reports the oldest one. A strict freshness policy is not
   yet enforced.
5. **Free-tier rate limits.** A full corridor crawl can exceed 10
   requests/minute; client-side pacing is available
   (`SEARCH_MIN_REQUEST_INTERVAL_SECONDS`).
6. **Fares can be missing.** Unknown fares are `null` (dropped from the
   JSON) and listed in `segments_missing_fare`; CHEAPEST ranks them last.
   Never estimated. The provider supplies no booking URLs, so
   `booking_url` is always null.
7. **Single travel class and quota per search.** Cross-class journeys are
   a later feature.
8. **Schedule-day edge cases.** Schedules lacking `day` offsets are
   treated as day 1; legs whose computed arrival would precede their
   departure are rejected (misses, never invents). Circular trains are
   handled at station-code level (same-day re-boarding approximations are
   documented in the code).
9. **No persistence.** Caches are in-process; journey storage, refresh
   and comparison endpoints arrive with the DB phase.
10. **Same-station transfers only.** Walking to a nearby station is out of
    scope; both trains must stop at the exact same station code.

## Known limitations (Phase 4)

All phase 3 limitations apply and are inherited (schedule-anchored
discovery, availability freshness, free-tier rate limits, missing fares,
single class/quota per search, no persistence, same-station transfers
only). Additionally:

1. **Same-run continuations only.** Consecutive segments on one train
   always belong to the run being ridden (anchored by the actual arrival
   datetime). Journeys that alight from train T and later re-board a
   *different run* of the same train number are not generated — a
   deliberate rule that keeps seat changes unambiguous; such journeys are
   extremely rare in practice.
2. **Seat-change halts are not waiting time.** `total_transfer_wait_minutes`
   counts train-change waits only; the halt a passenger stays onboard for
   during a seat change is reported per transition
   (`onboard_halt_minutes`) but not summed into waiting.
3. **No station revisits (inherited).** Journeys that would legitimately
   ride a circular service a full lap (revisiting a station) are out of
   scope, consistent with phase 3.
4. **Combinatorial growth.** Same-train partitions multiply with train
   connections; the reported budgets (`max_expanded_states`,
   `max_journeys`, request/credit/time) cap the search honestly, and
   `SEARCH_LIMIT_REACHED` says so.
5. **Mixed semantics of ranking.** `FEWEST_SEAT_CHANGES` and
   `FEWEST_TRAIN_CHANGES` can coincide on networks where the fastest
   journey also has the fewest changes; ties break deterministically
   (fare → duration → segment count → journey id).

## Phase 5 next steps

All three engines now emit self-contained, composable journey objects
with explicit `journey_type` (`SAME_TRAIN_SEGMENTS` / `TRAIN_CHANGE` /
`MIXED`), per-segment statuses/fares/boarding dates and explicit segment
+ transition typing, so later phases can consume them directly: the
station manager and persistence layer (PostgreSQL/Alembic when genuinely
required), refresh/comparison endpoints, and the AI agent layer on top
of the three verified search engines.
