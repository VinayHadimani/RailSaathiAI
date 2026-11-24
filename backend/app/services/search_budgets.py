"""Configurable search budgets for the journey-combination engine.

These are COMPUTATIONAL SAFEGUARDS, not a maximum number of seat changes
or segments (spec section 9: no hardcoded change limits). When a budget
stops the search early, the engine reports a partial-search status and
never claims all possibilities were checked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..core.config import Settings


@dataclass(frozen=True)
class SearchBudgets:
    """Budget knobs for one search execution (all optional, all clamped >= 0)."""

    max_trains: int = 5
    max_api_requests: int = 40
    max_credits: int = 80
    time_budget_seconds: float = 30.0
    max_combinations_per_train: int = 25
    max_results: int = 50
    schedule_cache_ttl_seconds: float = 600.0
    discovery_cache_ttl_seconds: float = 300.0
    min_request_interval_seconds: float = 0.0
    batch_size: int = 25

    def __post_init__(self) -> None:
        # Clamp to sane, non-negative values.
        object.__setattr__(self, "max_trains", max(1, self.max_trains))
        object.__setattr__(self, "max_api_requests", max(1, self.max_api_requests))
        object.__setattr__(self, "max_credits", max(1, self.max_credits))
        object.__setattr__(self, "time_budget_seconds", max(0.5, self.time_budget_seconds))
        object.__setattr__(self, "max_combinations_per_train", max(1, self.max_combinations_per_train))
        object.__setattr__(self, "max_results", max(1, self.max_results))
        object.__setattr__(
            self, "batch_size", max(1, min(25, self.batch_size))
        )  # provider limit: 25 items per batch

    @classmethod
    def from_settings(cls, settings: Settings) -> "SearchBudgets":
        """Build the default budgets from application settings."""
        return cls(
            max_trains=settings.search_max_trains,
            max_api_requests=settings.search_max_api_requests,
            max_credits=settings.search_max_credits,
            time_budget_seconds=settings.search_time_budget_seconds,
            max_combinations_per_train=settings.search_max_combinations_per_train,
            max_results=settings.search_max_results,
            schedule_cache_ttl_seconds=settings.search_schedule_cache_ttl_seconds,
            discovery_cache_ttl_seconds=settings.search_discovery_cache_ttl_seconds,
            min_request_interval_seconds=settings.search_min_request_interval_seconds,
        )


@dataclass(frozen=True)
class TrainChangeBudgets:
    """Budget knobs for one train-change search execution.

    Same philosophy as ``SearchBudgets``: these are COMPUTATIONAL
    SAFEGUARDS, never a maximum number of train changes (spec: no fixed
    change limit). Transfer buffers and the horizon are search-scope
    parameters; when a budget stops the search early, the engine reports
    a partial-search status and never claims all possibilities were
    checked.

    The fields used by ``SegmentQueryPlanner`` (max_api_requests,
    max_credits, batch_size, *_cache_ttl_seconds,
    min_request_interval_seconds) intentionally mirror ``SearchBudgets``
    so the existing planner, its budget enforcement and its caches are
    reused unchanged.
    """

    # Transfer validation scope (also overridable per request).
    min_transfer_buffer_minutes: int = 30
    max_transfer_wait_minutes: int = 720
    search_horizon_days: int = 2

    # Discovery / pool safeguards.
    max_discovery_pairs: int = 20  # (station-pair, date) queries per search
    max_trains: int = 15  # train schedules held in the pool per search
    max_expanded_states: int = 300  # arrival events expanded per search
    max_journeys: int = 25  # complete journeys recorded per search
    max_results: int = 20  # results returned (and cap for candidates)

    # Provider usage safeguards (enforced by SegmentQueryPlanner).
    max_api_requests: int = 40
    max_credits: int = 120
    time_budget_seconds: float = 30.0
    schedule_cache_ttl_seconds: float = 600.0
    discovery_cache_ttl_seconds: float = 300.0
    min_request_interval_seconds: float = 0.0
    batch_size: int = 25

    def __post_init__(self) -> None:
        # Clamp to sane values; keep invariants (wait >= buffer) intact.
        object.__setattr__(
            self, "min_transfer_buffer_minutes", max(0, self.min_transfer_buffer_minutes)
        )
        object.__setattr__(
            self,
            "max_transfer_wait_minutes",
            max(self.min_transfer_buffer_minutes, self.max_transfer_wait_minutes, 1),
        )
        object.__setattr__(self, "search_horizon_days", max(0, min(7, self.search_horizon_days)))
        object.__setattr__(self, "max_discovery_pairs", max(1, self.max_discovery_pairs))
        object.__setattr__(self, "max_trains", max(1, self.max_trains))
        object.__setattr__(self, "max_expanded_states", max(1, self.max_expanded_states))
        object.__setattr__(self, "max_journeys", max(1, self.max_journeys))
        object.__setattr__(self, "max_results", max(1, self.max_results))
        object.__setattr__(self, "max_api_requests", max(1, self.max_api_requests))
        object.__setattr__(self, "max_credits", max(1, self.max_credits))
        object.__setattr__(self, "time_budget_seconds", max(0.5, self.time_budget_seconds))
        object.__setattr__(self, "batch_size", max(1, min(25, self.batch_size)))

    def with_overrides(
        self,
        min_transfer_buffer_minutes: int | None,
        max_transfer_wait_minutes: int | None,
    ) -> "TrainChangeBudgets":
        """Apply per-request transfer-window overrides on a copy."""
        buffer = (
            self.min_transfer_buffer_minutes
            if min_transfer_buffer_minutes is None
            else max(0, int(min_transfer_buffer_minutes))
        )
        wait = (
            self.max_transfer_wait_minutes
            if max_transfer_wait_minutes is None
            else max(1, int(max_transfer_wait_minutes))
        )
        if wait < buffer:
            raise ValueError("max_transfer_wait_minutes must be >= min_transfer_buffer_minutes")
        return replace(self, min_transfer_buffer_minutes=buffer, max_transfer_wait_minutes=wait)

    @classmethod
    def from_settings(cls, settings: Settings) -> "TrainChangeBudgets":
        """Build the default budgets from application settings."""
        return cls(
            min_transfer_buffer_minutes=settings.train_change_min_transfer_buffer_minutes,
            max_transfer_wait_minutes=settings.train_change_max_transfer_wait_minutes,
            search_horizon_days=settings.train_change_search_horizon_days,
            max_discovery_pairs=settings.train_change_max_discovery_pairs,
            max_trains=settings.train_change_max_trains,
            max_api_requests=settings.train_change_max_api_requests,
            max_credits=settings.train_change_max_credits,
            time_budget_seconds=settings.train_change_time_budget_seconds,
            max_expanded_states=settings.train_change_max_expanded_states,
            max_journeys=settings.train_change_max_journeys,
            max_results=settings.train_change_max_results,
            schedule_cache_ttl_seconds=settings.search_schedule_cache_ttl_seconds,
            discovery_cache_ttl_seconds=settings.search_discovery_cache_ttl_seconds,
            min_request_interval_seconds=settings.search_min_request_interval_seconds,
        )


@dataclass(frozen=True)
class MixedJourneyBudgets(TrainChangeBudgets):
    """Budget knobs for one mixed-journey search execution (Phase 4).

    A thin subclass of ``TrainChangeBudgets``: the mixed engine reuses the
    train-change engine's connection machinery (including its planner-
    compatible budget fields and per-request transfer-window overrides), so
    the knobs are identical - only their default values come from the
    ``mixed_*`` settings. As always these are COMPUTATIONAL SAFEGUARDS:
    never a maximum number of seat changes or train changes (the engine
    explores journeys of ANY shape within these budgets and reports
    truncation honestly).
    """

    @classmethod
    def from_settings(cls, settings: Settings) -> "MixedJourneyBudgets":
        """Build the default budgets from the mixed_* application settings."""
        return cls(
            min_transfer_buffer_minutes=settings.mixed_min_transfer_buffer_minutes,
            max_transfer_wait_minutes=settings.mixed_max_transfer_wait_minutes,
            search_horizon_days=settings.mixed_search_horizon_days,
            max_discovery_pairs=settings.mixed_max_discovery_pairs,
            max_trains=settings.mixed_max_trains,
            max_api_requests=settings.mixed_max_api_requests,
            max_credits=settings.mixed_max_credits,
            time_budget_seconds=settings.mixed_time_budget_seconds,
            max_expanded_states=settings.mixed_max_expanded_states,
            max_journeys=settings.mixed_max_journeys,
            max_results=settings.mixed_max_results,
            schedule_cache_ttl_seconds=settings.search_schedule_cache_ttl_seconds,
            discovery_cache_ttl_seconds=settings.search_discovery_cache_ttl_seconds,
            min_request_interval_seconds=settings.search_min_request_interval_seconds,
        )


@dataclass(frozen=True)
class UnifiedJourneyBudgets:
    """ONE shared budget envelope for a whole unified search (Phase 5).

    The same counters gate the phase 2 (same-train), phase 3 (train-change)
    and phase 4 (mixed) engines inside ONE unified search:

    - ``max_api_requests`` and ``max_credits`` are enforced by the shared
      ``SegmentQueryPlanner`` BEFORE any provider call is sent, for every
      engine, and the counters are NEVER reset when another engine starts.
    - ``time_budget_seconds`` is one wall-clock deadline for the entire
      unified search: each engine receives only the time that remains, and
      an engine whose turn arrives after the deadline is skipped with an
      honestly reported reason.

    The fields used by ``SegmentQueryPlanner`` (max_api_requests,
    max_credits, batch_size, *_cache_ttl_seconds,
    min_request_interval_seconds) intentionally mirror the per-engine budget
    classes so the planner's budget enforcement and caches are reused
    unchanged. Per-engine exploration caps (trains, discovery pairs,
    states, journeys) remain engine-local safeguards sourced from the
    engine settings; this envelope governs only the shared provider usage
    and the total time.

    As everywhere else in this codebase these are COMPUTATIONAL
    SAFEGUARDS, never a maximum number of seat changes or train changes:
    when the envelope stops the search early the unified response reports
    it explicitly and never claims all possibilities were checked.
    """

    max_api_requests: int = 60
    max_credits: int = 150
    time_budget_seconds: float = 45.0
    max_results: int = 30
    schedule_cache_ttl_seconds: float = 600.0
    discovery_cache_ttl_seconds: float = 300.0
    min_request_interval_seconds: float = 0.0
    batch_size: int = 25

    def __post_init__(self) -> None:
        # Clamp to sane, non-negative values (provider limit: 25 items per
        # availability batch).
        object.__setattr__(self, "max_api_requests", max(1, self.max_api_requests))
        object.__setattr__(self, "max_credits", max(1, self.max_credits))
        object.__setattr__(self, "time_budget_seconds", max(0.5, self.time_budget_seconds))
        object.__setattr__(self, "max_results", max(1, self.max_results))
        object.__setattr__(self, "batch_size", max(1, min(25, self.batch_size)))

    @classmethod
    def from_settings(cls, settings: Settings) -> "UnifiedJourneyBudgets":
        """Build the default shared envelope from the unified_* settings."""
        return cls(
            max_api_requests=settings.unified_max_api_requests,
            max_credits=settings.unified_max_credits,
            time_budget_seconds=settings.unified_time_budget_seconds,
            max_results=settings.unified_max_results,
            schedule_cache_ttl_seconds=settings.unified_schedule_cache_ttl_seconds,
            discovery_cache_ttl_seconds=settings.unified_discovery_cache_ttl_seconds,
            min_request_interval_seconds=settings.unified_min_request_interval_seconds,
        )
