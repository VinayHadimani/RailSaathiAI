"""Unified journey orchestration service (Phase 5).

One user search coordinates the three existing engines - the phase 2
``SameTrainCombinationService`` (SAME_TRAIN), the phase 3
``TrainChangeConnectionService`` (TRAIN_CHANGE) and the phase 4
``MixedJourneyCombinationService`` (MIXED) - and returns ONE merged,
deduplicated, re-validated and re-ranked result set.

Architecture - an orchestration LAYER, not a rewrite
----------------------------------------------------
The engines' services, algorithms and schemas are reused unchanged
(constructor compatibility preserved: each engine is constructed exactly
as its own endpoint constructs it, then ONE shared ``SegmentQueryPlanner``
is injected post-construction in place of the engine's per-instance
planner). The shared planner gives the unified search:

- ONE global budget envelope (``UnifiedJourneyBudgets``): the planner
  enforces maximum API requests and estimated credits BEFORE any provider
  call, for every engine, and the counters are never reset between
  engines;
- ONE cross-engine availability memo, so the same physical availability
  item queried by two engines costs ONE credit total (the remaining
  credit-saving opportunity the phase 5 audit identified);
- the shared schedule/discovery caches all engines already share.

Wall-clock: ONE deadline for the whole unified search. Each engine
receives budgets whose time slice is only the time that REMAINS, and an
engine whose turn arrives after the shared envelope (requests, credits or
time) is exhausted is SKIPPED with an honestly reported reason - never
silently dropped, and the global budget is never exceeded even when an
engine fails or times out.

Pipeline
--------
1. Run the selected engines in the fixed order SAME_TRAIN, TRAIN_CHANGE,
   MIXED (the same-train engine is the cheapest and warms the shared
   schedule/discovery caches for the connection engines).
2. Normalize each engine's response into the unified journey
   representation (``journey_normalizer``).
3. Merge and deduplicate by segment signature
   ``(train_number, from_sequence, to_sequence, boarding_date)``: the
   same physical journey discovered by several engines appears once, with
   every source engine recorded and ``duplicates_removed`` incremented;
   different segment partitions remain distinct.
4. Re-validate every merged journey (defensive: endpoints, continuity,
   forward movement, no station revisits, transfer-window compliance) and
   RECOMPUTE ``all_segments_confirmed_available`` from the segments
   themselves: only journeys whose every segment is explicitly
   CONFIRMED_AVAILABLE stay in ``results``; anything else moves to
   ``unverified_candidates`` with its diagnostics preserved (a provider
   error is never an unavailable seat, and an unverified journey is never
   labelled AVAILABLE_COMBINATION).
5. Re-rank the merged result set with ONE deterministic rule (primary
   requested metric, secondary metrics, segment count, journey id) - the
   per-engine orderings are never concatenated. Unknown fares rank below
   known fares for CHEAPEST; fares are never estimated.
6. Report honestly: per-engine abridged diagnostics, the merge report,
   aggregate completeness with shared budget usage, one api_usage block
   over the whole search, and per-engine failure isolation (one engine
   failing never suppresses the other engines' confirmed results).
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from ..core.config import get_settings
from ..providers.base import InternalAvailabilityStatus
from ..providers.railcore import RailCoreClient
from ..schemas.journey_search import ApiUsageOut, RankingPreference, SameTrainSearchRequest
from ..schemas.mixed_search import MixedRankingPreference, MixedSearchRequest
from ..schemas.train_change_search import DirectTrainRefOut, JourneyVerification, TrainChangeSearchRequest
from ..schemas.unified_search import (
    UnifiedBudgetUsageOut,
    UnifiedCompletenessOut,
    UnifiedDeduplicationOut,
    UnifiedEngineReportOut,
    UnifiedEngineSelection,
    UnifiedJourneyOut,
    UnifiedRankingPreference,
    UnifiedSearchRequest,
    UnifiedSearchResponse,
    UnifiedSearchStatus,
)
from .journey_normalizer import journey_signature, normalize_engine_response
from .mixed_journey_service import MixedJourneyCombinationService
from .same_train_combination_service import SameTrainCombinationService
from .search_budgets import (
    MixedJourneyBudgets,
    SearchBudgets,
    TrainChangeBudgets,
    UnifiedJourneyBudgets,
)
from .segment_query_planner import SegmentQueryPlanner
from .train_change_connection_service import TrainChangeConnectionService

logger = logging.getLogger("railroute.services.unified_engine")

# Fixed engine execution order (see module docstring).
ENGINE_ORDER: tuple[UnifiedEngineSelection, ...] = (
    UnifiedEngineSelection.SAME_TRAIN,
    UnifiedEngineSelection.TRAIN_CHANGE,
    UnifiedEngineSelection.MIXED,
)

# An engine needs at least this much remaining time to be worth starting
# (per-engine budget clamps round the time slice up to 0.5s; starting an
# engine with less than that would overrun the unified deadline).
MIN_ENGINE_START_SECONDS = 0.5

# Engine statuses that mean "a configured limit, not a provider fault,
# stopped this engine early".
_LIMIT_ENGINE_STATUSES = {
    "SEARCH_LIMIT_REACHED",
    "PARTIAL_BUDGET_EXHAUSTED",
    "PARTIAL_TIME_LIMIT",
    "SKIPPED_SHARED_BUDGET_EXHAUSTED",
    "SKIPPED_TIME_BUDGET_EXHAUSTED",
}
# Engine statuses that mean "this engine failed at the provider (or
# crashed); its results are absent, NOT unavailable".
_FAILURE_ENGINE_STATUSES = {
    "PROVIDER_ERROR",
    "PARTIAL_PROVIDER_ERROR",
    "ENGINE_ERROR",
}

# An engine report with one of these statuses (or complete=False) makes the
# unified search incomplete: the early-exit responses of the engines can
# report complete=True with no reasons of their own (e.g. a phase 2
# discovery failure), and the unified layer never silently discards that.
_PROBLEM_ENGINE_STATUSES = _LIMIT_ENGINE_STATUSES | _FAILURE_ENGINE_STATUSES

SEARCH_NOTES = [
    "Unified search: results from the selected engines are normalized, merged and "
    "deduplicated by segment signature (train number, from sequence, to sequence, "
    "boarding date); a journey found by several engines appears once with every "
    "source engine listed. Different segment partitions remain distinct journeys.",
    "Rankings are recalculated over the merged result set with ONE deterministic "
    "tie-break (primary metric, secondary metrics, segment count, journey id); "
    "per-engine orderings are never concatenated.",
    "Journeys with unknown fares rank below journeys with known fares for CHEAPEST; "
    "fares are never estimated.",
    "The same-train engine returns only fully confirmed combinations by construction, "
    "so it can never contribute unverified candidates; unverified candidates originate "
    "from the train-change and mixed engines only.",
    "All engines share ONE budget envelope (API requests, estimated credits, "
    "wall-clock time): counters are never reset between engines, and an engine is "
    "skipped with a reported reason when the envelope is exhausted.",
    "Same-train (phase 2) absolute departure/arrival timestamps are derived with "
    "that engine's own overnight boarding-date semantics.",
    "These results are point-in-time availability checks, not booked tickets. "
    "RailRoute AI does not book tickets.",
]


class UnifiedJourneyOrchestrationService:
    """Orchestrates the unified journey search across the three engines."""

    def __init__(self, client: RailCoreClient, budgets: UnifiedJourneyBudgets | None = None) -> None:
        self.client = client
        self.budgets = budgets or UnifiedJourneyBudgets.from_settings(get_settings())

    # ------------------------------------------------------------------ run

    def run(self, request: UnifiedSearchRequest) -> tuple[dict, int]:
        """Execute the unified search. Returns (response_body_dict, http_status)."""
        budgets = self._effective_budgets(request)
        started = time.monotonic()
        deadline = started + budgets.time_budget_seconds
        stats_before = self.client.stats_snapshot()
        credits_before = self.client.credits_cost_total
        shared_planner = SegmentQueryPlanner(self.client, budgets)

        engine_reports: list[UnifiedEngineReportOut] = []
        errors: list[dict] = []
        direct_trains_by_engine: dict[str, list[dict]] = {}
        # (engine, journey, channel) in engine order; "results" channel first.
        collected: list[tuple[UnifiedEngineSelection, UnifiedJourneyOut, str]] = []

        for engine in ENGINE_ORDER:
            if engine not in request.engines:
                continue
            remaining = deadline - time.monotonic()
            skip_reason = self._shared_envelope_skip(shared_planner, remaining)
            if skip_reason is not None:
                engine_reports.append(self._skipped_report(engine, skip_reason))
                continue

            try:
                body, http_status = self._run_engine(
                    engine, request, shared_planner, budgets, remaining
                )
                results, unverified = normalize_engine_response(engine, body)
            except Exception as exc:  # engine crash: isolate, record, continue
                logger.exception("unified engine %s failed unexpectedly", engine.value)
                errors.append(
                    {
                        "code": "ENGINE_FAILURE",
                        "category": "INTERNAL",
                        "engine": engine.value,
                        "message": f"{type(exc).__name__}: {exc}",
                        "retryable": False,
                    }
                )
                engine_reports.append(
                    UnifiedEngineReportOut(
                        engine=engine.value,
                        status="ENGINE_ERROR",
                        http_status=500,
                        complete=False,
                        incomplete_reasons=["ENGINE_ERROR"],
                        message=(
                            "The engine raised an unexpected internal error; its "
                            "results are absent, not unavailable. The other engines "
                            "continued."
                        ),
                    )
                )
                continue

            direct_trains_by_engine[engine.value] = list(body.get("direct_trains") or [])
            for engine_error in body.get("errors") or []:
                entry = dict(engine_error)
                entry.setdefault("engine", engine.value)
                errors.append(entry)
            for journey in results:
                collected.append((engine, journey, "results"))
            for journey in unverified:
                collected.append((engine, journey, "unverified"))
            engine_reports.append(self._engine_report(engine, body, http_status))

        merged, results_duplicates, unverified_duplicates, cross_engine, collisions = self._dedup(
            collected
        )

        # Defensive re-validation + recomputed availability partition.
        validated: list[UnifiedJourneyOut] = []
        for journey in merged:
            violations = self._validate_journey(journey, request)
            if violations:
                errors.append(
                    {
                        "code": "UNIFIED_VALIDATION_DROPPED_JOURNEY",
                        "category": "PROTOCOL",
                        "engine": (journey.source_engines or ["UNKNOWN"])[0],
                        "journey_id": journey.journey_id,
                        "message": "; ".join(violations),
                        "retryable": False,
                    }
                )
                logger.warning("unified journey dropped after validation: %s", violations)
                continue
            validated.append(journey)

        confirmed: list[UnifiedJourneyOut] = []
        unverified_candidates: list[UnifiedJourneyOut] = []
        for journey in validated:
            all_confirmed = all(
                segment.status == InternalAvailabilityStatus.CONFIRMED_AVAILABLE
                for segment in journey.segments
            )
            journey.all_segments_confirmed_available = all_confirmed
            if all_confirmed:
                journey.journey_status = JourneyVerification.AVAILABLE_COMBINATION
                journey.unverified_segment_numbers = []
                confirmed.append(journey)
            else:
                journey.journey_status = JourneyVerification.PARTIALLY_VERIFIED
                journey.unverified_segment_numbers = [
                    segment.segment_number
                    for segment in journey.segments
                    if segment.status != InternalAvailabilityStatus.CONFIRMED_AVAILABLE
                ]
                unverified_candidates.append(journey)

        ranked = self._rank(confirmed, request.ranking_preference)
        results_truncated = len(ranked) > request.max_results
        ranked = ranked[: request.max_results]
        if len(unverified_candidates) > request.max_results:
            unverified_candidates = unverified_candidates[: request.max_results]

        status, http_status = self._aggregate_status(
            engine_reports, confirmed, unverified_candidates
        )
        message = self._status_message(status, len(ranked), len(unverified_candidates), engine_reports)

        response = UnifiedSearchResponse(
            success=bool(confirmed),
            status=status,
            message=message,
            search=request.search_echo(),
            ranked_by=request.ranking_preference,
            results=ranked,
            total_results=len(ranked),
            results_truncated=results_truncated,
            unverified_candidates=unverified_candidates,
            total_unverified_candidates=len(unverified_candidates),
            deduplication=UnifiedDeduplicationOut(
                journeys_before_merge=len(collected),
                journeys_after_merge=len(merged),
                duplicates_removed=results_duplicates + unverified_duplicates,
                duplicates_removed_unverified_candidates=unverified_duplicates,
                cross_engine_duplicates=cross_engine,
                signature_collisions=collisions,
            ),
            per_engine=engine_reports,
            direct_trains=self._union_direct_trains(direct_trains_by_engine),
            search_completeness=self._completeness(
                engine_reports, shared_planner, budgets, started, request
            ),
            api_usage=self._api_usage(stats_before, credits_before),
            availability_checked_at=self._checked_at(ranked, unverified_candidates),
            errors=errors,
            notes=SEARCH_NOTES,
        )
        return response.model_dump(mode="json", exclude_none=True), http_status

    # -------------------------------------------------------------- engines

    def _effective_budgets(self, request: UnifiedSearchRequest) -> UnifiedJourneyBudgets:
        """The shared envelope: defaults with per-request overrides applied."""
        if request.budget is None:
            return self.budgets
        overrides = request.budget.model_dump(exclude_none=True)
        if not overrides:
            return self.budgets
        return replace(self.budgets, **overrides)

    @staticmethod
    def _shared_envelope_skip(planner: SegmentQueryPlanner, remaining: float) -> str | None:
        """Why an engine cannot start, or None when it can.

        The checks run BEFORE the engine starts, so the shared envelope is
        never exceeded: an engine never even begins once the budget is
        gone, and a failing engine cannot open a hole in the envelope."""
        if remaining < MIN_ENGINE_START_SECONDS:
            return "SKIPPED_TIME_BUDGET_EXHAUSTED"
        if not planner.requests_available():
            return "SKIPPED_SHARED_BUDGET_EXHAUSTED"
        if not planner.credits_available(1):
            return "SKIPPED_SHARED_BUDGET_EXHAUSTED"
        return None

    @staticmethod
    def _skipped_report(engine: UnifiedEngineSelection, reason: str) -> UnifiedEngineReportOut:
        if reason == "SKIPPED_TIME_BUDGET_EXHAUSTED":
            incomplete = ["TIME_LIMIT_REACHED"]
            message = (
                "The shared unified time budget was exhausted before this engine "
                "could start; it did not run. NOT all possibilities were checked."
            )
        else:
            incomplete = ["BUDGET_REQUESTS_EXHAUSTED", "BUDGET_CREDITS_EXHAUSTED"]
            message = (
                "The shared unified budget (API requests or estimated credits) was "
                "exhausted before this engine could start; it did not run. NOT all "
                "possibilities were checked."
            )
        return UnifiedEngineReportOut(
            engine=engine.value,
            status=reason,
            http_status=200,
            complete=False,
            incomplete_reasons=incomplete,
            message=message,
        )

    def _run_engine(
        self,
        engine: UnifiedEngineSelection,
        request: UnifiedSearchRequest,
        shared_planner: SegmentQueryPlanner,
        budgets: UnifiedJourneyBudgets,
        remaining_seconds: float,
    ) -> tuple[dict, int]:
        """Run one engine with the shared planner injected.

        Each engine service is constructed exactly as its own endpoint
        constructs it (constructor compatibility preserved), then the
        shared planner replaces the engine's per-instance planner, so all
        engines draw from ONE budget envelope and ONE availability memo.
        Engine-local exploration caps stay sourced from the engine's own
        settings; only the shared provider usage (requests, credits) and
        the time slice follow the unified envelope.
        """
        settings = get_settings()
        if engine == UnifiedEngineSelection.SAME_TRAIN:
            engine_budgets = SearchBudgets(
                max_trains=settings.search_max_trains,
                max_api_requests=budgets.max_api_requests,
                max_credits=budgets.max_credits,
                time_budget_seconds=remaining_seconds,
                max_combinations_per_train=settings.search_max_combinations_per_train,
                max_results=max(settings.search_max_results, request.max_results),
                schedule_cache_ttl_seconds=budgets.schedule_cache_ttl_seconds,
                discovery_cache_ttl_seconds=budgets.discovery_cache_ttl_seconds,
                min_request_interval_seconds=budgets.min_request_interval_seconds,
            )
            engine_request = SameTrainSearchRequest(
                from_station=request.from_station,
                to_station=request.to_station,
                journey_date=request.journey_date,
                travel_class=request.travel_class,
                quota=request.quota,
                ranking_preference=self._phase23_preference(request.ranking_preference),
            )
            service = SameTrainCombinationService(
                self.client, engine_budgets
            )
        elif engine == UnifiedEngineSelection.TRAIN_CHANGE:
            engine_budgets = TrainChangeBudgets(
                min_transfer_buffer_minutes=settings.train_change_min_transfer_buffer_minutes,
                max_transfer_wait_minutes=settings.train_change_max_transfer_wait_minutes,
                search_horizon_days=settings.train_change_search_horizon_days,
                max_discovery_pairs=settings.train_change_max_discovery_pairs,
                max_trains=settings.train_change_max_trains,
                max_api_requests=budgets.max_api_requests,
                max_credits=budgets.max_credits,
                time_budget_seconds=remaining_seconds,
                max_expanded_states=settings.train_change_max_expanded_states,
                max_journeys=settings.train_change_max_journeys,
                max_results=max(settings.train_change_max_results, request.max_results),
                schedule_cache_ttl_seconds=budgets.schedule_cache_ttl_seconds,
                discovery_cache_ttl_seconds=budgets.discovery_cache_ttl_seconds,
                min_request_interval_seconds=budgets.min_request_interval_seconds,
            )
            engine_request = TrainChangeSearchRequest(
                from_station=request.from_station,
                to_station=request.to_station,
                journey_date=request.journey_date,
                travel_class=request.travel_class,
                quota=request.quota,
                ranking_preference=self._phase23_preference(request.ranking_preference),
                via_stations=request.via_stations,
                min_transfer_buffer_minutes=request.min_transfer_buffer_minutes,
                max_transfer_wait_minutes=request.max_transfer_wait_minutes,
            )
            service = TrainChangeConnectionService(self.client, engine_budgets)
        else:  # MIXED
            engine_budgets = MixedJourneyBudgets(
                min_transfer_buffer_minutes=settings.mixed_min_transfer_buffer_minutes,
                max_transfer_wait_minutes=settings.mixed_max_transfer_wait_minutes,
                search_horizon_days=settings.mixed_search_horizon_days,
                max_discovery_pairs=settings.mixed_max_discovery_pairs,
                max_trains=settings.mixed_max_trains,
                max_api_requests=budgets.max_api_requests,
                max_credits=budgets.max_credits,
                time_budget_seconds=remaining_seconds,
                max_expanded_states=settings.mixed_max_expanded_states,
                max_journeys=settings.mixed_max_journeys,
                max_results=max(settings.mixed_max_results, request.max_results),
                schedule_cache_ttl_seconds=budgets.schedule_cache_ttl_seconds,
                discovery_cache_ttl_seconds=budgets.discovery_cache_ttl_seconds,
                min_request_interval_seconds=budgets.min_request_interval_seconds,
            )
            engine_request = MixedSearchRequest(
                from_station=request.from_station,
                to_station=request.to_station,
                journey_date=request.journey_date,
                travel_class=request.travel_class,
                quota=request.quota,
                ranking_preference=MixedRankingPreference(request.ranking_preference.value),
                via_stations=request.via_stations,
                min_transfer_buffer_minutes=request.min_transfer_buffer_minutes,
                max_transfer_wait_minutes=request.max_transfer_wait_minutes,
            )
            service = MixedJourneyCombinationService(self.client, engine_budgets)

        # Shared planner injection: the engine's requests, credits and
        # availability memo all draw from ONE envelope shared by every
        # engine of this unified search (never reset between engines).
        service.planner = shared_planner
        return service.run(engine_request)

    @staticmethod
    def _phase23_preference(preference: UnifiedRankingPreference) -> RankingPreference:
        """Map the unified preference onto the phase 2/3 three-value enum.

        The unified ranker recomputes the final ordering from normalized
        data anyway, so this only steers each engine's internal (capped)
        ordering: both FEWEST_* preferences fall back to the engines'
        FEWEST_CHANGES (their own fewest-changes ordering).
        """
        if preference.value in ("CHEAPEST", "FASTEST"):
            return RankingPreference(preference.value)
        return RankingPreference.FEWEST_CHANGES

    @staticmethod
    def _engine_report(
        engine: UnifiedEngineSelection, body: dict, http_status: int
    ) -> UnifiedEngineReportOut:
        """Abridged per-engine diagnostics from the engine's own response."""
        completeness = body.get("search_completeness") or {}
        return UnifiedEngineReportOut(
            engine=engine.value,
            status=str(body.get("status") or "UNKNOWN"),
            http_status=http_status,
            total_results=int(body.get("total_results") or len(body.get("results") or [])),
            results_before_merge=len(body.get("results") or []),
            total_unverified_candidates=int(
                body.get("total_unverified_candidates")
                or len(body.get("unverified_candidates") or [])
            ),
            complete=bool(completeness.get("complete", False)),
            incomplete_reasons=list(completeness.get("incomplete_reasons") or []),
            message=body.get("message"),
        )

    # ------------------------------------------------------------ merge/dedup

    @staticmethod
    def _dedup(
        collected: list[tuple[UnifiedEngineSelection, UnifiedJourneyOut, str]]
    ) -> tuple[list[UnifiedJourneyOut], int, int, int, list[dict]]:
        """Deduplicate by segment signature.

        The first-discovering engine's normalized representation is kept
        (engine order: SAME_TRAIN, TRAIN_CHANGE, MIXED); every source
        engine and each engine's own journey id are recorded on the kept
        journey, and each removal increments the counters. Different
        segment partitions have different signatures and stay distinct.
        """
        kept: list[UnifiedJourneyOut] = []
        by_signature: dict[tuple, int] = {}
        results_duplicates = 0
        unverified_duplicates = 0
        cross_engine = 0
        collisions: list[dict] = []

        for engine, journey, channel in collected:
            signature = journey_signature(journey)
            existing_index = by_signature.get(signature)
            if existing_index is None:
                journey.source_engines = [engine.value]
                journey.engine_journey_ids = {engine.value: journey.journey_id}
                by_signature[signature] = len(kept)
                kept.append(journey)
                continue

            existing = kept[existing_index]
            if channel == "unverified":
                unverified_duplicates += 1
            else:
                results_duplicates += 1
            if engine.value not in existing.source_engines:
                existing.source_engines.append(engine.value)
                cross_engine += 1
            existing.engine_journey_ids[engine.value] = journey.journey_id
            collisions.append(
                {
                    "kept_journey_id": existing.journey_id,
                    "duplicate_engine": engine.value,
                    "duplicate_journey_id": journey.journey_id,
                    "channel": channel,
                    "signature": [list(part) for part in signature],
                }
            )

        return kept, results_duplicates, unverified_duplicates, cross_engine, collisions

    # ----------------------------------------------------------- validation

    @staticmethod
    def _validate_journey(journey: UnifiedJourneyOut, request: UnifiedSearchRequest) -> list[str]:
        """Re-validate one merged journey (defensive; generation already
        enforces every rule inside each engine). Returns violations.

        Checks: endpoints, segment continuity, forward movement within
        each segment, same-train seat changes continue the same stop of
        the same train, no station revisits, per-segment arrival after
        departure, and transfer-window compliance of train-change
        transitions. Boarding dates are carried by every segment (schema
        enforced)."""
        violations: list[str] = []
        segments = journey.segments
        if not segments:
            return ["journey has no segments"]

        if journey.from_station != request.from_station:
            violations.append("journey does not start at the source station")
        if journey.to_station != request.to_station:
            violations.append("journey does not end at the destination station")

        station_path = [segments[0].from_station.code]
        station_path += [segment.to_station.code for segment in segments]
        if len(set(station_path)) != len(station_path):
            violations.append("a station is revisited")

        for index, segment in enumerate(segments):
            if (
                segment.from_station.sequence is not None
                and segment.to_station.sequence is not None
                and segment.to_station.sequence <= segment.from_station.sequence
            ):
                violations.append(f"segment {index + 1} does not move forward on its train")
            departure = segment.departure_datetime
            arrival = segment.arrival_datetime
            if departure and arrival and arrival <= departure:
                violations.append(f"segment {index + 1} arrives before it departs")

        for index in range(len(segments) - 1):
            current, following = segments[index], segments[index + 1]
            if following.from_station.code != current.to_station.code:
                violations.append(f"segments {index + 1}-{index + 2} are not consecutive")
                continue
            if current.train_number == following.train_number:
                # Same-train seat change: must continue from the SAME stop.
                if (
                    current.to_station.sequence is not None
                    and following.from_station.sequence is not None
                    and following.from_station.sequence != current.to_station.sequence
                ):
                    violations.append(
                        f"seat change {index + 1} boards a different stop of the same train"
                    )

        for transition in journey.transitions:
            if transition.is_train_change and (
                transition.within_min_buffer is False or transition.within_max_wait is False
            ):
                violations.append(
                    f"train change {transition.transition_number} violates the transfer window"
                )
        return violations

    # -------------------------------------------------------------- ranking

    @staticmethod
    def _rank(
        journeys: list[UnifiedJourneyOut], preference: UnifiedRankingPreference
    ) -> list[UnifiedJourneyOut]:
        """Rank the MERGED set with ONE deterministic rule.

        Tie-break: (1) the primary requested metric, (2) the secondary
        metrics in the phase 4 order, (3) segment count, (4) journey id.
        Unknown fares rank below journeys with known fares for CHEAPEST
        (the documented inf policy shared by all engines); missing
        durations likewise; fares are never estimated."""

        def key(journey: UnifiedJourneyOut):
            fare = journey.total_fare if journey.fares_complete else float("inf")
            duration = (
                journey.total_duration_minutes
                if journey.total_duration_minutes is not None
                else float("inf")
            )
            changes = journey.train_changes
            seats = journey.seat_changes
            if preference == UnifiedRankingPreference.CHEAPEST:
                primary = (fare, duration, changes, seats)
            elif preference == UnifiedRankingPreference.FASTEST:
                primary = (duration, fare, changes, seats)
            elif preference == UnifiedRankingPreference.FEWEST_TRAIN_CHANGES:
                primary = (changes, duration, fare, seats)
            else:
                primary = (seats, changes, duration, fare)
            return (*primary, journey.segment_count, journey.journey_id)

        return sorted(journeys, key=key)

    # --------------------------------------------------------------- status

    @staticmethod
    def _aggregate_status(
        engine_reports: list[UnifiedEngineReportOut],
        confirmed: list[UnifiedJourneyOut],
        unverified: list[UnifiedJourneyOut],
    ) -> tuple[UnifiedSearchStatus, int]:
        """Unified status precedence (see UnifiedSearchStatus docstring)."""
        statuses = {report.status for report in engine_reports}
        if confirmed:
            return UnifiedSearchStatus.AVAILABLE_COMBINATION, 200
        if "CONFIG_ERROR" in statuses:
            return UnifiedSearchStatus.CONFIG_ERROR, 503
        if statuses & _LIMIT_ENGINE_STATUSES:
            return UnifiedSearchStatus.SEARCH_LIMIT_REACHED, 200
        if unverified:
            return UnifiedSearchStatus.PARTIALLY_VERIFIED, 200
        if statuses & _FAILURE_ENGINE_STATUSES:
            return UnifiedSearchStatus.PROVIDER_ERROR, 502
        if engine_reports and statuses == {"NO_CANDIDATE_TRAINS"}:
            return UnifiedSearchStatus.NO_CANDIDATE_TRAINS, 200
        return UnifiedSearchStatus.NO_COMPLETE_JOURNEY, 200

    @staticmethod
    def _status_message(
        status: UnifiedSearchStatus,
        results: int,
        candidates: int,
        engine_reports: list[UnifiedEngineReportOut],
    ) -> str:
        incomplete_engines = [
            report.engine
            for report in engine_reports
            if not report.complete or report.status in _PROBLEM_ENGINE_STATUSES
        ]
        if status == UnifiedSearchStatus.AVAILABLE_COMBINATION:
            message = (
                f"{results} fully confirmed journey(s) found across the selected "
                "engines after normalization, merging and deduplication."
            )
            if incomplete_engines:
                message += (
                    " The search was partially incomplete ("
                    + ", ".join(incomplete_engines)
                    + "): see per_engine and search_completeness.incomplete_reasons - "
                    "more journeys may exist."
                )
            return message
        if status == UnifiedSearchStatus.CONFIG_ERROR:
            return (
                "The RailCore provider is not configured, so no engine could run and "
                "no availability claims are made for this search."
            )
        if status == UnifiedSearchStatus.SEARCH_LIMIT_REACHED:
            return (
                "A configured search limit (shared budget, time, horizon or an "
                "engine exploration cap) stopped the unified search before it "
                f"finished: {results} fully confirmed journey(s) and {candidates} "
                "unverified candidate(s) were found. NOT all possibilities were "
                "checked."
            )
        if status == UnifiedSearchStatus.PARTIALLY_VERIFIED:
            return (
                "No fully confirmed journey was found, but schedule-valid journeys "
                f"exist whose availability could not be fully determined ({candidates} "
                "unverified candidate(s), every affected segment listed). A provider "
                "error is never treated as an unavailable seat; these journeys are "
                "not recommendations."
            )
        if status == UnifiedSearchStatus.PROVIDER_ERROR:
            return (
                "At least one engine failed at the provider and no engine produced "
                "confirmed results, so no availability claims are made for this "
                "search. Affected possibilities are reported as unverified, never "
                "as unavailable."
            )
        if status == UnifiedSearchStatus.NO_CANDIDATE_TRAINS:
            return (
                "No through train was found between the requested stations on the "
                "requested date, so no schedule could anchor interchange candidates. "
                "Retry with one to three via_stations hints. No availability claims "
                "are made."
            )
        return (
            "The unified search completed within its budgets, but no engine found "
            "a journey in which every segment explicitly reports confirmed "
            "availability. Per-engine details are included; unverified segments "
            "are not treated as unavailable."
        )

    # ------------------------------------------------------------- response

    @staticmethod
    def _union_direct_trains(direct_trains_by_engine: dict[str, list[dict]]) -> list[DirectTrainRefOut]:
        """Reference-only union of the engines' direct through trains.

        Direct trains carry no availability claim (the engines list them
        without checking seats), so the union is reference data only;
        duplicate entries (the same through train discovered by several
        engines) appear once."""
        seen: set[tuple] = set()
        union: list[DirectTrainRefOut] = []
        for engine in ENGINE_ORDER:
            for train in direct_trains_by_engine.get(engine.value, []):
                key = (
                    train.get("train_number"),
                    train.get("from_station_code"),
                    train.get("to_station_code"),
                    train.get("departure_time"),
                )
                if key in seen:
                    continue
                seen.add(key)
                union.append(DirectTrainRefOut(**train))
        return union

    @staticmethod
    def _completeness(
        engine_reports: list[UnifiedEngineReportOut],
        shared_planner: SegmentQueryPlanner,
        budgets: UnifiedJourneyBudgets,
        started: float,
        request: UnifiedSearchRequest,
    ) -> UnifiedCompletenessOut:
        planner_stats = shared_planner.stats
        incomplete_reasons: list[str] = []
        engines_incomplete: list[str] = []
        for report in engine_reports:
            if report.complete and report.status not in _PROBLEM_ENGINE_STATUSES:
                continue
            engines_incomplete.append(report.engine)
            reasons = list(report.incomplete_reasons)
            if not reasons:
                # The engine's own early-exit response can carry no explicit
                # reasons (e.g. a phase 2 discovery failure): the engine's
                # status itself is the honest reason, never dropped silently.
                if report.status == "ENGINE_ERROR":
                    reasons = ["ENGINE_ERROR"]
                elif report.status in _FAILURE_ENGINE_STATUSES:
                    reasons = ["PROVIDER_ERROR"]
            for reason in reasons:
                if reason not in incomplete_reasons:
                    incomplete_reasons.append(reason)
        settings = get_settings()
        effective_buffer = (
            request.min_transfer_buffer_minutes
            if request.min_transfer_buffer_minutes is not None
            else settings.train_change_min_transfer_buffer_minutes
        )
        effective_wait = (
            request.max_transfer_wait_minutes
            if request.max_transfer_wait_minutes is not None
            else settings.train_change_max_transfer_wait_minutes
        )
        return UnifiedCompletenessOut(
            complete=not incomplete_reasons and bool(engine_reports),
            budget=UnifiedBudgetUsageOut(
                requests_used=planner_stats["requests_sent"],
                requests_max=budgets.max_api_requests,
                credits_used_estimate=planner_stats["estimated_credits"],
                credits_max=budgets.max_credits,
                duration_ms=int((time.monotonic() - started) * 1000),
                time_budget_ms=int(budgets.time_budget_seconds * 1000),
            ),
            segments_checked=planner_stats["availability_items_sent"],
            distinct_availability_queries=planner_stats["distinct_availability_queries"],
            deduplicated_queries=planner_stats["deduplicated_queries"],
            schedule_cache_hits=planner_stats["schedule_cache_hits"],
            discovery_cache_hits=planner_stats["discovery_cache_hits"],
            incomplete_reasons=incomplete_reasons,
            engines_reporting_incomplete=engines_incomplete,
            search_scope={
                "engines": [engine.value for engine in ENGINE_ORDER if engine in request.engines],
                "engine_order": [engine.value for engine in ENGINE_ORDER],
                "min_transfer_buffer_minutes": effective_buffer,
                "max_transfer_wait_minutes": effective_wait,
                "transfer_window_engines": ["TRAIN_CHANGE", "MIXED"],
                "via_stations": list(request.via_stations),
            },
        )

    def _api_usage(self, stats_before: dict[str, int], credits_before: int) -> dict:
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
        return api_usage.model_dump()

    @staticmethod
    def _checked_at(
        results: list[UnifiedJourneyOut], unverified: list[UnifiedJourneyOut]
    ) -> str | None:
        checked_at: str | None = None
        for journey in results + unverified:
            if journey.availability_checked_at and (
                checked_at is None or journey.availability_checked_at > checked_at
            ):
                checked_at = journey.availability_checked_at
        return checked_at
