"""Mixed journey combination search engine (Phase 4).

Combines SAME-TRAIN seat changes (phase 2 semantics: multiple consecutive
ticket segments on one train run, every segment explicitly confirmed) with
TRAIN CHANGES at interchange stations (phase 3 semantics: validated
transfers between different trains) into ONE complete journey, in any
order and any number.

Architecture - an orchestration LAYER, not a rewrite
----------------------------------------------------
``MixedJourneyCombinationService`` subclasses the phase-3
``TrainChangeConnectionService`` and inherits its machinery verbatim:

- seed/corridor/lazy pair discovery (``_seed_discovery``,
  ``_seed_pair_pass``, ``_discover_pair``, ``_ensure_station_discovery``,
  ``_discovery_targets``),
- schedule pool, corridor learning and class filtering
  (``_fetch_schedules``, ``_learn_schedule``,
  ``_register_discovered_train``),
- schedule-validated leg generation with transfer windows, day offsets,
  running days, horizon and cycle prevention (``_generate_legs``),
- first-leg seeding (``_push_first_legs``),
- budget-aware, deduplicated, batched availability queries
  (``_query_legs`` through the shared ``SegmentQueryPlanner``).

The ONLY algorithmic addition: when a passenger alights at station X on
train T (run R), the chronological search may ALSO continue ON THE SAME
TRAIN RUN R from X (``_same_run_continuation_legs``). Such a continuation
is a SAME-TRAIN SEAT CHANGE (the passenger stays onboard through the halt;
the ticket boundary changes) - it is NOT a train transfer, so no transfer
buffer or maximum-wait applies. Boarding a different run of the same train
number is NOT a continuation and is excluded (documented limitation).

Journey model
-------------
A mixed journey is a chronological sequence of individual ticket segments.
Consecutive segments on the same train (same run) form a seat-change
group; a transition to a different train is a validated train change.
The phase-2 per-segment boarding-date semantics (IRCTC: query with the
calendar date the train departs the boarding station, advancing by one
day per overnight rollover) are preserved for every segment, including
same-train continuations.

Search
------
Same chronological earliest-arrival (time-dependent) expansion as phase 3:
arrival events are explored in arrival order; every candidate leg is
schedule-validated BEFORE any availability query; only CONFIRMED_AVAILABLE
legs extend the search; PROVIDER_ERROR / UNKNOWN legs are traversed and
reported as unverified candidates (a provider error is never treated as
unavailability). There is NO fixed maximum of seat changes or train
changes; computational budgets (requests, credits, time, pairs, states,
journeys, horizon) cap the search and are reported honestly. Duplicate
journeys are detected by segment signature; cycles are prevented by
never revisiting a station. No dominance pruning is applied: pruning
states that share (station, arrival time) would discard valid journeys
whose earlier segments differ, so the engine only uses safe rejections
(schedule-impossible connections, station revisits, horizon, budgets).

A journey of a SINGLE segment (source -> destination on one train) is a
direct train, not a combination: excluded from results, listed in
``direct_trains`` without any availability claim.
"""

from __future__ import annotations

import heapq
import logging
import time
from datetime import datetime, time as time_cls, timedelta

from ..core.config import get_settings
from ..models.schedule import TrainSchedule
from ..providers.base import InternalAvailabilityStatus
from ..providers.railcore import RailCoreClient
from ..schemas.journey_search import (
    ApiUsageOut,
    JourneyEndpointOut,
    StationPointOut,
)
from ..schemas.mixed_search import (
    MixedBudgetUsageOut,
    MixedCompletenessOut,
    MixedJourneyOut,
    MixedRankingPreference,
    MixedSearchRequest,
    MixedSearchResponse,
    MixedSearchStatus,
    MixedSegmentOut,
    MixedTransitionOut,
)
from ..schemas.train_change_search import (
    JourneyVerification,
    StationReportOut,
)
from .search_budgets import MixedJourneyBudgets
from .segment_query_planner import SegmentQueryPlanner
from .train_change_connection_service import (
    IST,
    LIMIT_REASONS,
    REASON_JOURNEY_LIMIT,
    REASON_PROVIDER,
    REASON_STATE_LIMIT,
    REASON_TIME,
    REASON_HORIZON,
    TrainChangeConnectionService,
    _hhmm_to_time,
    _fare_of,
    _Leg,
    _Path,
    _RunState,
)

logger = logging.getLogger("railroute.services.mixed_engine")

