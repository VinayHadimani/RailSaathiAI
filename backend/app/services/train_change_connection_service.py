"""Train-change connection search engine (Phase 3).

Given source, destination, journey date, class and quota, find complete
journeys that require CHANGING TRAINS at intermediate stations, with every
segment explicitly confirmed available (provider status AVAILABLE). Any
number of train changes is supported - there is no fixed maximum; the
search is bounded only by *reported* computational budgets.

Pipeline
--------
1. Seed discovery:     GET /routes/trains (S -> D, 1 credit). Through
                       trains become (a) the direct-train reference list and
                       (b) the first source of corridor stations.
2. Corridor learning:  every fetched schedule teaches the engine the
                       stations that exist between source and destination.
                       Discovery pairs are ONLY ever built from stations
                       that appear in fetched schedules - never by blindly
                       enumerating station pairs.
3. First-leg pairs:    GET /routes/trains for (S, Y) with Y a known
                       station, nearest-corridor-first, budget-capped.
                       Optional request-level via_stations are extra
                       anchors (the bootstrap for routes with no through
                       train at all).
4. Chronological BFS:  arrival events are explored in arrival-time order
                       (a time-dependent earliest-arrival search over a
                       leg graph derived from pool schedules). Expanding an
                       arrival at station X:
                         - lazily discovers trains for (X, Y) pairs whose
                           Y is not known to lie before X (plus (X, D)),
                           for the calendar dates the transfer window
                           covers;
                         - generates candidate legs purely from schedule
                           data (actual stops, station order, running
                           days, day offsets, transfer buffer / max wait,
                           horizon, no station revisits, consecutive
                           trains must differ) - impossible connections
                           are rejected BEFORE any availability query;
                         - checks availability through the existing
                           SegmentQueryPlanner (batched, deduplicated,
                           budget-enforced BEFORE requests, cached).
5. Extension rule:     only CONFIRMED_AVAILABLE legs extend the search.
                       RAC / waitlist / not-available legs are dead
                       branches. PROVIDER_ERROR / UNKNOWN legs are
                       traversed but every journey through them is
                       reported as an unverified candidate, never as a
                       confirmed result (a provider error is never treated
                       as unavailability).
6. Assembly/ranking:   journeys are assembled with per-segment
                       availability/fares/timestamps, transfer blocks,
                       totals (fare, duration incl. waits, changes, wait
                       sum) and ranked cheapest / fastest / fewest changes.

Direct single-train journeys (S -> D on one train) are NOT results of this
engine; discovered through trains are reported in ``direct_trains`` without
any availability claim. Mixed same-train + train-change journeys are
deliberately out of scope (phase 4).
"""

from __future__ import annotations

import heapq
import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as time_cls, timedelta, timezone
from itertools import count
from typing import Any

from ..core.config import get_settings
from ..models.route_search import RouteTrainSummary
from ..models.schedule import TrainSchedule
from ..providers.base import (
    InternalAvailabilityStatus,
    ProviderClientError,
    ProviderNotConfiguredError,
    RailwayProviderError,
)
from ..providers.railcore import RailCoreClient
from ..schemas.journey_search import ApiUsageOut, RankingPreference
from ..schemas.train_change_search import (
    DirectTrainRefOut,
    JourneyVerification,
    PairDiscoveryOut,
    StationPointOut,
    StationReportOut,
    TrainChangeBudgetUsageOut,
    TrainChangeCompletenessOut,
    TrainChangeJourneyOut,
    TrainChangeSearchRequest,
    TrainChangeSearchResponse,
    TrainChangeSearchStatus,
    TrainSegmentOut,
    TrainSkipReportOut,
    TransferOut,
    JourneyEndpointOut,
)
from .search_budgets import TrainChangeBudgets
from .segment_query_planner import (
    STOP_BUDGET_CREDITS,
    STOP_BUDGET_REQUESTS,
    STOP_PROVIDER_ERROR,
    STOP_TIME_LIMIT,
    SegmentQueryPlanner,
    SegmentQueryOutcome,
    _BudgetExhaustedError,
)

logger = logging.getLogger("railroute.services.train_change_engine")

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

# Search-incompleteness reasons (surfaced in search_completeness).
REASON_TRAIN_LIMIT = "TRAIN_LIMIT_REACHED"
REASON_PAIR_LIMIT = "DISCOVERY_PAIR_LIMIT_REACHED"
REASON_STATE_LIMIT = "STATE_LIMIT_REACHED"
REASON_JOURNEY_LIMIT = "JOURNEY_LIMIT_REACHED"
REASON_BUDGET_REQUESTS = "BUDGET_REQUESTS_EXHAUSTED"
REASON_BUDGET_CREDITS = "BUDGET_CREDITS_EXHAUSTED"
REASON_TIME = "TIME_LIMIT_REACHED"
REASON_HORIZON = "HORIZON_LIMIT_REACHED"
REASON_PROVIDER = "PROVIDER_ERROR"

# Reasons that mean a configured limit (not a provider fault) stopped the
# search early -> SEARCH_LIMIT_REACHED when nothing fully confirmed was found.
LIMIT_REASONS = {
    REASON_TRAIN_LIMIT,
    REASON_PAIR_LIMIT,
    REASON_STATE_LIMIT,
    REASON_JOURNEY_LIMIT,
    REASON_BUDGET_REQUESTS,
    REASON_BUDGET_CREDITS,
    REASON_TIME,
    REASON_HORIZON,
}

PLANNER_STOP_MAP = {
    STOP_BUDGET_REQUESTS: REASON_BUDGET_REQUESTS,
    STOP_BUDGET_CREDITS: REASON_BUDGET_CREDITS,
    STOP_TIME_LIMIT: REASON_TIME,
    STOP_PROVIDER_ERROR: REASON_PROVIDER,
}

SEARCH_NOTES = [
    "These results are point-in-time availability checks, not booked tickets. RailRoute AI does not book tickets.",
    "Every segment of every confirmed result explicitly reports confirmed availability (provider status AVAILABLE). "
    "RAC, waitlist, regret, unavailable, unknown and provider errors never qualify.",
    "Journeys whose availability could not be fully verified are returned separately as unverified candidates "
    "(journey_status PARTIALLY_VERIFIED) and are never labelled confirmed. A provider error is never interpreted "
    "as an unavailable seat.",
    "Each train change happens at one interchange station with the exact same station code on both trains; "
    "nearby but different stations are never assumed to be interchangeable.",
    "Availability figures can be older than the check time (provider-side caching): each segment carries its "
    "provider last_updated_at and each journey reports the oldest one.",
    "Direct through trains are listed for reference only: their availability is NOT checked by this engine.",
    "Fares are summed per segment and never estimated; when any segment fare is unknown the total fare is null "
    "and the affected segments are listed.",
    "A train-change journey involves separate tickets on separate trains; connection validity depends on the "
    "trains running to time. Verify railway rules and leave realistic buffers before relying on a connection.",
]


