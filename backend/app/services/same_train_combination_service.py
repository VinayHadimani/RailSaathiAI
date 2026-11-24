"""Same-train segment combination search engine (Phase 2).

Given source, destination, journey date, class and quota, find trains whose
complete source -> destination journey can be covered by MULTIPLE
consecutive station-to-station segments on the SAME train, with EVERY
segment explicitly reporting confirmed availability.

Pipeline (spec sections 8-13, 18):

1. Candidate discovery:   GET /routes/trains (match=station, 1 credit)
2. Candidate filtering:   travel class offered; (running day checked
                          precisely after the schedule is fetched)
3. Schedule:              per candidate, cached (1 credit, TTL cache)
4. Window:                the train's scheduled stops from source to
                          destination, in forward order
5. Availability BFS:      batched, deduplicated segment queries; a stop
                          is expanded only when some confirmed partition
                          reaches it (query pruning), and per-segment
                          boarding dates are computed from day offsets so
                          overnight segments query the correct train run
6. Enumeration:           DFS over the confirmed-segment graph produces
                          every valid partition of ANY size (no fixed
                          segment/seat-change limit), capped only by a
                          reported enumeration budget
7. Metrics + ranking:     fare sum, duration via day offsets, seat
                          changes, direct-fare comparison; ranked by
                          cheapest / fastest / fewest changes

Safety rules (spec sections 7, 10, 11, 17, 18):
- Only CONFIRMED_AVAILABLE (explicit provider AVAILABLE) qualifies. RAC,
  waitlist, regret, not-available, unknown, class-not-offered, failed
  batch items and provider errors NEVER qualify.
- A provider error is never interpreted as unavailability.
- Availability is a point-in-time check, never a booked ticket.
- Split-ticket combinations are reported as requiring railway-rule
  verification, never as officially permitted.
- Budget exhaustion is reported as a partial search, never silently.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from ..core.config import get_settings
from ..models.schedule import ScheduleStop, TrainSchedule
from ..providers.base import (
    InternalAvailabilityStatus,
    ProviderClientError,
    ProviderNotConfiguredError,
    RailwayProviderError,
)
from ..providers.railcore import RailCoreClient
from ..schemas.journey_search import (
    ApiUsageOut,
    BudgetUsageOut,
    CombinationOut,
    DirectSegmentOut,
    FareComparisonOut,
    JourneyEndpointOut,
    RankingPreference,
    SameTrainSearchRequest,
    SameTrainSearchResponse,
    SearchCompletenessOut,
    SearchStatus,
    SegmentDetailOut,
    StationPointOut,
    TrainSearchReportOut,
)
from .search_budgets import SearchBudgets
from .segment_query_planner import (
    STOP_BUDGET_CREDITS,
    STOP_BUDGET_REQUESTS,
    STOP_PROVIDER_ERROR,
    STOP_TIME_LIMIT,
    SegmentQueryPlanner,
    SegmentQueryOutcome,
    _BudgetExhaustedError,
)

logger = logging.getLogger("railroute.services.same_train_engine")

WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

SEARCH_NOTES = [
    "These results are point-in-time availability checks, not booked tickets. RailRoute AI does not book tickets.",
    "Every segment of every returned combination explicitly reports confirmed availability (provider status AVAILABLE). "
    "RAC, waitlist, regret, unavailable, unknown and provider errors never qualify.",
    "A combination of separately available segment tickets on one train is NOT an officially confirmed booking "
    "permission: Indian Railways ticketing rules for split journeys on the same train must be verified before "
    "relying on this.",
    "Availability figures can be older than the check time (provider-side caching): each segment carries its "
    "provider last_updated_at and each combination reports the oldest one.",
    "A provider error is never interpreted as unavailability; affected possibilities are reported as unverified.",
    "Segment fares can exceed the direct through-route fare; every result includes a fare comparison when the "
    "direct fare is known.",
]

# Map planner stop reasons -> search incompleteness reasons.
STOP_REASON_MAP = {
    STOP_BUDGET_REQUESTS: "BUDGET_REQUESTS_EXHAUSTED",
    STOP_BUDGET_CREDITS: "BUDGET_CREDITS_EXHAUSTED",
    STOP_TIME_LIMIT: "TIME_LIMIT_REACHED",
    STOP_PROVIDER_ERROR: "PROVIDER_ERROR",
}


def _weekday_code(day) -> str:
    return WEEKDAY_CODES[day.weekday()]


def _hhmm_to_minutes(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def _fare_of(outcome: SegmentQueryOutcome) -> float | None:
    """The ticket cost of a segment (total_fare preferred over fare)."""
    requested = outcome.availability.requested_class if outcome.availability else None
    if requested is None:
        return None
    if requested.total_fare is not None:
        return requested.total_fare
    return requested.fare


class SameTrainCombinationService:
    """Orchestrates the same-train combination search."""

    def __init__(self, client: RailCoreClient, budgets: SearchBudgets | None = None) -> None:
        self.client = client
        self.budgets = budgets or SearchBudgets.from_settings(get_settings())
        self.planner = SegmentQueryPlanner(client, self.budgets)

    # ------------------------------------------------------------------ run

    def run(self, request: SameTrainSearchRequest) -> tuple[dict, int]:
        """Execute the search. Returns (response_body_dict, http_status)."""
        started = time.monotonic()
        deadline = started + self.budgets.time_budget_seconds
        stats_before = self.client.stats_snapshot()
        credits_before = self.client.credits_cost_total
        errors: list[dict] = []

        # ---- Step 1: candidate discovery -----------------------------------
        try:
            route = self.planner.get_trains_between(
                from_station=request.from_station,
                to_station=request.to_station,
                journey_date=request.journey_date.isoformat(),
                quota=request.quota,
            )
        except ProviderNotConfiguredError as exc:
            return self._build(
                status=SearchStatus.CONFIG_ERROR,
                message=exc.message,
                request=request,
                errors=[exc.to_dict()],
                started=started,
                stats_before=stats_before,
                credits_before=credits_before,
            ), 503
        except ProviderClientError as exc:
            if exc.code == "NO_TRAINS_FOUND":
                return self._build(
                    status=SearchStatus.NO_CANDIDATE_TRAINS,
                    message=(
                        f"RailCore reports no trains running between {request.from_station} and "
                        f"{request.to_station} on {request.journey_date.isoformat()}. "
                        "No availability claims are made."
                    ),
                    request=request,
                    errors=[exc.to_dict()],
                    started=started,
                    stats_before=stats_before,
                    credits_before=credits_before,
                ), 200
            errors.append(exc.to_dict())
            return self._provider_error_response(request, errors, started, stats_before, credits_before), 502
        except _BudgetExhaustedError:
            return self._build(
                status=SearchStatus.PARTIAL_BUDGET_EXHAUSTED,
                message="The search budget was exhausted before candidate discovery could run.",
                request=request,
                errors=errors,
                started=started,
                stats_before=stats_before,
                credits_before=credits_before,
                incomplete_reasons=["BUDGET_REQUESTS_EXHAUSTED"],
            ), 200
        except RailwayProviderError as exc:
            errors.append(exc.to_dict())
            return self._provider_error_response(request, errors, started, stats_before, credits_before), 502

        # ---- Step 2: candidate pre-filtering -------------------------------
        # Class filter is safe before the schedule fetch (train-level fact).
        # Running-day checks happen precisely AFTER the schedule is fetched
        # (day offsets are needed to know the run's origin departure date).
        eligible = []
        reports: list[TrainSearchReportOut] = []
        for train in route.trains:
            if train.classes and request.travel_class not in train.classes:
                reports.append(
                    TrainSearchReportOut(
                        train_number=train.train_number,
                        train_name=train.train_name,
                        processed=False,
                        skip_reason="CLASS_NOT_OFFERED",
                        skip_detail=(
                            f"Train does not carry class {request.travel_class} "
                            f"(classes: {', '.join(train.classes)})."
                        ),
                    )
                )
                continue
            eligible.append(train)

        train_limit_hit = len(eligible) > self.budgets.max_trains
        selected = eligible[: self.budgets.max_trains]
        for train in eligible[self.budgets.max_trains:]:
            reports.append(
                TrainSearchReportOut(
                    train_number=train.train_number,
                    train_name=train.train_name,
                    processed=False,
                    skip_reason="TRAIN_LIMIT_REACHED",
                    skip_detail=(
                        f"Search budget allows {self.budgets.max_trains} trains per search; "
                        "this candidate was not processed."
                    ),
                )
            )

        # ---- Steps 3-6: per-train search ------------------------------------
        combinations: list[CombinationOut] = []
        incomplete_reasons: list[str] = []
        if train_limit_hit:
            incomplete_reasons.append("TRAIN_LIMIT_REACHED")
        seen_combination_ids: set[str] = set()

        for index, train in enumerate(selected):
            now = time.monotonic()
            if now > deadline:
                incomplete_reasons.append("TIME_LIMIT_REACHED")
                reports.extend(self._unprocessed_reports(selected[index:], "BUDGET_EXHAUSTED", "Search time budget reached."))
                break
            if not self.planner.requests_available() or not self.planner.credits_available(1):
                incomplete_reasons.append("BUDGET_REQUESTS_EXHAUSTED")
                reports.extend(self._unprocessed_reports(selected[index:], "BUDGET_EXHAUSTED", "API request/credit budget exhausted."))
                break

            report, train_combinations, stop_reason = self._search_train(
                train, request, deadline, errors
            )
            if stop_reason is not None:
                reason = STOP_REASON_MAP.get(stop_reason)
                if reason and reason not in incomplete_reasons:
                    incomplete_reasons.append(reason)
            for combination in train_combinations:
                combination.combination_id = self._unique_id(
                    combination.combination_id, seen_combination_ids
                )
                seen_combination_ids.add(combination.combination_id)
            combinations.extend(train_combinations)
            reports.append(report)

        # ---- Step 7: ranking + result cap ------------------------------------
        ranked = self._rank(combinations, request.ranking_preference)
        enumeration_truncated = any(report.combinations_truncated for report in reports)
        results_truncated = len(ranked) > self.budgets.max_results or enumeration_truncated
        if len(ranked) > self.budgets.max_results:
            ranked = ranked[: self.budgets.max_results]

        if incomplete_reasons:
            if "PROVIDER_ERROR" in incomplete_reasons:
                status = SearchStatus.PARTIAL_PROVIDER_ERROR
            elif "TIME_LIMIT_REACHED" in incomplete_reasons:
                status = SearchStatus.PARTIAL_TIME_LIMIT
            else:
                status = SearchStatus.PARTIAL_BUDGET_EXHAUSTED
        elif ranked:
            status = SearchStatus.COMPLETED
        else:
            status = SearchStatus.COMPLETED_NO_COMBINATIONS

        message = self._status_message(status, len(ranked), reports)

        body = self._build(
            status=status,
            message=message,
            request=request,
            results=ranked,
            results_truncated=results_truncated,
            reports=reports,
            incomplete_reasons=incomplete_reasons,
            errors=errors,
            started=started,
            stats_before=stats_before,
            credits_before=credits_before,
        )
        return body, 200

    # ----------------------------------------------------------- per-train

    def _search_train(
        self,
        route_train,
        request: SameTrainSearchRequest,
        deadline: float,
        errors: list[dict],
    ) -> tuple[TrainSearchReportOut, list[CombinationOut], str | None]:
        """Search one candidate train. Returns (report, combinations, stop_reason)."""
        report = TrainSearchReportOut(
            train_number=route_train.train_number,
            train_name=route_train.train_name or route_train.extended_train_name,
        )

        # Schedule (cached, provider errors never fake data).
        try:
            schedule = self.planner.get_schedule(route_train.train_number)
        except _BudgetExhaustedError:
            report.skip_reason = "BUDGET_EXHAUSTED"
            report.skip_detail = "API request/credit budget exhausted before the schedule could be fetched."
            return report, [], STOP_BUDGET_REQUESTS
        except RailwayProviderError as exc:
            report.skip_reason = "SCHEDULE_FETCH_FAILED"
            report.skip_detail = "The train schedule could not be retrieved; this train is unverified, not unavailable."
            report.errors = [exc.to_dict()]
            errors.append(exc.to_dict())
            return report, [], STOP_PROVIDER_ERROR

        # Window: the train's scheduled stops from source to destination.
        src = schedule.stop_by_code(request.from_station)
        dst = schedule.stop_by_code(request.to_station)
        missing = [
            code
            for code, stop in ((request.from_station, src), (request.to_station, dst))
            if stop is None
        ]
        if missing:
            report.skip_reason = "STATIONS_NOT_ON_ROUTE"
            report.skip_detail = f"Stations not on this train's route: {', '.join(missing)}."
            return report, [], None
        assert src is not None and dst is not None
        if src.is_stop is False or dst.is_stop is False:
            report.skip_reason = "STATION_NOT_A_STOP"
            report.skip_detail = "A requested station is a pass-through point on this train (no boarding/alighting)."
            return report, [], None
        if src.sequence >= dst.sequence:
            report.skip_reason = "STATION_ORDER_INVALID"
            report.skip_detail = (
                f"{request.to_station} (sequence {dst.sequence}) is not after "
                f"{request.from_station} (sequence {src.sequence}) on this train."
            )
            return report, [], None

        window = [
            stop
            for stop in schedule.stops
            if src.sequence <= stop.sequence <= dst.sequence and stop.is_stop is not False
        ]
        if len(window) < 2:
            report.skip_reason = "STATION_NOT_A_STOP"
            report.skip_detail = "No pair of scheduled stops spans the requested journey on this train."
            return report, [], None

        # Precise running-day check: the run's origin departure date is the
        # journey date minus the source stop's day offset (overnight boarding).
        src_day = src.day or 1
        run_origin_date = request.journey_date - timedelta(days=src_day - 1)
        if schedule.running_days and _weekday_code(run_origin_date) not in schedule.running_days:
            report.skip_reason = "NOT_RUNNING_ON_DATE"
            report.skip_detail = (
                f"This train runs on {', '.join(schedule.running_days)} only; the run covering "
                f"{request.from_station} on {request.journey_date.isoformat()} would depart its origin on "
                f"{run_origin_date.isoformat()} ({_weekday_code(run_origin_date)})."
            )
            return report, [], None

        report.window_stop_count = len(window)

        # Availability BFS (batched, deduplicated, budget-aware).
        confirmed, reachable, stop_reason, segments_checked = self._expand_availability(
            route_train.train_number, window, request, deadline
        )
        report.segments_checked = segments_checked

        # Direct segment reference (used for fare comparison).
        direct_outcome = confirmed.get((0, len(window) - 1))
        if direct_outcome is not None:
            requested_class = (
                direct_outcome.availability.requested_class if direct_outcome.availability else None
            )
            report.direct_segment = DirectSegmentOut(
                status=direct_outcome.status,
                provider_status=requested_class.provider_status if requested_class else None,
                availability_text=requested_class.availability_text if requested_class else None,
                total_fare=_fare_of(direct_outcome),
                checked_at=direct_outcome.checked_at,
            )

        # Enumerate all valid partitions (no fixed segment limit, capped
        # only by a REPORTED enumeration budget).
        partitions, truncated = self._enumerate(window, confirmed, self.budgets.max_combinations_per_train)
        report.combinations_truncated = truncated

        combinations: list[CombinationOut] = []
        for boundaries in partitions:
            combinations.append(
                self._build_combination(
                    window=window,
                    boundaries=boundaries,
                    confirmed=confirmed,
                    request=request,
                    train=schedule,
                    direct_outcome=direct_outcome,
                    run_origin_date=run_origin_date,
                )
            )
        report.combinations_found = len(combinations)
        return report, combinations, stop_reason

    # ------------------------------------------------------------- BFS

    def _expand_availability(
        self,
        train_number: str,
        window: list[ScheduleStop],
        request: SameTrainSearchRequest,
        deadline: float,
    ) -> tuple[dict[tuple[int, int], SegmentQueryOutcome], set[int], str | None, int]:
        """Batched availability BFS over the stop window.

        A stop is expanded (all its outgoing segments queried) only when it
        is REACHABLE: some partition with fully confirmed segments reaches
        it. Unreachable stops' segments are never queried (query pruning).
        Returns (confirmed outcomes by index pair, reachable indices,
        stop_reason, distinct segments checked).
        """
        n = len(window)
        confirmed: dict[tuple[int, int], SegmentQueryOutcome] = {}
        reachable: set[int] = {0}
        processed: set[int] = set()
        stop_reason: str | None = None
        distinct_checked = 0

        while True:
            to_process = sorted(c for c in reachable if c < n - 1 and c not in processed)
            if not to_process:
                break
            segments: list[tuple[str, str, str]] = []
            pair_to_indices: dict[tuple[str, str], list[tuple[int, int]]] = {}
            for a in to_process:
                for b in range(a + 1, n):
                    if (a, b) in confirmed:
                        continue
                    frm, to = window[a].station_code, window[b].station_code
                    segments.append((frm, to, self._boarding_date(window, a, request.journey_date)))
                    pair_to_indices.setdefault((frm, to), []).append((a, b))
            if not segments:
                break

            outcomes, stop_reason = self.planner.query_segments(
                train_number=train_number,
                segments=segments,
                travel_class=request.travel_class,
                quota=request.quota,
                deadline=deadline,
            )
            distinct_checked += len(outcomes)
            for pair, outcome in outcomes.items():
                for a, b in pair_to_indices.get(pair, []):
                    confirmed[(a, b)] = outcome
            for (a, b), outcome in confirmed.items():
                if outcome.status == InternalAvailabilityStatus.CONFIRMED_AVAILABLE:
                    reachable.add(b)
            for a in to_process:
                if all((a, b) in confirmed for b in range(a + 1, n)):
                    processed.add(a)
            if stop_reason is not None:
                break

        return confirmed, reachable, stop_reason, distinct_checked

    @staticmethod
    def _boarding_date(window: list[ScheduleStop], index: int, journey_date) -> str:
        """Calendar date the train departs window[index] (IRCTC semantics).

        The provider refuses a date the train does not LEAVE the boarding
        station, so a segment boarded after midnight (day offset > 1) must
        be queried with the advanced date. All segments still belong to
        the SAME train run (origin departure date is fixed).
        """
        base_day = window[0].day or 1
        day = window[index].day or base_day
        return (journey_date + timedelta(days=day - base_day)).isoformat()

    # ------------------------------------------------------ enumeration

    @staticmethod
    def _enumerate(
        window: list[ScheduleStop],
        confirmed: dict[tuple[int, int], SegmentQueryOutcome],
        cap: int,
    ) -> tuple[list[list[int]], bool]:
        """Enumerate every valid partition of ANY size, fewest segments first.

        Iterative-deepening DFS over the confirmed-segment graph: partitions
        with exactly 2 segments are produced first, then 3, and so on. When
        the enumeration budget (cap) is hit, the SIMPLEST combinations have
        already been produced - the most useful ones survive truncation.
        Dead branches prune early (a stop is only extended along confirmed
        segments), so the work per level is bounded by confirmed paths.

        Returns (boundary index lists, truncated flag). The direct single
        segment (boundaries [0, n-1]) is excluded - combinations have at
        least two segments by definition; the direct segment is reported
        separately as the fare/availability reference.
        """
        n = len(window)
        results: list[list[int]] = []
        truncated = False

        def dfs(a: int, boundaries: list[int], limit: int) -> None:
            nonlocal truncated
            if a == n - 1:
                # Record only partitions of EXACTLY `limit` segments;
                # shallower ones were recorded at their own level.
                if len(boundaries) == limit + 1 and len(boundaries) >= 3:
                    results.append(list(boundaries))
                return
            if len(boundaries) > limit:
                return  # extending would exceed the segment limit
            for b in range(a + 1, n):
                if len(results) >= cap:
                    truncated = True
                    return
                outcome = confirmed.get((a, b))
                if outcome is not None and outcome.status == InternalAvailabilityStatus.CONFIRMED_AVAILABLE:
                    boundaries.append(b)
                    dfs(b, boundaries, limit)
                    boundaries.pop()

        # Segment counts 2 .. n-1 (a window of n stops spans at most n-1
        # segments). No fixed cap on the number of segments/seat changes.
        for limit in range(2, n):
            dfs(0, [0], limit)
            if truncated or len(results) >= cap:
                break
        return results, truncated

    # ------------------------------------------------------------ metrics

    def _build_combination(
        self,
        *,
        window: list[ScheduleStop],
        boundaries: list[int],
        confirmed: dict[tuple[int, int], SegmentQueryOutcome],
        request: SameTrainSearchRequest,
        train: TrainSchedule,
        direct_outcome: SegmentQueryOutcome | None,
        run_origin_date,
    ) -> CombinationOut:
        segments: list[SegmentDetailOut] = []
        total_fare = 0.0
        fares_complete = True
        missing_fare_segments: list[int] = []
        checked_times: list[str] = []
        oldest_updates: list[str] = []

        for t in range(len(boundaries) - 1):
            a, b = boundaries[t], boundaries[t + 1]
            stop_from, stop_to = window[a], window[b]
            outcome = confirmed[(a, b)]
            requested_class = outcome.availability.requested_class if outcome.availability else None

            fare_value = _fare_of(outcome)
            if fare_value is None:
                fares_complete = False
                missing_fare_segments.append(t + 1)
            else:
                total_fare += fare_value
            if outcome.checked_at:
                checked_times.append(outcome.checked_at)
            if requested_class is not None and requested_class.last_updated_at:
                oldest_updates.append(requested_class.last_updated_at)

            segments.append(
                SegmentDetailOut(
                    segment_number=t + 1,
                    train_number=train.train_number,
                    from_station=StationPointOut(
                        code=stop_from.station_code, name=stop_from.station_name, sequence=stop_from.sequence
                    ),
                    to_station=StationPointOut(
                        code=stop_to.station_code, name=stop_to.station_name, sequence=stop_to.sequence
                    ),
                    departure_time=stop_from.departure_time,
                    departure_day=stop_from.day,
                    arrival_time=stop_to.arrival_time,
                    arrival_day=stop_to.day,
                    halt_minutes_at_to_station=stop_to.halt_minutes,
                    distance_km_at_to_station=stop_to.distance_km,
                    travel_class=request.travel_class,
                    quota=request.quota,
                    boarding_query_date=outcome.journey_date,
                    status=outcome.status,
                    provider_status=requested_class.provider_status if requested_class else None,
                    availability_text=requested_class.availability_text if requested_class else None,
                    available_count=requested_class.available_count if requested_class else None,
                    waitlist_count=requested_class.waitlist_count if requested_class else None,
                    rac_count=requested_class.rac_count if requested_class else None,
                    fare=requested_class.fare if requested_class else None,
                    total_fare=requested_class.total_fare if requested_class else None,
                    last_updated_at=requested_class.last_updated_at if requested_class else None,
                    checked_at=outcome.checked_at,
                    request_id=outcome.availability.request_id if outcome.availability else None,
                    error=outcome.error,
                )
            )

        first, last = window[0], window[-1]
        duration = self._duration_minutes(first, last)
        overnight = (last.day or 1) > (first.day or 1)
        fare_comparison = self._fare_comparison(direct_outcome, total_fare if fares_complete else None)
        combination_id = (
            f"{train.train_number}-{request.journey_date.isoformat()}-"
            + "-".join(window[i].station_code for i in boundaries)
        )

        return CombinationOut(
            combination_id=combination_id,
            journey_type="SAME_TRAIN_SEGMENTS",
            train_number=train.train_number,
            train_name=train.train_name,
            journey_date=request.journey_date.isoformat(),
            train_run_origin_departure_date=run_origin_date.isoformat(),
            segments=segments,
            segment_count=len(segments),
            seat_changes=len(segments) - 1,
            departure=JourneyEndpointOut(
                station=StationPointOut(code=first.station_code, name=first.station_name, sequence=first.sequence),
                time=first.departure_time,
                day=first.day,
            ),
            arrival=JourneyEndpointOut(
                station=StationPointOut(code=last.station_code, name=last.station_name, sequence=last.sequence),
                time=last.arrival_time,
                day=last.day,
            ),
            overnight=overnight,
            total_duration_minutes=duration,
            total_fare=round(total_fare, 2) if fares_complete else None,
            fares_complete=fares_complete,
            segments_missing_fare=missing_fare_segments,
            fare_comparison=fare_comparison,
            all_segments_confirmed_available=True,
            availability_checked_at=max(checked_times) if checked_times else None,
            oldest_availability_update_at=min(oldest_updates) if oldest_updates else None,
        )

    @staticmethod
    def _duration_minutes(first: ScheduleStop, last: ScheduleStop) -> int | None:
        """Total journey duration using day offsets (overnight-safe)."""
        dep = _hhmm_to_minutes(first.departure_time or first.arrival_time)
        arr = _hhmm_to_minutes(last.arrival_time or last.departure_time)
        if dep is None or arr is None:
            return None
        dep_day = first.day or 1
        arr_day = last.day or 1
        return (arr_day - dep_day) * 1440 + arr - dep

    @staticmethod
    def _fare_comparison(
        direct_outcome: SegmentQueryOutcome | None, total_fare: float | None
    ) -> FareComparisonOut | None:
        if direct_outcome is None:
            return None
        requested_class = (
            direct_outcome.availability.requested_class if direct_outcome.availability else None
        )
        direct_fare = _fare_of(direct_outcome)
        comparison = FareComparisonOut(
            direct_segment_status=direct_outcome.status,
            direct_availability_text=requested_class.availability_text if requested_class else None,
            direct_total_fare=direct_fare,
        )
        if direct_fare is not None and total_fare is not None:
            difference = round(total_fare - direct_fare, 2)
            comparison.difference = difference
            if direct_fare > 0:
                comparison.difference_percent = round(difference / direct_fare * 100, 1)
            comparison.note = (
                "The segmented tickets cost more than the direct through-route ticket."
                if difference > 0
                else "The segmented tickets do not cost more than the direct through-route ticket."
            )
        else:
            comparison.note = "Direct through-route fare unknown; fare comparison not available."
        return comparison

    # ------------------------------------------------------------ ranking

    @staticmethod
    def _rank(combinations: list[CombinationOut], preference: RankingPreference) -> list[CombinationOut]:
        """Order results by the requested preference.

        Ties are broken deterministically (fare, duration, segment count,
        train number, combination id). Note: for combinations of the SAME
        train the duration is identical (same departure/arrival), so
        FASTEST differentiates between trains, not within one train.
        """

        def key(combination: CombinationOut):
            fare = combination.total_fare if combination.fares_complete else float("inf")
            duration = (
                combination.total_duration_minutes
                if combination.total_duration_minutes is not None
                else float("inf")
            )
            segments = combination.segment_count
            if preference == RankingPreference.CHEAPEST:
                primary = (fare, duration, segments)
            elif preference == RankingPreference.FASTEST:
                primary = (duration, fare, segments)
            else:
                primary = (segments, fare, duration)
            return (*primary, combination.train_number, combination.combination_id)

        return sorted(combinations, key=key)

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _unique_id(candidate: str, seen: set[str]) -> str:
        if candidate not in seen:
            return candidate
        suffix = 2
        while f"{candidate}-{suffix}" in seen:
            suffix += 1
        return f"{candidate}-{suffix}"

    @staticmethod
    def _unprocessed_reports(trains, skip_reason: str, detail: str) -> list[TrainSearchReportOut]:
        return [
            TrainSearchReportOut(
                train_number=train.train_number,
                train_name=train.train_name,
                processed=False,
                skip_reason=skip_reason,
                skip_detail=detail,
            )
            for train in trains
        ]

    @staticmethod
    def _status_message(status: SearchStatus, result_count: int, reports: list[TrainSearchReportOut]) -> str:
        processed = sum(1 for report in reports if report.processed)
        if status == SearchStatus.COMPLETED:
            return (
                f"{result_count} same-train combination(s) with fully confirmed availability found "
                f"across {processed} searched train(s)."
            )
        if status == SearchStatus.COMPLETED_NO_COMBINATIONS:
            return (
                "The search completed within its budgets, but no train offers a multi-segment "
                "combination in which every segment explicitly reports confirmed availability. "
                "Per-train details and skip reasons are included; unverified segments are not "
                "treated as unavailable."
            )
        if status == SearchStatus.PARTIAL_BUDGET_EXHAUSTED:
            return (
                f"The search stopped early because a configured budget (trains, API requests or "
                f"credits) was exhausted. {result_count} fully confirmed combination(s) were found "
                "before the budget ran out; NOT all possibilities were checked."
            )
        if status == SearchStatus.PARTIAL_TIME_LIMIT:
            return (
                f"The search stopped early at its configured time budget. {result_count} fully "
                "confirmed combination(s) were found; NOT all possibilities were checked."
            )
        if status == SearchStatus.PARTIAL_PROVIDER_ERROR:
            return (
                f"At least one provider call failed during the search. {result_count} fully "
                "confirmed combination(s) were found; affected trains/segments are reported as "
                "unverified, NOT as unavailable, and NOT all possibilities could be checked."
            )
        return "Search finished."

    def _provider_error_response(
        self, request, errors, started, stats_before, credits_before
    ) -> dict:
        return self._build(
            status=SearchStatus.PROVIDER_ERROR,
            message=(
                "Candidate train discovery failed at the provider, so no availability claims "
                "are made for this search."
            ),
            request=request,
            errors=errors,
            started=started,
            stats_before=stats_before,
            credits_before=credits_before,
        )

    # ------------------------------------------------------------ response

    def _build(
        self,
        *,
        status: SearchStatus,
        message: str,
        request: SameTrainSearchRequest,
        results: list[CombinationOut] | None = None,
        results_truncated: bool = False,
        reports: list[TrainSearchReportOut] | None = None,
        incomplete_reasons: list[str] | None = None,
        errors: list[dict] | None = None,
        started: float,
        stats_before: dict[str, int],
        credits_before: int,
    ) -> dict:
        results = results or []
        reports = reports or []
        incomplete_reasons = incomplete_reasons or []
        elapsed_ms = int((time.monotonic() - started) * 1000)

        processed = sum(1 for report in reports if report.processed)
        planner_stats = self.planner.stats
        completeness = SearchCompletenessOut(
            complete=not incomplete_reasons,
            budget=BudgetUsageOut(
                requests_used=planner_stats["requests_sent"],
                requests_max=self.budgets.max_api_requests,
                credits_used_estimate=planner_stats["estimated_credits"],
                credits_max=self.budgets.max_credits,
                duration_ms=elapsed_ms,
                time_budget_ms=int(self.budgets.time_budget_seconds * 1000),
                trains_processed=processed,
                trains_total=len(reports),
            ),
            segments_checked=planner_stats["availability_items_sent"],
            distinct_availability_queries=planner_stats["distinct_availability_queries"],
            deduplicated_queries=planner_stats["deduplicated_queries"],
            combinations_found=len(results),
            schedule_cache_hits=planner_stats["schedule_cache_hits"],
            discovery_cache_hits=planner_stats["discovery_cache_hits"],
            incomplete_reasons=incomplete_reasons,
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

        all_errors = list(errors or [])
        all_errors.extend(self.planner.provider_errors)

        checked_at: str | None = None
        for report in reports:
            if report.direct_segment is not None and report.direct_segment.checked_at:
                candidate = report.direct_segment.checked_at
                if checked_at is None or candidate > checked_at:
                    checked_at = candidate
        for combination in results:
            if combination.availability_checked_at and (
                checked_at is None or combination.availability_checked_at > checked_at
            ):
                checked_at = combination.availability_checked_at

        response = SameTrainSearchResponse(
            success=bool(results),
            status=status,
            message=message,
            search=request.search_echo(),
            ranked_by=request.ranking_preference,
            results=results,
            total_results=len(results),
            results_truncated=results_truncated,
            candidates=reports,
            search_completeness=completeness,
            api_usage=api_usage,
            availability_checked_at=checked_at,
            errors=all_errors,
            notes=SEARCH_NOTES,
        )
        return response.model_dump(mode="json", exclude_none=True)