SEARCH_NOTES = [
    "These results are point-in-time availability checks, not booked tickets. RailRoute AI does not book tickets.",
    "Every segment of every confirmed result explicitly reports confirmed availability (provider status AVAILABLE). "
    "RAC, waitlist, regret, unavailable, unknown and provider errors never qualify.",
    "Journeys whose availability could not be fully verified are returned separately as unverified candidates "
    "(journey_status PARTIALLY_VERIFIED) and are never labelled confirmed. A provider error is never interpreted "
    "as an unavailable seat.",
    "A same-train seat change means consecutive ticket segments on ONE train run while the passenger stays onboard: "
    "it is NOT a train transfer and no transfer buffer applies. Indian Railways ticketing rules for split journeys "
    "on the same train must be verified before relying on them.",
    "Each train change happens at one interchange station with the exact same station code on both trains, inside "
    "the configurable minimum transfer buffer and maximum transfer wait.",
    "Availability figures can be older than the check time (provider-side caching): each segment carries its "
    "provider last_updated_at and each journey reports the oldest one.",
    "Fares are summed per segment and never estimated; when any segment fare is unknown the total fare is null and "
    "the affected segments are listed. Total waiting time counts train-change waits only (seat-change halts do not "
    "count).",
    "Direct through trains are listed for reference only: their availability is NOT checked by this engine.",
    "A mixed journey involves separate tickets on one or more trains; connection validity depends on the trains "
    "running to time. Verify railway rules and leave realistic buffers before relying on a connection.",
]