def _hhmm_to_time(value: str | None) -> time_cls | None:
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        return time_cls(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _fare_of(outcome: SegmentQueryOutcome) -> float | None:
    requested = outcome.availability.requested_class if outcome.availability else None
    if requested is None:
        return None
    if requested.total_fare is not None:
        return requested.total_fare
    return requested.fare


@dataclass(frozen=True)
class _Leg:
    """One schedule-valid train leg (no availability attached)."""

    train_number: str
    train_name: str | None
    from_code: str
    from_name: str | None
    from_seq: int
    to_code: str
    to_name: str | None
    to_seq: int
    departure_time: str | None
    departure_day: int | None
    arrival_time: str | None
    arrival_day: int | None
    boarding_date: str  # ISO date the train departs from_code (IRCTC semantics)
    departure_dt: datetime
    arrival_dt: datetime
    halt_minutes: int | None
    distance_km: float | None
    segment_distance_km: float | None = None

    @property
    def query_key(self) -> tuple[str, str, str, str]:
        return (self.train_number, self.from_code, self.to_code, self.boarding_date)


@dataclass(frozen=True)
class _Path:
    """A partial or complete journey: ordered legs + their outcomes."""

    legs: tuple[_Leg, ...]
    outcomes: tuple[SegmentQueryOutcome | None, ...]
    visited: frozenset[str]
    unverified_legs: tuple[int, ...] = ()  # 0-based indices of unverified legs

    def extended(
        self, leg: _Leg, outcome: SegmentQueryOutcome, unverified: bool
    ) -> "_Path":
        legs = self.legs + (leg,)
        unverified_legs = self.unverified_legs + (len(legs) - 1,) if unverified else self.unverified_legs
        return _Path(
            legs=legs,
            outcomes=self.outcomes + (outcome,),
            visited=self.visited | {leg.to_code},
            unverified_legs=unverified_legs,
        )


class _RunState:
    """Mutable per-search state."""

    def __init__(self, request: TrainChangeSearchRequest, budgets: TrainChangeBudgets) -> None:
        self.request = request
        self.budgets = budgets
        self.known: set[str] = set()
        self.pool: dict[str, TrainSchedule] = {}
        self.before: dict[str, set[str]] = {}  # station -> stations known to lie before it
        self.pending_schedules: list[str] = []
        self.pending_set: set[str] = set()
        self.skip_reported: set[str] = set()  # train numbers already in skip reports
        self.seen_trains: set[str] = set()  # every distinct discovered train number
        self.train_names: dict[str, str] = {}
        self.station_names: dict[str, str] = {}
        self.pair_done: set[tuple[str, str, str]] = set()
        self.pairs_used = 0
        self.event_counter = count()  # stable tiebreak for the event heap
        self.reasons: list[str] = []
        self.errors: list[dict] = []
        self.events: list[tuple[datetime, int, _Path]] = []
        self.journeys: list[_Path] = []
        self.candidates: list[_Path] = []
        self.states_expanded = 0
        self.horizon_truncated = False
        self.unqueried_legs = 0
        self.direct_trains: list[DirectTrainRefOut] = []
        self.skip_reports: list[TrainSkipReportOut] = []
        self.station_reports: dict[str, dict[str, Any]] = {}
        self.seed_pair_done: set[str] = set()
        self.seed_no_through_trains = False
        self.seed_trains: set[str] = set()
        self.seed_corridor: set[str] = set()
        self.hubs_to_dest: dict[str, int] | None = None

    # ---------------------------------------------------------------- helpers

    def add_reason(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def station_report(self, code: str) -> dict[str, Any]:
        report = self.station_reports.get(code)
        if report is None:
            report = {
                "station_code": code,
                "station_name": self.station_names.get(code),
                "events_expanded": 0,
                "first_arrival_datetime": None,
                "pairs": [],
                "legs_evaluated": 0,
                "legs_confirmed": 0,
                "legs_rejected": 0,
                "legs_unverified": 0,
            }
            self.station_reports[code] = report
        return report


class TrainChangeConnectionService:
    """Orchestrates the train-change connection search."""

    def __init__(self, client: RailCoreClient, budgets: TrainChangeBudgets | None = None) -> None:
        self.client = client
        self.budgets = budgets or TrainChangeBudgets.from_settings(get_settings())
        self.planner = SegmentQueryPlanner(client, self.budgets)

    # ------------------------------------------------------------------ run

    def run(self, request: TrainChangeSearchRequest) -> tuple[dict, int]:
        """Execute the search. Returns (response_body_dict, http_status)."""
        started = time.monotonic()
        deadline = started + self.budgets.time_budget_seconds
        stats_before = self.client.stats_snapshot()
        credits_before = self.client.credits_cost_total

        try:
            budgets = self.budgets.with_overrides(
                request.min_transfer_buffer_minutes,
                request.max_transfer_wait_minutes,
            )
        except ValueError as exc:
            return self._override_response(
                request, started, stats_before, credits_before,
                status=TrainChangeSearchStatus.CONFIG_ERROR,
                message=str(exc),
                http_status=422,
            )

        state = _RunState(request, budgets)
        source, destination = request.from_station, request.to_station
        state.known |= {source, destination, *request.via_stations}
        horizon_end = datetime.combine(
            request.journey_date + timedelta(days=budgets.search_horizon_days),
            time_cls(23, 59, 59),
            tzinfo=IST,
        )

        # ---- Step 1: seed discovery (S -> D) --------------------------------
        early = self._seed_discovery(state, deadline)
        if early is not None:
            status, http_status, message = early
            return self._override_response(
                state, started, stats_before, credits_before,
                status=status, message=message, http_status=http_status,
            )

        # ---- Steps 2-3: schedules + first-leg pair discovery -----------------
        self._fetch_schedules(state, deadline)
        self._seed_pair_pass(state, deadline)
        self._fetch_schedules(state, deadline)

        # ---- Steps 4-5: staged first legs + chronological BFS --------------
        self._push_and_explore(state, horizon_end, deadline)
        if state.horizon_truncated:
            state.add_reason(REASON_HORIZON)

        # ---- Step 6: assemble ------------------------------------------------
        return self._finish(state, started, stats_before, credits_before)

    # -------------------------------------------------------------- discovery

    def _seed_discovery(self, state: _RunState, deadline: float):
        """Discover through trains S -> D. Returns an early-exit tuple or None."""
        request = state.request
        source, destination = request.from_station, request.to_station
        journey_date = request.journey_date.isoformat()
        try:
            result = self.planner.get_trains_between(
                from_station=source, to_station=destination, journey_date=journey_date, quota=request.quota
            )
        except ProviderNotConfiguredError as exc:
            return TrainChangeSearchStatus.CONFIG_ERROR, 503, exc.message
        except ProviderClientError as exc:
            if exc.code == "NO_TRAINS_FOUND":
                state.seed_no_through_trains = True
                if request.via_stations:
                    # Hints anchor the search without any through train.
                    return None
                return (
                    TrainChangeSearchStatus.NO_CANDIDATE_TRAINS,
                    200,
                    f"RailCore reports no trains running between {source} and {destination} on "
                    f"{journey_date}. With no through train there are no schedules to derive "
                    "interchange candidates from, and the provider offers no 'trains departing a "
                    "station' endpoint to enumerate them. Retry with one to three via_stations "
                    "hints to anchor the search. No availability claims are made.",
                )
            state.errors.append(exc.to_dict())
            state.add_reason(REASON_PROVIDER)
            return (
                TrainChangeSearchStatus.PROVIDER_ERROR,
                502,
                f"Candidate discovery for {source} -> {destination} failed at the provider "
                f"({exc.code}); no availability claims are made for this search.",
            )
        except _BudgetExhaustedError:
            state.add_reason(REASON_BUDGET_REQUESTS)
            return None
        except RailwayProviderError as exc:
            state.errors.append(exc.to_dict())
            state.add_reason(REASON_PROVIDER)
            return (
                TrainChangeSearchStatus.PROVIDER_ERROR,
                502,
                f"Candidate discovery for {source} -> {destination} failed at the provider "
                f"({exc.code}); no availability claims are made for this search.",
            )

        state.pair_done.add((source, destination, journey_date))
        state.pairs_used += 1
        report = state.station_report(source)
        cache_hits_before = self.planner.stats["discovery_cache_hits"]
        pair = PairDiscoveryOut(
            from_station=source,
            to_station=destination,
            journey_date=journey_date,
            trains_found=len(result.trains),
            cache_hit=self.planner.stats["discovery_cache_hits"] > cache_hits_before,
        )
        report["pairs"].append(pair)
        seen_direct: set[str] = {entry.train_number for entry in state.direct_trains}
        for train in result.trains:
            state.seed_trains.add(train.train_number)
            if train.train_number not in seen_direct:  # duplicate candidate trains
                seen_direct.add(train.train_number)
                state.direct_trains.append(
                    DirectTrainRefOut(
                        train_number=train.train_number,
                        train_name=train.train_name or train.extended_train_name,
                        from_station_code=train.from_station_code,
                        to_station_code=train.to_station_code,
                        departure_time=train.departure_time,
                        arrival_time=train.arrival_time,
                        classes=list(train.classes),
                    )
                )
            self._register_discovered_train(state, train)
        return None

    def _register_discovered_train(self, state: _RunState, train: RouteTrainSummary) -> None:
        number = train.train_number
        if train.train_name or train.extended_train_name:
            state.train_names.setdefault(number, train.train_name or train.extended_train_name)
        state.seen_trains.add(number)
        if number in state.pool or number in state.pending_set or number in state.skip_reported:
            return  # duplicate candidate train: pool and reports stay deduplicated
        if train.classes and state.request.travel_class not in train.classes:
            state.skip_reported.add(number)
            state.skip_reports.append(
                TrainSkipReportOut(
                    train_number=number,
                    train_name=train.train_name or train.extended_train_name,
                    skip_reason="CLASS_NOT_OFFERED",
                    skip_detail=(
                        f"Train does not carry class {state.request.travel_class} "
                        f"(classes: {', '.join(train.classes)})."
                    ),
                )
            )
            return
        is_known_class = bool(train.classes and state.request.travel_class in train.classes)
        is_special = str(number).startswith("0")
        if is_known_class and not is_special:
            # Place trains known to carry the requested class ahead of unknown-class/special entries.
            insert_at = len(state.pending_schedules)
            for idx, existing_no in enumerate(state.pending_schedules):
                if existing_no not in state.seed_trains and str(existing_no).startswith("0"):
                    insert_at = idx
                    break
            state.pending_schedules.insert(insert_at, number)
        else:
            state.pending_schedules.append(number)
        state.pending_set.add(number)

    def _seed_pair_pass(self, state: _RunState, deadline: float) -> None:
        """Discover trains from S toward every known in-corridor station.

        Repeats while new stations are learned from fetched schedules and
        the pair budget allows it. Ordered nearest-corridor-first so the
        most plausible interchanges are discovered before the cap bites.
        """
        request = state.request
        source, destination = request.from_station, request.to_station
        while True:
            remaining = sorted(
                (
                    code
                    for code in state.known
                    if code not in {source, destination}
                    and code not in state.seed_pair_done
                    and not self._beyond_destination(state, code, destination)
                ),
                key=lambda code: (self._corridor_distance(state, source, code), code),
            )
            if not remaining:
                return
            for target in remaining:
                if state.pairs_used >= state.budgets.max_discovery_pairs:
                    state.add_reason(REASON_PAIR_LIMIT)
                    return
                if time.monotonic() > deadline:
                    state.add_reason(REASON_TIME)
                    return
                if not self.planner.requests_available():
                    state.add_reason(REASON_BUDGET_REQUESTS)
                    return
                if not self.planner.credits_available(1):
                    state.add_reason(REASON_BUDGET_CREDITS)
                    return
                self._discover_pair(state, source, target, request.journey_date.isoformat())
                state.seed_pair_done.add(target)
                self._fetch_schedules(state, deadline)
                if state.pairs_used >= state.budgets.max_discovery_pairs:
                    state.add_reason(REASON_PAIR_LIMIT)
                    return

    def _discover_pair(self, state: _RunState, from_code: str, to_code: str, iso_date: str) -> None:
        """One trains-between-stations query; records a pair report entry."""
        key = (from_code, to_code, iso_date)
        if key in state.pair_done:
            return
        state.pair_done.add(key)
        state.pairs_used += 1
        report = state.station_report(from_code)
        cache_hits_before = self.planner.stats["discovery_cache_hits"]
        pair = PairDiscoveryOut(from_station=from_code, to_station=to_code, journey_date=iso_date)
        try:
            result = self.planner.get_trains_between(
                from_station=from_code, to_station=to_code, journey_date=iso_date,
                quota=state.request.quota,
            )
        except ProviderClientError as exc:
            if exc.code == "NO_TRAINS_FOUND":
                pair.no_trains_found = True  # honest dead end; not an error, no claim
            else:
                pair.error = exc.to_dict()
                state.errors.append(exc.to_dict())
                state.add_reason(REASON_PROVIDER)
        except _BudgetExhaustedError:
            state.add_reason(REASON_BUDGET_REQUESTS)
        except RailwayProviderError as exc:
            pair.error = exc.to_dict()
            state.errors.append(exc.to_dict())
            state.add_reason(REASON_PROVIDER)
        else:
            pair.trains_found = len(result.trains)
            pair.cache_hit = self.planner.stats["discovery_cache_hits"] > cache_hits_before
            for train in result.trains:
                self._register_discovered_train(state, train)
        report["pairs"].append(pair)

    def _discovery_targets(self, state: _RunState, station: str) -> list[str]:
        """Pair targets from `station`: the destination plus every known
        station not known to lie before it (corridor-nearest first).

        Stations that lie beyond the destination on every pool train that
        carries both are skipped: journeys through them would have to come
        back past the destination, and such pairs only waste the discovery
        budget. The station itself is always reachable as an alighting
        stop, so onward discovery from it still happens if it is reached.
        """
        request = state.request
        destination, source = request.to_station, request.from_station
        before = state.before.get(station, set())
        targets: list[tuple[int, str]] = []
        for code in state.known:
            if code in (station, source, destination) or code in before:
                continue
            if self._beyond_destination(state, code, destination):
                continue
            targets.append((self._corridor_distance(state, station, code), code))
        targets.sort()
        return [destination] + [code for _, code in targets]

    def _beyond_destination(self, state: _RunState, code: str, destination: str) -> bool:
        """True when `code` lies after the destination on every pool train
        carrying both (i.e. it is never an in-corridor station)."""
        both = 0
        for schedule in state.pool.values():
            stop_code = schedule.stop_by_code(code)
            stop_dest = schedule.stop_by_code(destination)
            if stop_code is None or stop_dest is None:
                continue
            both += 1
            if stop_code.sequence < stop_dest.sequence:
                return False
        return both > 0

    def _corridor_distance(self, state: _RunState, a: str, b: str) -> int:
        """Minimum stop-sequence distance a -> b across pool trains (999 when unknown)."""
        best = 999
        for schedule in state.pool.values():
            stop_a = schedule.stop_by_code(a)
            stop_b = schedule.stop_by_code(b)
            if stop_a is not None and stop_b is not None and stop_b.sequence > stop_a.sequence:
                best = min(best, stop_b.sequence - stop_a.sequence)
        return best

    def _known_before(self, state: _RunState, station: str, other: str) -> bool:
        return other in state.before.get(station, set())

    # --------------------------------------------------------------- schedules

    def _fetch_schedules(self, state: _RunState, deadline: float) -> None:
        """Drain the pending-schedule queue within the pool cap and budgets."""
        request = state.request
        while state.pending_schedules:
            if len(state.pool) >= state.budgets.max_trains:
                self._skip_pending(state, "TRAIN_LIMIT_REACHED",
                                   f"Search budget allows {state.budgets.max_trains} train schedules per search; "
                                   "these candidates were not processed.")
                state.add_reason(REASON_TRAIN_LIMIT)
                return
            if time.monotonic() > deadline:
                state.add_reason(REASON_TIME)
                return
            if not self.planner.requests_available():
                self._skip_pending(state, "BUDGET_EXHAUSTED",
                                    "API request budget exhausted before the schedule could be fetched.")
                state.add_reason(REASON_BUDGET_REQUESTS)
                return
            if not self.planner.credits_available(1):
                self._skip_pending(state, "BUDGET_EXHAUSTED",
                                    "API credit budget exhausted before the schedule could be fetched.")
                state.add_reason(REASON_BUDGET_CREDITS)
                return
            number = state.pending_schedules.pop(0)
            state.pending_set.discard(number)
            try:
                schedule = self.planner.get_schedule(number)
            except _BudgetExhaustedError:
                state.add_reason(REASON_BUDGET_REQUESTS)
                state.pending_schedules.insert(0, number)
                state.pending_set.add(number)
                return
            except RailwayProviderError as exc:
                state.skip_reported.add(number)
                state.skip_reports.append(
                    TrainSkipReportOut(
                        train_number=number,
                        train_name=state.train_names.get(number),
                        skip_reason="SCHEDULE_FETCH_FAILED",
                        skip_detail="The train schedule could not be retrieved; this train is unverified, not unavailable.",
                        error=exc.to_dict(),
                    )
                )
                state.errors.append(exc.to_dict())
                state.add_reason(REASON_PROVIDER)
                continue
            if schedule.classes and request.travel_class not in schedule.classes:
                state.skip_reported.add(number)
                state.skip_reports.append(
                    TrainSkipReportOut(
                        train_number=number,
                        train_name=schedule.train_name or state.train_names.get(number),
                        skip_reason="CLASS_NOT_OFFERED",
                        skip_detail=(
                            f"Train does not carry class {request.travel_class} "
                            f"(classes: {', '.join(schedule.classes)})."
                        ),
                    )
                )
                continue
            state.pool[number] = schedule
            self._learn_schedule(state, schedule)

    def _skip_pending(self, state: _RunState, reason: str, detail: str) -> None:
        for number in state.pending_schedules:
            if number in state.skip_reported:
                continue
            state.skip_reported.add(number)
            state.skip_reports.append(
                TrainSkipReportOut(
                    train_number=number,
                    train_name=state.train_names.get(number),
                    skip_reason=reason,
                    skip_detail=detail,
                )
            )
        state.pending_schedules.clear()
        state.pending_set.clear()

    def _learn_schedule(self, state: _RunState, schedule: TrainSchedule) -> None:
        """Learn station universe + partial order from one fetched schedule."""
        is_seed = schedule.train_number in state.seed_trains
        seen_dest_in_seed = False
        for stop in schedule.stops:
            code = stop.station_code.strip().upper()
            state.known.add(code)
            if is_seed and not seen_dest_in_seed:
                if code == state.request.to_station:
                    seen_dest_in_seed = True
                elif code != state.request.from_station:
                    state.seed_corridor.add(code)
            if stop.station_name and code not in state.station_names:
                state.station_names[code] = stop.station_name
        # Partial ordering with first-occurrence dedupe (circular routes).
        ordered: list[str] = []
        seen: set[str] = set()
        for stop in schedule.stops:
            code = stop.station_code.strip().upper()
            if code not in seen:
                seen.add(code)
                ordered.append(code)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                state.before.setdefault(second, set()).add(first)

    # ------------------------------------------------------------- leg graph

    def _generate_legs(
        self,
        state: _RunState,
        *,
        station: str,
        visited: frozenset[str] | set[str],
        last_train: str | None,
        window_start: datetime,
        window_end: datetime,
        first_leg: bool,
        horizon_end: datetime,
    ) -> list[_Leg]:
        """Schedule-derived candidate legs from `station` inside the window.

        Pure computation - no provider calls. Every leg is validated:
        actual stops both ends, forward station order, running days against
        the run's origin departure date, overnight-safe day offsets, the
        transfer window (buffer + max wait), the horizon, no station
        revisits, and consecutive trains must differ. First legs never end
        at the destination (that is a direct train, not a train change).
        """
        request = state.request
        destination = request.to_station
        legs: list[_Leg] = []
        seen_legs: set[tuple[str, int, int, str]] = set()
        for train_number in sorted(state.pool):
            if train_number == last_train:
                continue  # a train change means actually changing trains
            schedule = state.pool[train_number]
            for index, stop in enumerate(schedule.stops):
                if stop.station_code != station or stop.is_stop is False:
                    continue
                dep_time_str = stop.departure_time or stop.arrival_time
                dep_time = _hhmm_to_time(dep_time_str)
                if dep_time is None:
                    continue
                day_from = stop.day or 1
                # Candidate runs: boarding dates covering the window.
                boarding = window_start.date()
                last_boarding = window_end.date()
                while boarding <= last_boarding:
                    origin_date = boarding - timedelta(days=day_from - 1)
                    if schedule.running_days and WEEKDAY_CODES[origin_date.weekday()] not in schedule.running_days:
                        boarding += timedelta(days=1)
                        continue
                    dep_dt = datetime.combine(boarding, dep_time, tzinfo=IST)
                    if getattr(self.client, "_owns_client", False):
                        now_ist = datetime.now(tz=IST)
                        if dep_dt < now_ist - timedelta(minutes=5):
                            boarding += timedelta(days=1)
                            continue
                    if window_start <= dep_dt <= window_end:
                        for to_stop in schedule.stops[index + 1:]:
                            if to_stop.station_code == station or to_stop.is_stop is False:
                                continue
                            to_code = to_stop.station_code
                            if to_code in visited:
                                continue
                            if first_leg and to_code == destination:
                                continue  # direct train: reported in direct_trains, not a result
                            if to_code != destination and self._beyond_destination(state, to_code, destination):
                                continue
                            arr_time_str = to_stop.arrival_time or to_stop.departure_time
                            arr_time = _hhmm_to_time(arr_time_str)
                            if arr_time is None:
                                continue
                            day_to = to_stop.day or 1
                            arrival_dt = datetime.combine(
                                origin_date + timedelta(days=day_to - 1), arr_time, tzinfo=IST
                            )
                            if arrival_dt <= dep_dt:
                                continue  # impossible: guards malformed day offsets
                            if arrival_dt > horizon_end:
                                state.horizon_truncated = True
                                continue
                            key = (train_number, stop.sequence, to_stop.sequence, boarding.isoformat())
                            if key in seen_legs:
                                continue
                            seen_legs.add(key)
                            legs.append(
                                _Leg(
                                    train_number=train_number,
                                    train_name=schedule.train_name or state.train_names.get(train_number),
                                    from_code=station,
                                    from_name=stop.station_name or state.station_names.get(station),
                                    from_seq=stop.sequence,
                                    to_code=to_code,
                                    to_name=to_stop.station_name or state.station_names.get(to_code),
                                    to_seq=to_stop.sequence,
                                    departure_time=dep_time_str,
                                    departure_day=stop.day,
                                    arrival_time=arr_time_str,
                                    arrival_day=to_stop.day,
                                    boarding_date=boarding.isoformat(),
                                    departure_dt=dep_dt,
                                    arrival_dt=arrival_dt,
                                    halt_minutes=to_stop.halt_minutes,
                                    distance_km=to_stop.distance_km,
                                    segment_distance_km=(
                                        round(to_stop.distance_km - stop.distance_km, 2)
                                        if stop.distance_km is not None
                                        and to_stop.distance_km is not None
                                        and to_stop.distance_km >= stop.distance_km
                                        else None
                                    ),
                                )
                            )
                    boarding += timedelta(days=1)
        return legs

    def _query_legs(
        self,
        state: _RunState,
        legs: list[_Leg],
        deadline: float,
        report: dict[str, Any] | None = None,
    ) -> dict[tuple[str, str, str, str], SegmentQueryOutcome]:
        """Availability for candidate legs, deduplicated and batched.

        Budgets are enforced BEFORE any provider request (by the planner
        and re-checked here); unqueried legs are never treated as
        unavailable - they simply cannot extend the search, and the stop
        reason is reported.
        """
        if not legs:
            return {}
        if not self.planner.requests_available():
            state.add_reason(REASON_BUDGET_REQUESTS)
            state.unqueried_legs += len(legs)
            return {}
        if not self.planner.credits_available(1):
            state.add_reason(REASON_BUDGET_CREDITS)
            state.unqueried_legs += len(legs)
            return {}

        request = state.request
        items = [leg.query_key for leg in legs]
        raw_outcomes, stop_reason = self.planner.query_items(
            items=items,
            travel_class=request.travel_class,
            quota=request.quota,
            deadline=deadline,
        )
        if stop_reason:
            state.add_reason(PLANNER_STOP_MAP.get(stop_reason, stop_reason))

        outcomes: dict[tuple[str, str, str, str], SegmentQueryOutcome] = {}
        for leg in legs:
            outcome = raw_outcomes.get(leg.query_key)
            if outcome is not None:
                outcomes[leg.query_key] = outcome
            else:
                state.unqueried_legs += 1
        return outcomes

    def _keys_rate_limited(self) -> bool:
        pool = getattr(self.client, "key_pool", None)
        return pool is not None and pool.min_cooldown_remaining() > 5.0

    # ----------------------------------------------------------------- search

    def _ensure_hubs_to_dest(self, state: _RunState) -> None:
        """Cache {station_code: onward_train_count} reaching destination (0 credits)."""
        if state.hubs_to_dest is not None:
            return
        store = getattr(self.planner, "_timetable_store", None)
        if store is not None:
            state.hubs_to_dest = store.get_stations_reaching(
                state.request.to_station,
                travel_class=state.request.travel_class,
            )
        else:
            state.hubs_to_dest = {}

    def _prioritize_first_legs(self, state: _RunState, legs: list[_Leg]) -> list[_Leg]:
        """Rank first legs so major destination-reaching hubs & diverse corridor stations go first."""
        if len(legs) <= state.budgets.batch_size:
            return legs
        self._ensure_hubs_to_dest(state)
        hubs = state.hubs_to_dest or {}
        via = set(state.request.via_stations)
        cls = state.request.travel_class

        def _tier(leg: _Leg) -> int:
            if leg.to_code in via:
                return 0
            if leg.to_code in hubs and leg.to_code in state.seed_corridor:
                return 1
            if leg.to_code in hubs:
                return 2
            if leg.to_code in state.seed_corridor:
                return 3
            return 4

        def _class_penalty(leg: _Leg) -> int:
            sched = state.pool.get(leg.train_number)
            if sched and sched.classes and cls in sched.classes:
                return 0
            return 1

        prelim = sorted(
            legs,
            key=lambda lg: (
                _tier(lg),
                _class_penalty(lg),
                -hubs.get(lg.to_code, 0),
                lg.arrival_dt,
                lg.train_number,
            ),
        )
        per_station_counts: dict[str, int] = {}
        ranked: list[tuple[int, int, int, int, datetime, str, _Leg]] = []
        for lg in prelim:
            occ = per_station_counts.get(lg.to_code, 0)
            per_station_counts[lg.to_code] = occ + 1
            ranked.append(
                (
                    _tier(lg),
                    occ,
                    _class_penalty(lg),
                    -hubs.get(lg.to_code, 0),
                    lg.arrival_dt,
                    lg.train_number,
                    lg,
                )
            )
        ranked.sort(key=lambda item: item[:6])
        return [item[6] for item in ranked]

    def _select_explore_legs(self, state: _RunState, legs: list[_Leg]) -> list[_Leg]:
        """Order candidate onward legs so destination-finishing legs always run first."""
        if len(legs) <= state.budgets.batch_size:
            return legs
        self._ensure_hubs_to_dest(state)
        hubs = state.hubs_to_dest or {}
        via = set(state.request.via_stations)
        destination = state.request.to_station
        cls = state.request.travel_class

        def _score(lg: _Leg):
            if lg.to_code == destination:
                tier = 0
            elif lg.to_code in via:
                tier = 1
            elif lg.to_code in hubs:
                tier = 2
            else:
                tier = 3
            sched = state.pool.get(lg.train_number)
            cls_pen = 0 if (sched and sched.classes and cls in sched.classes) else 1
            return (tier, cls_pen, -hubs.get(lg.to_code, 0), lg.arrival_dt, lg.train_number)

        ordered = sorted(legs, key=_score)
        return ordered[: state.budgets.batch_size]

    def _complete_two_leg_journeys(
        self, state: _RunState, horizon_end: datetime, deadline: float
    ) -> None:
        """Directly check (hub -> destination) completion legs for reached depth-1 hubs
        before _explore() spends credits expanding multi-hop detours.
        """
        if not state.events:
            return
        self._ensure_hubs_to_dest(state)
        destination = state.request.to_station
        budgets = state.budgets
        buffer_delta = timedelta(minutes=budgets.min_transfer_buffer_minutes)
        wait_delta = timedelta(minutes=budgets.max_transfer_wait_minutes)

        # Group depth-1 paths in the event heap by arrival station.
        by_station: list[tuple[datetime, _Path]] = []
        for arr_dt, _, path in sorted(state.events, key=lambda e: (e[0], e[1])):
            st = path.legs[-1].to_code
            if st == destination or len(path.legs) != 1:
                continue
            by_station.append((arr_dt, path))

        if not by_station:
            return

        # Discover (hub -> destination) pairs for reached hubs and fetch their schedules.
        cand_items: list[tuple[_Path, _Leg]] = []
        seen_station_window: set[tuple[str, str, str]] = set()
        for arr_dt, path in by_station:
            if time.monotonic() > deadline:
                state.add_reason(REASON_TIME)
                break
            station = path.legs[-1].to_code
            window_start = arr_dt + buffer_delta
            window_end = arr_dt + wait_delta
            win_key = (station, window_start.date().isoformat(), window_end.date().isoformat())
            if win_key not in seen_station_window:
                seen_station_window.add(win_key)
                day = window_start.date()
                while day <= window_end.date():
                    iso_d = day.isoformat()
                    if (station, destination, iso_d) not in state.pair_done:
                        if state.pairs_used >= budgets.max_discovery_pairs:
                            state.add_reason(REASON_PAIR_LIMIT)
                            break
                        if not self.planner.requests_available() or not self.planner.credits_available(1):
                            break
                        self._discover_pair(state, station, destination, iso_d)
                    day += timedelta(days=1)
                self._fetch_schedules(state, deadline)

            legs = self._generate_legs(
                state,
                station=station,
                visited=set(path.visited),
                last_train=path.legs[-1].train_number,
                window_start=window_start,
                window_end=window_end,
                first_leg=False,
                horizon_end=horizon_end,
            )
            for leg in legs:
                if leg.to_code == destination:
                    cand_items.append((path, leg))

        if not cand_items:
            return

        # Prioritize SL-confirmed, fastest-arriving completion legs up to one batch (or 25).
        cls = state.request.travel_class
        cand_items.sort(
            key=lambda item: (
                0 if (state.pool.get(item[1].train_number) and state.pool[item[1].train_number].classes and cls in state.pool[item[1].train_number].classes) else 1,
                1 if item[0].unverified_legs else 0,
                item[1].arrival_dt,
                item[1].train_number,
            )
        )
        # Deduplicate leg query_keys while keeping diverse hubs.
        selected_legs: list[_Leg] = []
        seen_keys: set[tuple[str, str, str, str]] = set()
        for _, lg in cand_items:
            if lg.query_key not in seen_keys:
                seen_keys.add(lg.query_key)
                selected_legs.append(lg)
                if len(selected_legs) >= budgets.batch_size:
                    break

        outcomes = self._query_legs(state, selected_legs, deadline)
        seen_signatures: set[tuple] = {
            tuple((lg.train_number, lg.from_seq, lg.to_seq, lg.boarding_date) for lg in p.legs)
            for p in state.journeys + state.candidates
        }
        for path, leg in cand_items:
            outcome = outcomes.get(leg.query_key)
            if outcome is None:
                continue
            status = outcome.status
            if status == InternalAvailabilityStatus.CONFIRMED_AVAILABLE:
                extended = path.extended(leg, outcome, False)
            elif status in (InternalAvailabilityStatus.PROVIDER_ERROR, InternalAvailabilityStatus.UNKNOWN):
                extended = path.extended(leg, outcome, True)
            else:
                continue
            sig = tuple((lg.train_number, lg.from_seq, lg.to_seq, lg.boarding_date) for lg in extended.legs)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            if extended.unverified_legs:
                state.candidates.append(extended)
            else:
                state.journeys.append(extended)

    def _push_and_explore(self, state: _RunState, horizon_end: datetime, deadline: float) -> None:
        """Evaluate first legs and run chronological BFS, staging large leg sets in waves."""
        request = state.request
        source = request.from_station
        report = state.station_report(source)
        window_start = datetime.combine(request.journey_date, time_cls(0, 0), tzinfo=IST)
        window_end = datetime.combine(request.journey_date, time_cls(23, 59, 59), tzinfo=IST)
        all_legs = self._generate_legs(
            state, station=source, visited={source}, last_train=None,
            window_start=window_start, window_end=window_end,
            first_leg=True, horizon_end=horizon_end,
        )
        all_legs = self._prioritize_first_legs(state, all_legs)

        if len(all_legs) <= state.budgets.batch_size:
            self._evaluate_first_leg_batch(state, all_legs, deadline, report)
            self._explore(state, horizon_end, deadline)
            return

        idx = 0
        batch_limit = state.budgets.batch_size
        while idx < len(all_legs):
            if time.monotonic() > deadline:
                state.add_reason(REASON_TIME)
                break
            if not self.planner.requests_available():
                state.add_reason(REASON_BUDGET_REQUESTS)
                break
            credits_left = max(0, self.planner.budgets.max_credits - self.planner.stats["estimated_credits"])
            if credits_left <= 0:
                state.add_reason(REASON_BUDGET_CREDITS)
                break
            # Always reserve at least ~50% of remaining credits for 2-leg destination completion.
            wave_cap = min(batch_limit, max(4, credits_left // 2))
            wave = all_legs[idx : idx + wave_cap]
            idx += len(wave)
            self._evaluate_first_leg_batch(state, wave, deadline, report)
            if self._keys_rate_limited():
                break
            # Check all direct (hub -> destination) 2-leg completions first!
            self._complete_two_leg_journeys(state, horizon_end, deadline)
            if state.journeys or self._keys_rate_limited():
                if idx < len(all_legs) and not self._keys_rate_limited():
                    state.add_reason(REASON_TRAIN_LIMIT)
                break
            # Only fall back to deeper multi-hop BFS if 2-leg completions found 0 journeys.
            self._explore(state, horizon_end, deadline)
            if state.journeys or self._keys_rate_limited():
                if idx < len(all_legs) and not self._keys_rate_limited():
                    state.add_reason(REASON_TRAIN_LIMIT)
                break

    def _evaluate_first_leg_batch(
        self,
        state: _RunState,
        legs: list[_Leg],
        deadline: float,
        report: dict[str, Any],
    ) -> None:
        source = state.request.from_station
        outcomes = self._query_legs(state, legs, deadline, report)
        for leg in legs:
            outcome = outcomes.get(leg.query_key)
            if outcome is None:
                continue
            status = outcome.status
            report["legs_evaluated"] += 1
            if status == InternalAvailabilityStatus.CONFIRMED_AVAILABLE:
                report["legs_confirmed"] += 1
                seed = _Path(legs=(leg,), outcomes=(outcome,), visited=frozenset({source}))
                heapq.heappush(state.events, (leg.arrival_dt, next(state.event_counter), seed))
            elif status in (InternalAvailabilityStatus.PROVIDER_ERROR, InternalAvailabilityStatus.UNKNOWN):
                report["legs_unverified"] += 1
                seed = _Path(
                    legs=(leg,), outcomes=(outcome,), visited=frozenset({source}),
                    unverified_legs=(0,),
                )
                heapq.heappush(state.events, (leg.arrival_dt, next(state.event_counter), seed))
            else:
                report["legs_rejected"] += 1

    def _push_first_legs(self, state: _RunState, horizon_end: datetime, deadline: float) -> None:
        """Discover nothing new: evaluate every pool leg leaving the source
        on the journey date and seed the event heap."""
        request = state.request
        source = request.from_station
        report = state.station_report(source)
        window_start = datetime.combine(request.journey_date, time_cls(0, 0), tzinfo=IST)
        window_end = datetime.combine(request.journey_date, time_cls(23, 59, 59), tzinfo=IST)
        legs = self._generate_legs(
            state, station=source, visited={source}, last_train=None,
            window_start=window_start, window_end=window_end,
            first_leg=True, horizon_end=horizon_end,
        )
        self._evaluate_first_leg_batch(state, legs, deadline, report)

    def _explore(self, state: _RunState, horizon_end: datetime, deadline: float) -> None:
        """Chronological earliest-arrival expansion (time-dependent BFS)."""
        request = state.request
        destination = request.to_station
        budgets = state.budgets
        buffer_delta = timedelta(minutes=budgets.min_transfer_buffer_minutes)
        wait_delta = timedelta(minutes=budgets.max_transfer_wait_minutes)

        while state.events:
            arrival_dt, _, path = heapq.heappop(state.events)
            station = path.legs[-1].to_code
            if station == destination:
                if len(path.legs) < 2:
                    continue  # defensive: direct single-leg journeys are never results
                if path.unverified_legs:
                    state.candidates.append(path)
                else:
                    state.journeys.append(path)
                if len(state.journeys) + len(state.candidates) >= budgets.max_journeys and state.events:
                    state.add_reason(REASON_JOURNEY_LIMIT)
                    break
                continue
            if state.states_expanded >= budgets.max_expanded_states:
                if state.events:
                    state.add_reason(REASON_STATE_LIMIT)
                break
            if time.monotonic() > deadline:
                if state.events:
                    state.add_reason(REASON_TIME)
                break
            if self._keys_rate_limited():
                state.add_reason(REASON_PROVIDER)
                break
            state.states_expanded += 1
            report = state.station_report(station)
            report["events_expanded"] += 1
            if report["first_arrival_datetime"] is None:
                report["first_arrival_datetime"] = arrival_dt.isoformat()

            window_start = arrival_dt + buffer_delta
            window_end = arrival_dt + wait_delta
            self._ensure_station_discovery(state, station, window_start, window_end, deadline)
            self._fetch_schedules(state, deadline)

            legs = self._generate_legs(
                state, station=station, visited=set(path.visited),
                last_train=path.legs[-1].train_number,
                window_start=window_start, window_end=window_end,
                first_leg=False, horizon_end=horizon_end,
            )
            legs = self._select_explore_legs(state, legs)
            outcomes = self._query_legs(state, legs, deadline, report)
            for leg in legs:
                outcome = outcomes.get(leg.query_key)
                if outcome is None:
                    continue
                report["legs_evaluated"] += 1
                status = outcome.status
                if status == InternalAvailabilityStatus.CONFIRMED_AVAILABLE:
                    report["legs_confirmed"] += 1
                    heapq.heappush(
                        state.events,
                        (leg.arrival_dt, next(state.event_counter), path.extended(leg, outcome, False)),
                    )
                elif status in (InternalAvailabilityStatus.PROVIDER_ERROR, InternalAvailabilityStatus.UNKNOWN):
                    report["legs_unverified"] += 1
                    heapq.heappush(
                        state.events,
                        (leg.arrival_dt, next(state.event_counter), path.extended(leg, outcome, True)),
                    )
                else:
                    # RAC / waitlist / not available: a definitive answer -
                    # this branch is dead but nothing else is claimed.
                    report["legs_rejected"] += 1

    def _ensure_station_discovery(
        self,
        state: _RunState,
        station: str,
        window_start: datetime,
        window_end: datetime,
        deadline: float,
    ) -> None:
        """Lazily discover trains leaving `station` for the dates the
        transfer window covers (one query per (pair, date), deduplicated)."""
        if station == state.request.from_station:
            return  # seed pass already discovered the source's pairs
        # Enumerate EVERY calendar date the transfer window covers: a long
        # max-wait window can span several dates, and trains may be listed
        # per date by the provider.
        dates: list[str] = []
        day = window_start.date()
        while day <= window_end.date():
            dates.append(day.isoformat())
            day += timedelta(days=1)
        targets = self._discovery_targets(state, station)
        for iso_date in dates:
            for target in targets:
                if (station, target, iso_date) in state.pair_done:
                    continue
                if state.pairs_used >= state.budgets.max_discovery_pairs:
                    state.add_reason(REASON_PAIR_LIMIT)
                    return
                if time.monotonic() > deadline:
                    state.add_reason(REASON_TIME)
                    return
                if not self.planner.requests_available():
                    state.add_reason(REASON_BUDGET_REQUESTS)
                    return
                if not self.planner.credits_available(1):
                    state.add_reason(REASON_BUDGET_CREDITS)
                    return
                self._discover_pair(state, station, target, iso_date)

    # -------------------------------------------------------------- assembly

    def _build_journey(self, state: _RunState, path: _Path) -> TrainChangeJourneyOut:
        request = state.request
        budgets = state.budgets
        first, last = path.legs[0], path.legs[-1]
        segments: list[TrainSegmentOut] = []
        transfers: list[TransferOut] = []
        total_fare = 0.0
        fares_complete = True
        missing_fare: list[int] = []
        checked_times: list[str] = []
        oldest_updates: list[str] = []

        for index, (leg, outcome) in enumerate(zip(path.legs, path.outcomes), start=1):
            requested = outcome.availability.requested_class if outcome.availability else None
            fare_value = _fare_of(outcome)
            if fare_value is None:
                fares_complete = False
                missing_fare.append(index)
            else:
                total_fare += fare_value
            if outcome.checked_at:
                checked_times.append(outcome.checked_at)
            if requested is not None and requested.last_updated_at:
                oldest_updates.append(requested.last_updated_at)
            segments.append(
                TrainSegmentOut(
                    segment_number=index,
                    segment_type="TRAIN",
                    train_number=leg.train_number,
                    train_name=leg.train_name,
                    from_station=StationPointOut(code=leg.from_code, name=leg.from_name, sequence=leg.from_seq),
                    to_station=StationPointOut(code=leg.to_code, name=leg.to_name, sequence=leg.to_seq),
                    departure_time=leg.departure_time,
                    departure_day=leg.departure_day,
                    departure_datetime=leg.departure_dt.isoformat(),
                    arrival_time=leg.arrival_time,
                    arrival_day=leg.arrival_day,
                    arrival_datetime=leg.arrival_dt.isoformat(),
                    halt_minutes_at_to_station=leg.halt_minutes,
                    distance_km_at_to_station=leg.distance_km,
                    segment_distance_km=leg.segment_distance_km,
                    travel_class=request.travel_class,
                    quota=request.quota,
                    boarding_query_date=leg.boarding_date,
                    status=outcome.status,
                    provider_status=requested.provider_status if requested else None,
                    availability_text=requested.availability_text if requested else None,
                    available_count=requested.available_count if requested else None,
                    waitlist_count=requested.waitlist_count if requested else None,
                    rac_count=requested.rac_count if requested else None,
                    fare=requested.fare if requested else None,
                    total_fare=requested.total_fare if requested else None,
                    last_updated_at=requested.last_updated_at if requested else None,
                    checked_at=outcome.checked_at,
                    request_id=outcome.availability.request_id if outcome.availability else None,
                    booking_url=None,  # RailCore does not provide booking URLs
                    error=outcome.error,
                )
            )

        wait_total = 0
        for index in range(len(path.legs) - 1):
            leg_a, leg_b = path.legs[index], path.legs[index + 1]
            wait = int((leg_b.departure_dt - leg_a.arrival_dt).total_seconds() // 60)
            wait_total += wait
            transfers.append(
                TransferOut(
                    transfer_number=index + 1,
                    transfer_type="TRAIN_CHANGE",
                    station=StationPointOut(
                        code=leg_a.to_code, name=leg_a.to_name, sequence=leg_a.to_seq
                    ),
                    from_train=leg_a.train_number,
                    to_train=leg_b.train_number,
                    arrival_datetime=leg_a.arrival_dt.isoformat(),
                    departure_datetime=leg_b.departure_dt.isoformat(),
                    wait_minutes=wait,
                    within_min_buffer=wait >= budgets.min_transfer_buffer_minutes,
                    within_max_wait=wait <= budgets.max_transfer_wait_minutes,
                )
            )

        duration = int((last.arrival_dt - first.departure_dt).total_seconds() // 60)
        overnight = last.arrival_dt.date() > first.departure_dt.date()
        all_confirmed = not path.unverified_legs
        journey_id = (
            f"{request.journey_date.isoformat()}-"
            + "-".join(f"{leg.train_number}@{leg.from_code}" for leg in path.legs)
            + f"-{request.to_station}"
        )
        return TrainChangeJourneyOut(
            journey_id=journey_id,
            journey_type="TRAIN_CHANGE",
            journey_status=(
                JourneyVerification.AVAILABLE_COMBINATION
                if all_confirmed
                else JourneyVerification.PARTIALLY_VERIFIED
            ),
            from_station=request.from_station,
            to_station=request.to_station,
            journey_date=request.journey_date.isoformat(),
            departure=JourneyEndpointOut(
                station=StationPointOut(code=first.from_code, name=first.from_name, sequence=first.from_seq),
                time=first.departure_time,
                day=first.departure_day,
            ),
            arrival=JourneyEndpointOut(
                station=StationPointOut(code=last.to_code, name=last.to_name, sequence=last.to_seq),
                time=last.arrival_time,
                day=last.arrival_day,
            ),
            departure_datetime=first.departure_dt.isoformat(),
            arrival_datetime=last.arrival_dt.isoformat(),
            overnight=overnight,
            total_duration_minutes=duration,
            total_fare=round(total_fare, 2) if fares_complete else None,
            fares_complete=fares_complete,
            segments_missing_fare=missing_fare,
            segment_count=len(segments),
            train_changes=len(segments) - 1,
            total_transfer_wait_minutes=wait_total,
            transfers=transfers,
            segments=segments,
            all_segments_confirmed_available=all_confirmed,
            unverified_segment_numbers=[i + 1 for i in path.unverified_legs],
            availability_checked_at=max(checked_times) if checked_times else None,
            oldest_availability_update_at=min(oldest_updates) if oldest_updates else None,
        )

    @staticmethod
    def _rank_key(journey: TrainChangeJourneyOut, preference: RankingPreference):
        fare = journey.total_fare if journey.fares_complete else float("inf")
        duration = (
            journey.total_duration_minutes
            if journey.total_duration_minutes is not None
            else float("inf")
        )
        changes = journey.train_changes
        if preference == RankingPreference.CHEAPEST:
            primary = (fare, duration, changes)
        elif preference == RankingPreference.FASTEST:
            primary = (duration, fare, changes)
        else:
            primary = (changes, duration, fare)
        return (*primary, journey.journey_id)

    def _rank(
        self, journeys: list[TrainChangeJourneyOut], preference: RankingPreference
    ) -> list[TrainChangeJourneyOut]:
        return sorted(journeys, key=lambda journey: self._rank_key(journey, preference))

    # -------------------------------------------------------------- response

    def _status_message(
        self, status: TrainChangeSearchStatus, results: int, candidates: int, state: _RunState
    ) -> str:
        if status == TrainChangeSearchStatus.AVAILABLE_COMBINATION:
            message = (
                f"{results} train-change journey(s) with fully confirmed availability found. "
            )
            if state.reasons:
                message += (
                    "The search was truncated by a configured limit or a provider error "
                    "(see search_completeness.incomplete_reasons), so more journeys may exist."
                )
            return message
        if status == TrainChangeSearchStatus.SEARCH_LIMIT_REACHED:
            return (
                "A configured search limit (budget, time, horizon or exploration cap) stopped the "
                f"search before it finished: {results} fully confirmed journey(s) and {candidates} "
                "unverified candidate(s) were found. NOT all possibilities were checked."
            )
        if status == TrainChangeSearchStatus.PARTIALLY_VERIFIED:
            return (
                "No fully confirmed train-change journey was found, but schedule-valid journeys "
                f"exist whose availability could not be fully determined ({candidates} unverified "
                "candidate(s), every affected segment listed). A provider error is never treated as "
                "an unavailable seat; these journeys are not recommendations."
            )
        if status == TrainChangeSearchStatus.NO_COMPLETE_JOURNEY:
            return (
                "The search completed within its budgets, but no train-change journey was found in "
                "which every segment explicitly reports confirmed availability. Per-station details, "
                "skip reasons and rejected legs are included; unverified segments are not treated as "
                "unavailable."
            )
        if status == TrainChangeSearchStatus.NO_CANDIDATE_TRAINS:
            return (
                "No through train was found between the requested stations on the requested date, "
                "so no schedule could anchor interchange candidates. Retry with one to three "
                "via_stations hints. No availability claims are made."
            )
        return "The search could not be completed."

    def _finish(
        self,
        state: _RunState,
        started: float,
        stats_before: dict[str, int],
        credits_before: int,
    ) -> tuple[dict, int]:
        request = state.request
        budgets = state.budgets
        ranked = self._rank(
            [self._build_journey(state, path) for path in state.journeys], request.ranking_preference
        )
        results_truncated = len(ranked) > budgets.max_results
        ranked = ranked[: budgets.max_results]
        candidates = self._rank(
            [self._build_journey(state, path) for path in state.candidates],
            request.ranking_preference,
        )[: budgets.max_results]

        limit_hit = any(reason in LIMIT_REASONS for reason in state.reasons)
        if ranked:
            status = TrainChangeSearchStatus.AVAILABLE_COMBINATION
        elif limit_hit:
            status = TrainChangeSearchStatus.SEARCH_LIMIT_REACHED
        elif candidates:
            status = TrainChangeSearchStatus.PARTIALLY_VERIFIED
        elif REASON_PROVIDER in state.reasons:
            status = TrainChangeSearchStatus.PROVIDER_ERROR
        else:
            status = TrainChangeSearchStatus.NO_COMPLETE_JOURNEY
        http_status = 502 if status == TrainChangeSearchStatus.PROVIDER_ERROR else 200
        message = self._status_message(status, len(ranked), len(candidates), state)
        return self._build_response(
            state, started, stats_before, credits_before,
            status=status, http_status=http_status, message=message,
            results=ranked, candidates=candidates, results_truncated=results_truncated,
        )

    def _override_response(
        self,
        state_or_request,
        started: float,
        stats_before: dict[str, int],
        credits_before: int,
        *,
        status: TrainChangeSearchStatus,
        message: str,
        http_status: int,
    ) -> tuple[dict, int]:
        if not isinstance(state_or_request, _RunState):
            state = _RunState(state_or_request, self.budgets)
        else:
            state = state_or_request
        return self._build_response(
            state, started, stats_before, credits_before,
            status=status, http_status=http_status, message=message,
        )

    def _build_response(
        self,
        state: _RunState,
        started: float,
        stats_before: dict[str, int],
        credits_before: int,
        *,
        status: TrainChangeSearchStatus,
        http_status: int,
        message: str,
        results: list[TrainChangeJourneyOut] | None = None,
        candidates: list[TrainChangeJourneyOut] | None = None,
        results_truncated: bool = False,
    ) -> tuple[dict, int]:
        request = state.request
        budgets = state.budgets
        results = results or []
        candidates = candidates or []
        elapsed_ms = int((time.monotonic() - started) * 1000)
        planner_stats = self.planner.stats

        if state.horizon_truncated:
            state.add_reason(REASON_HORIZON)

        completeness = TrainChangeCompletenessOut(
            complete=not state.reasons,
            budget=TrainChangeBudgetUsageOut(
                requests_used=planner_stats["requests_sent"],
                requests_max=budgets.max_api_requests,
                credits_used_estimate=planner_stats["estimated_credits"],
                credits_max=budgets.max_credits,
                duration_ms=elapsed_ms,
                time_budget_ms=int(budgets.time_budget_seconds * 1000),
                discovery_pairs_used=state.pairs_used,
                discovery_pairs_max=budgets.max_discovery_pairs,
                trains_processed=len(state.pool),
                trains_total=len(state.seen_trains),
                states_expanded=state.states_expanded,
                states_max=budgets.max_expanded_states,
                journeys_found=len(state.journeys) + len(state.candidates),
                journeys_max=budgets.max_journeys,
            ),
            segments_checked=planner_stats["availability_items_sent"],
            distinct_availability_queries=planner_stats["distinct_availability_queries"],
            deduplicated_queries=planner_stats["deduplicated_queries"],
            schedule_cache_hits=planner_stats["schedule_cache_hits"],
            discovery_cache_hits=planner_stats["discovery_cache_hits"],
            incomplete_reasons=list(state.reasons),
            search_scope={
                "min_transfer_buffer_minutes": budgets.min_transfer_buffer_minutes,
                "max_transfer_wait_minutes": budgets.max_transfer_wait_minutes,
                "search_horizon_days": budgets.search_horizon_days,
                "horizon_end_date": (
                    request.journey_date + timedelta(days=budgets.search_horizon_days)
                ).isoformat(),
                "boarding_date_at_source": request.journey_date.isoformat(),
                "via_stations": list(request.via_stations),
            },
        )

        after = self.client.stats_snapshot()
        api_usage = ApiUsageOut(
            provider="railcore",
            requests_sent=after["requests_sent"] - stats_before["requests_sent"],
            successful_requests=after["successful_requests"] - stats_before["successful_requests"],
            failed_requests=after["failed_requests"] - stats_before["failed_requests"],
            credits_cost=self.client.credits_cost_total - credits_before,
            credits_remaining=self.client.credits_remaining,
            rate_limit_remaining_per_minute=self.client.rate_limit_remaining,
            rate_limit_day_remaining=self.client.rate_limit_day_remaining,
        )

        station_reports = [
            StationReportOut(**report) for report in state.station_reports.values()
        ]
        checked_at: str | None = None
        for journey in results + candidates:
            if journey.availability_checked_at and (checked_at is None or journey.availability_checked_at > checked_at):
                checked_at = journey.availability_checked_at

        all_errors = list(state.errors)
        all_errors.extend(self.planner.provider_errors)

        response = TrainChangeSearchResponse(
            success=bool(results),
            status=status,
            message=message,
            search=request.search_echo(),
            ranked_by=request.ranking_preference,
            results=results,
            total_results=len(results),
            results_truncated=results_truncated,
            unverified_candidates=candidates,
            total_unverified_candidates=len(candidates),
            direct_trains=state.direct_trains,
            stations=station_reports,
            skipped_trains=state.skip_reports,
            search_completeness=completeness,
            api_usage=api_usage.model_dump(),
            availability_checked_at=checked_at,
            errors=all_errors,
            notes=SEARCH_NOTES,
        )
        return response.model_dump(mode="json", exclude_none=True), http_status