class MixedJourneyCombinationService(TrainChangeConnectionService):
    """Orchestrates the mixed same-train + train-change journey search."""

    def __init__(self, client: RailCoreClient, budgets: MixedJourneyBudgets | None = None) -> None:
        self.client = client
        self.budgets = budgets or MixedJourneyBudgets.from_settings(get_settings())
        self.planner = SegmentQueryPlanner(client, self.budgets)

    # ------------------------------------------------------------------ run

    def run(self, request: MixedSearchRequest) -> tuple[dict, int]:
        """Execute the mixed search. Returns (response_body_dict, http_status)."""
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
                request,
                started,
                stats_before,
                credits_before,
                status=MixedSearchStatus.CONFIG_ERROR,
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

        # ---- Step 1: seed discovery (S -> D), inherited from phase 3 --------
        early = self._seed_discovery(state, deadline)
        if early is not None:
            tc_status, http_status, message = early
            return self._override_response(
                state,
                started,
                stats_before,
                credits_before,
                status=MixedSearchStatus(tc_status.value),
                message=message,
                http_status=http_status,
            )

        # ---- Steps 2-3: schedules + first-leg pair discovery (inherited) ---
        self._fetch_schedules(state, deadline)
        self._seed_pair_pass(state, deadline)
        self._fetch_schedules(state, deadline)

        # ---- Step 4: first legs (inherited; direct legs excluded) -----------
        self._push_first_legs(state, horizon_end, deadline)

        # ---- Step 5: chronological mixed expansion --------------------------
        self._explore(state, horizon_end, deadline)
        if state.horizon_truncated:
            state.add_reason(REASON_HORIZON)

        # ---- Step 6: assemble ------------------------------------------------
        return self._finish(state, started, stats_before, credits_before)

    # ----------------------------------------------------------------- search

    def _explore(self, state: _RunState, horizon_end: datetime, deadline: float) -> None:
        """Chronological earliest-arrival expansion with mixed edges.

        Identical to the phase-3 expansion, plus same-run continuation
        legs: when the popped arrival event came from train T, the
        passenger may keep riding T (a seat change at the alighting
        station - not a transfer). Journeys are recorded when the
        destination is reached, deduplicated by segment signature.
        """
        request = state.request
        destination = request.to_station
        budgets = state.budgets
        buffer_delta = timedelta(minutes=budgets.min_transfer_buffer_minutes)
        wait_delta = timedelta(minutes=budgets.max_transfer_wait_minutes)
        seen_signatures: set[tuple] = set()

        while state.events:
            arrival_dt, _, path = heapq.heappop(state.events)
            station = path.legs[-1].to_code
            if station == destination:
                if len(path.legs) < 2:
                    # A single segment source -> destination is a direct
                    # train, not a combination (reported in direct_trains).
                    continue
                self._record_journey(state, path, seen_signatures)
                if (
                    len(state.journeys) + len(state.candidates) >= budgets.max_journeys
                    and state.events
                ):
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
            state.states_expanded += 1
            report = state.station_report(station)
            report["events_expanded"] += 1
            if report["first_arrival_datetime"] is None:
                report["first_arrival_datetime"] = arrival_dt.isoformat()

            # Train-change legs: validated against the transfer window.
            window_start = arrival_dt + buffer_delta
            window_end = arrival_dt + wait_delta
            self._ensure_station_discovery(state, station, window_start, window_end, deadline)
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
            # Same-run continuation legs: seat changes while staying onboard
            # (no transfer window applies).
            legs = legs + self._same_run_continuation_legs(state, path, horizon_end)
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

    # ------------------------------------------------- same-run continuation

    def _same_run_continuation_legs(
        self, state: _RunState, path: _Path, horizon_end: datetime
    ) -> list[_Leg]:
        """Seat-change legs: keep riding the SAME train run after alighting.

        The run is anchored precisely: the last leg's actual arrival
        datetime identifies the run's origin departure date, so the
        continuation belongs to the same train run (overnight-safe day
        offsets). Pure computation - no provider calls. Validation:
        actual forward stops, no station revisits, no same-station loops,
        positive travel time, horizon. No transfer window applies: the
        passenger stays onboard through the halt (phase-2 semantics).
        """
        last = path.legs[-1]
        train_number = last.train_number
        schedule: TrainSchedule | None = state.pool.get(train_number)
        if schedule is None:  # defensive: the ridden train is always in the pool
            return []
        stop_index = next(
            (i for i, stop in enumerate(schedule.stops) if stop.sequence == last.to_seq),
            None,
        )
        if stop_index is None:
            return []
        stop = schedule.stops[stop_index]
        if stop.station_code != last.to_code:  # defensive: sequence/code mismatch
            return []

        day_from = stop.day or 1
        # The run's origin departure date, anchored by the actual arrival.
        origin_date = last.arrival_dt.date() - timedelta(days=day_from - 1)
        dep_time_str = stop.departure_time or stop.arrival_time
        dep_time = _hhmm_to_time(dep_time_str)
        if dep_time is None:
            return []
        dep_dt = datetime.combine(origin_date + timedelta(days=day_from - 1), dep_time, tzinfo=IST)
        if dep_dt < last.arrival_dt:
            return []  # malformed: the train cannot depart before it arrives

        legs: list[_Leg] = []
        seen: set[tuple[str, int, int, str]] = set()
        for to_stop in schedule.stops[stop_index + 1:]:
            if to_stop.station_code == last.to_code or to_stop.is_stop is False:
                continue
            if to_stop.station_code in path.visited:
                continue  # cycle prevention: never revisit a station
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
            key = (train_number, stop.sequence, to_stop.sequence, dep_dt.date().isoformat())
            if key in seen:
                continue
            seen.add(key)
            legs.append(
                _Leg(
                    train_number=train_number,
                    train_name=schedule.train_name or state.train_names.get(train_number),
                    from_code=last.to_code,
                    from_name=stop.station_name or state.station_names.get(last.to_code),
                    from_seq=stop.sequence,
                    to_code=to_stop.station_code,
                    to_name=to_stop.station_name or state.station_names.get(to_stop.station_code),
                    to_seq=to_stop.sequence,
                    departure_time=dep_time_str,
                    departure_day=stop.day,
                    arrival_time=arr_time_str,
                    arrival_day=to_stop.day,
                    # Boarding date for the availability check (IRCTC
                    # semantics): the calendar date the train departs the
                    # seat-change station.
                    boarding_date=dep_dt.date().isoformat(),
                    departure_dt=dep_dt,
                    arrival_dt=arrival_dt,
                    halt_minutes=to_stop.halt_minutes,
                    distance_km=to_stop.distance_km,
                )
            )
        return legs

    # ------------------------------------------------------------ recording

    @staticmethod
    def _journey_signature(path: _Path) -> tuple:
        """Identity of a journey: its ordered physical ticket segments.

        Stop sequences (not just station codes) participate so that even a
        circular service that visits the same station codes twice yields
        distinguishable segments.
        """
        return tuple(
            (leg.train_number, leg.from_seq, leg.to_seq, leg.boarding_date) for leg in path.legs
        )

    def _record_journey(
        self, state: _RunState, path: _Path, seen_signatures: set[tuple]
    ) -> None:
        """Record a destination-reaching journey, deduplicated by signature."""
        signature = self._journey_signature(path)
        if signature in seen_signatures:
            return  # duplicate journey: identical segment sequence recorded once
        seen_signatures.add(signature)
        if path.unverified_legs:
            state.candidates.append(path)
        else:
            state.journeys.append(path)

    # -------------------------------------------------- defensive validation

    @staticmethod
    def _validate_mixed_sequence(state: _RunState, path: _Path) -> list[str]:
        """Re-validate the assembled journey (defensive; generation already
        enforces every rule). Returns a list of violations (empty = valid).

        Checks: endpoints, station continuity, no station revisits, same-train
        continuations are forward on the same run, and every train change
        lies inside the configured transfer window.
        """
        request = state.request
        budgets = state.budgets
        legs = path.legs
        violations: list[str] = []

        if legs[0].from_code != request.from_station:
            violations.append("journey does not start at the source station")
        if legs[-1].to_code != request.to_station:
            violations.append("journey does not end at the destination station")

        station_path = [legs[0].from_code] + [leg.to_code for leg in legs]
        if len(set(station_path)) != len(station_path):
            violations.append("a station is revisited")

        for index in range(len(legs) - 1):
            leg_a, leg_b = legs[index], legs[index + 1]
            if leg_b.from_code != leg_a.to_code:
                violations.append(f"segments {index + 1}-{index + 2} are not consecutive")
                continue
            if leg_b.train_number == leg_a.train_number:
                # Same-train seat change: must be the SAME run continuing
                # forward from the alighting stop.
                if leg_b.from_seq != leg_a.to_seq:
                    violations.append(f"seat change {index + 1} boards a different stop of the same train")
                if leg_b.departure_dt < leg_a.arrival_dt:
                    violations.append(f"seat change {index + 1} departs before the train arrives")
                if leg_b.to_seq <= leg_a.to_seq:
                    violations.append(f"seat change {index + 1} does not move forward on the train")
            else:
                wait = (leg_b.departure_dt - leg_a.arrival_dt).total_seconds() / 60.0
                if wait < budgets.min_transfer_buffer_minutes:
                    violations.append(f"train change {index + 1} is below the minimum buffer")
                if wait > budgets.max_transfer_wait_minutes:
                    violations.append(f"train change {index + 1} exceeds the maximum wait")

        return violations

    # -------------------------------------------------------------- assembly

    def _build_journey(self, state: _RunState, path: _Path) -> MixedJourneyOut | None:
        """Assemble one mixed journey from a completed path."""
        request = state.request
        budgets = state.budgets
        legs = path.legs
        first, last = legs[0], legs[-1]

        violations = self._validate_mixed_sequence(state, path)
        if violations:
            # Defensive only: generation should make this unreachable.
            logger.warning(
                "mixed journey dropped after defensive validation: %s | %s",
                "; ".join(violations),
                [(leg.train_number, leg.from_code, leg.to_code) for leg in legs],
            )
            return None

        segments: list[MixedSegmentOut] = []
        transitions: list[MixedTransitionOut] = []
        total_fare = 0.0
        fares_complete = True
        missing_fare: list[int] = []
        checked_times: list[str] = []
        oldest_updates: list[str] = []

        for index, (leg, outcome) in enumerate(zip(legs, path.outcomes), start=1):
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

            if index == 1:
                segment_type = "FIRST_SEGMENT"
            elif leg.train_number == legs[index - 2].train_number:
                segment_type = "SAME_TRAIN_SEGMENT"
            else:
                segment_type = "TRAIN_CHANGE"

            segments.append(
                MixedSegmentOut(
                    segment_number=index,
                    segment_type=segment_type,
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
                    boarding_query_date=leg.boarding_date,
                    halt_minutes_at_to_station=leg.halt_minutes,
                    distance_km_at_to_station=leg.distance_km,
                    travel_class=request.travel_class,
                    quota=request.quota,
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
        for index in range(len(legs) - 1):
            leg_a, leg_b = legs[index], legs[index + 1]
            if leg_a.train_number == leg_b.train_number:
                onboard_halt = int((leg_b.departure_dt - leg_a.arrival_dt).total_seconds() // 60)
                transitions.append(
                    MixedTransitionOut(
                        transition_number=index + 1,
                        transition_type="SAME_TRAIN_SEAT_CHANGE",
                        is_train_change=False,
                        station=StationPointOut(code=leg_a.to_code, name=leg_a.to_name, sequence=leg_a.to_seq),
                        from_train=leg_a.train_number,
                        to_train=leg_b.train_number,
                        arrival_datetime=leg_a.arrival_dt.isoformat(),
                        departure_datetime=leg_b.departure_dt.isoformat(),
                        wait_minutes=None,
                        onboard_halt_minutes=onboard_halt,
                        within_min_buffer=None,
                        within_max_wait=None,
                    )
                )
            else:
                wait = int((leg_b.departure_dt - leg_a.arrival_dt).total_seconds() // 60)
                wait_total += wait
                transitions.append(
                    MixedTransitionOut(
                        transition_number=index + 1,
                        transition_type="TRAIN_CHANGE",
                        is_train_change=True,
                        station=StationPointOut(code=leg_a.to_code, name=leg_a.to_name, sequence=leg_a.to_seq),
                        from_train=leg_a.train_number,
                        to_train=leg_b.train_number,
                        arrival_datetime=leg_a.arrival_dt.isoformat(),
                        departure_datetime=leg_b.departure_dt.isoformat(),
                        wait_minutes=wait,
                        onboard_halt_minutes=None,
                        within_min_buffer=wait >= budgets.min_transfer_buffer_minutes,
                        within_max_wait=wait <= budgets.max_transfer_wait_minutes,
                    )
                )

        train_changes = sum(1 for transition in transitions if transition.is_train_change)
        seat_changes = len(transitions) - train_changes
        distinct_trains = list(dict.fromkeys(leg.train_number for leg in legs))
        duration = int((last.arrival_dt - first.departure_dt).total_seconds() // 60)
        overnight = last.arrival_dt.date() > first.departure_dt.date()
        all_confirmed = not path.unverified_legs
        if train_changes and seat_changes:
            journey_type = "MIXED"
        elif seat_changes:
            journey_type = "SAME_TRAIN_SEGMENTS"
        else:
            journey_type = "TRAIN_CHANGE"
        journey_id = (
            f"{request.journey_date.isoformat()}-"
            + "-".join(f"{leg.train_number}@{leg.from_code}" for leg in legs)
            + f"-{request.to_station}"
        )

        return MixedJourneyOut(
            journey_id=journey_id,
            journey_type=journey_type,
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
            train_changes=train_changes,
            seat_changes=seat_changes,
            total_transfer_wait_minutes=wait_total,
            distinct_train_count=len(distinct_trains),
            distinct_trains=distinct_trains,
            transitions=transitions,
            segments=segments,
            all_segments_confirmed_available=all_confirmed,
            unverified_segment_numbers=[i + 1 for i in path.unverified_legs],
            availability_checked_at=max(checked_times) if checked_times else None,
            oldest_availability_update_at=min(oldest_updates) if oldest_updates else None,
        )

    # -------------------------------------------------------------- ranking

    @staticmethod
    def _rank_key(journey: MixedJourneyOut, preference: MixedRankingPreference):
        fare = journey.total_fare if journey.fares_complete else float("inf")
        duration = (
            journey.total_duration_minutes
            if journey.total_duration_minutes is not None
            else float("inf")
        )
        changes = journey.train_changes
        seats = journey.seat_changes
        if preference == MixedRankingPreference.CHEAPEST:
            primary = (fare, duration, changes, seats)
        elif preference == MixedRankingPreference.FASTEST:
            primary = (duration, fare, changes, seats)
        elif preference == MixedRankingPreference.FEWEST_TRAIN_CHANGES:
            primary = (changes, duration, fare, seats)
        else:
            primary = (seats, changes, duration, fare)
        return (*primary, journey.segment_count, journey.journey_id)

    def _rank(
        self, journeys: list[MixedJourneyOut], preference: MixedRankingPreference
    ) -> list[MixedJourneyOut]:
        return sorted(journeys, key=lambda journey: self._rank_key(journey, preference))

    # -------------------------------------------------------------- response

    def _status_message(
        self, status: MixedSearchStatus, results: int, candidates: int, state: _RunState
    ) -> str:
        if status == MixedSearchStatus.AVAILABLE_COMBINATION:
            message = (
                f"{results} mixed journey(s) with fully confirmed availability found "
                "(same-train seat changes and/or train changes)."
            )
            if state.reasons:
                message += (
                    " The search was truncated by a configured limit or a provider error "
                    "(see search_completeness.incomplete_reasons), so more journeys may exist."
                )
            return message
        if status == MixedSearchStatus.SEARCH_LIMIT_REACHED:
            return (
                "A configured search limit (budget, time, horizon or exploration cap) stopped the "
                f"search before it finished: {results} fully confirmed journey(s) and {candidates} "
                "unverified candidate(s) were found. NOT all possibilities were checked."
            )
        if status == MixedSearchStatus.PARTIALLY_VERIFIED:
            return (
                "No fully confirmed mixed journey was found, but schedule-valid journeys exist "
                f"whose availability could not be fully determined ({candidates} unverified "
                "candidate(s), every affected segment listed). A provider error is never treated "
                "as an unavailable seat; these journeys are not recommendations."
            )
        if status == MixedSearchStatus.NO_COMPLETE_JOURNEY:
            return (
                "The search completed within its budgets, but no mixed journey was found in which "
                "every individual segment explicitly reports confirmed availability. Per-station "
                "details, skip reasons and rejected legs are included; unverified segments are not "
                "treated as unavailable."
            )
        if status == MixedSearchStatus.NO_CANDIDATE_TRAINS:
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
        built = [self._build_journey(state, path) for path in state.journeys]
        ranked = self._rank([j for j in built if j is not None], request.ranking_preference)
        results_truncated = len(ranked) > budgets.max_results
        ranked = ranked[: budgets.max_results]
        built_candidates = [self._build_journey(state, path) for path in state.candidates]
        candidates = self._rank(
            [j for j in built_candidates if j is not None], request.ranking_preference
        )[: budgets.max_results]

        limit_hit = any(reason in LIMIT_REASONS for reason in state.reasons)
        if ranked:
            status = MixedSearchStatus.AVAILABLE_COMBINATION
        elif limit_hit:
            status = MixedSearchStatus.SEARCH_LIMIT_REACHED
        elif candidates:
            status = MixedSearchStatus.PARTIALLY_VERIFIED
        elif REASON_PROVIDER in state.reasons:
            status = MixedSearchStatus.PROVIDER_ERROR
        else:
            status = MixedSearchStatus.NO_COMPLETE_JOURNEY
        http_status = 502 if status == MixedSearchStatus.PROVIDER_ERROR else 200
        message = self._status_message(status, len(ranked), len(candidates), state)
        return self._build_response(
            state,
            started,
            stats_before,
            credits_before,
            status=status,
            http_status=http_status,
            message=message,
            results=ranked,
            candidates=candidates,
            results_truncated=results_truncated,
        )

    def _override_response(
        self,
        state_or_request,
        started: float,
        stats_before: dict[str, int],
        credits_before: int,
        *,
        status: MixedSearchStatus,
        message: str,
        http_status: int,
    ) -> tuple[dict, int]:
        if not isinstance(state_or_request, _RunState):
            state = _RunState(state_or_request, self.budgets)
        else:
            state = state_or_request
        return self._build_response(
            state,
            started,
            stats_before,
            credits_before,
            status=status,
            http_status=http_status,
            message=message,
        )

    def _build_response(
        self,
        state: _RunState,
        started: float,
        stats_before: dict[str, int],
        credits_before: int,
        *,
        status: MixedSearchStatus,
        http_status: int,
        message: str,
        results: list[MixedJourneyOut] | None = None,
        candidates: list[MixedJourneyOut] | None = None,
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

        completeness = MixedCompletenessOut(
            complete=not state.reasons,
            budget=MixedBudgetUsageOut(
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
                "engine": "mixed",
                "min_transfer_buffer_minutes": budgets.min_transfer_buffer_minutes,
                "max_transfer_wait_minutes": budgets.max_transfer_wait_minutes,
                "search_horizon_days": budgets.search_horizon_days,
                "horizon_end_date": (
                    request.journey_date + timedelta(days=budgets.search_horizon_days)
                ).isoformat(),
                "boarding_date_at_source": request.journey_date.isoformat(),
                "via_stations": list(request.via_stations),
                "seat_change_semantics": (
                    "Same-train seat changes are ticket boundaries while staying onboard one "
                    "train run (no transfer window applies); train changes are validated "
                    "transfers between different trains."
                ),
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
            if journey.availability_checked_at and (
                checked_at is None or journey.availability_checked_at > checked_at
            ):
                checked_at = journey.availability_checked_at

        all_errors = list(state.errors)
        all_errors.extend(self.planner.provider_errors)

        response = MixedSearchResponse(
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
