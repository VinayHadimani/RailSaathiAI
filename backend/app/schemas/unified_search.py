"""Request/response schemas for the unified journey search (Phase 5).

Endpoint: POST /api/v1/journeys/search

One search coordinates the three existing engines - SAME_TRAIN (phase 2
same-train combinations), TRAIN_CHANGE (phase 3 train-change connections)
and MIXED (phase 4 mixed journeys) - normalizes their outputs into ONE
unified journey representation, merges the results, removes duplicates by
segment signature (train number, from sequence, to sequence, boarding
date), re-validates availability, and ranks everything with ONE
deterministic rule. The engines, their algorithms and their standalone
endpoints are reused unchanged.

Safety rules (preserved verbatim from the earlier phases):

- Only an explicit provider AVAILABLE (normalized CONFIRMED_AVAILABLE)
  qualifies a segment for a confirmed result. RAC, waitlist, regret,
  not-available, unknown statuses and provider errors NEVER qualify.
- The unified layer recomputes ``all_segments_confirmed_available`` from
  the normalized segments themselves; a journey containing any
  non-confirmed segment is moved to ``unverified_candidates`` with its
  diagnostic information preserved and is NEVER labelled
  AVAILABLE_COMBINATION.
- A provider error is never interpreted as unavailability.
- Availability is a point-in-time check; this endpoint never books
  tickets and never claims a booking confirmation.

Deduplication identity: the ordered tuple of
``(train_number, from_sequence, to_sequence, boarding_date)`` per segment.
Journey ids and station codes alone are deliberately NOT used: different
segment partitions of the same train remain distinct journeys.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..providers.base import InternalAvailabilityStatus
from .journey_search import (
    FareComparisonOut,
    JourneyEndpointOut,
    StationPointOut,
)
from .mixed_search import MixedSearchRequest
from .segment_test import ALLOWED_CLASSES, ALLOWED_QUOTAS
from .train_change_search import DirectTrainRefOut, JourneyVerification


class UnifiedEngineSelection(str, Enum):
    """Which combination engines a unified search runs.

    The engines always run in the fixed order SAME_TRAIN, TRAIN_CHANGE,
    MIXED (the same-train engine is the cheapest and warms the shared
    schedule/discovery caches for the connection engines); the request's
    ``engines`` field selects a subset, never the order.
    """

    SAME_TRAIN = "SAME_TRAIN"
    TRAIN_CHANGE = "TRAIN_CHANGE"
    MIXED = "MIXED"

    @classmethod
    def all_engines(cls) -> list["UnifiedEngineSelection"]:
        return [cls.SAME_TRAIN, cls.TRAIN_CHANGE, cls.MIXED]


class UnifiedRankingPreference(str, Enum):
    """How unified results are ordered (ties broken deterministically).

    The same four orderings the phase 4 engine supports, recalculated over
    the MERGED result set (per-engine orderings are never concatenated).
    """

    CHEAPEST = "CHEAPEST"
    FASTEST = "FASTEST"
    FEWEST_TRAIN_CHANGES = "FEWEST_TRAIN_CHANGES"
    FEWEST_SEAT_CHANGES = "FEWEST_SEAT_CHANGES"


class UnifiedSearchStatus(str, Enum):
    """Outcome of a unified search (kept separate from success).

    Status precedence: any confirmed result -> AVAILABLE_COMBINATION; else a
    configured limit stopped the search -> SEARCH_LIMIT_REACHED; else only
    unverified candidates -> PARTIALLY_VERIFIED; else a provider failure ->
    PROVIDER_ERROR; else no candidate trains at all -> NO_CANDIDATE_TRAINS;
    else NO_COMPLETE_JOURNEY. CONFIG_ERROR when the provider is not
    configured. Partial results (some engines incomplete or failed) are
    visible through ``per_engine`` and ``search_completeness``.
    """

    AVAILABLE_COMBINATION = "AVAILABLE_COMBINATION"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    NO_COMPLETE_JOURNEY = "NO_COMPLETE_JOURNEY"
    SEARCH_LIMIT_REACHED = "SEARCH_LIMIT_REACHED"
    NO_CANDIDATE_TRAINS = "NO_CANDIDATE_TRAINS"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"


class UnifiedBudgetOverrides(BaseModel):
    """Optional per-request override of the shared budget envelope.

    Every value is optional; omitted values keep the configured unified
    defaults. The envelope is shared by ALL engines of the search: the
    counters are never reset between engines and an engine is skipped
    honestly (with a reported reason) when the envelope is exhausted.
    """

    max_api_requests: int | None = Field(
        None,
        ge=1,
        le=500,
        description="Maximum provider HTTP requests for the WHOLE unified search.",
    )
    max_credits: int | None = Field(
        None,
        ge=1,
        le=2000,
        description="Maximum estimated credits (item-count model) for the WHOLE unified search.",
    )
    time_budget_seconds: float | None = Field(
        None,
        ge=0.5,
        le=300,
        description="Wall-clock budget in seconds for the WHOLE unified search.",
    )


class UnifiedSearchRequest(MixedSearchRequest):
    """Input for the unified journey search.

    Inherits the phase 3/4 request validation (station codes, class,
    quota, booking window, via_stations hints, transfer-window overrides)
    and widens the contract with engine selection, the shared budget
    envelope and the unified result cap.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "from_station": "MYS",
                "to_station": "DWR",
                "journey_date": "2026-11-27",
                "travel_class": "3A",
                "quota": "GN",
                "ranking_preference": "CHEAPEST",
                "via_stations": [],
                "engines": ["SAME_TRAIN", "TRAIN_CHANGE", "MIXED"],
                "budget": {
                    "max_api_requests": 60,
                    "max_credits": 150,
                    "time_budget_seconds": 45,
                },
                "max_results": 30,
            }
        }
    )

    ranking_preference: UnifiedRankingPreference = Field(
        UnifiedRankingPreference.FASTEST,
        description=(
            "Result ordering over the MERGED result set: CHEAPEST, FASTEST, "
            "FEWEST_TRAIN_CHANGES or FEWEST_SEAT_CHANGES (never the "
            "per-engine orderings)."
        ),
    )
    engines: list[UnifiedEngineSelection] = Field(
        default_factory=UnifiedEngineSelection.all_engines,
        min_length=1,
        max_length=3,
        description=(
            "Which engines run (default: all three). Engines always execute in "
            "the fixed order SAME_TRAIN, TRAIN_CHANGE, MIXED."
        ),
    )
    budget: UnifiedBudgetOverrides | None = Field(
        None,
        description="Optional per-request shared budget envelope (requests, credits, time).",
    )
    max_results: int = Field(
        30,
        ge=1,
        le=100,
        description="Maximum merged journeys returned (results and unverified candidates).",
    )

    @field_validator("engines")
    @classmethod
    def _unique_engines(cls, value: list[UnifiedEngineSelection]) -> list[UnifiedEngineSelection]:
        if len(set(value)) != len(value):
            raise ValueError("engines must not contain duplicates")
        return value

    def search_echo(self) -> dict:
        """The search parameters echoed back in the response."""
        return {
            "from_station": self.from_station,
            "to_station": self.to_station,
            "journey_date": self.journey_date.isoformat(),
            "travel_class": self.travel_class,
            "quota": self.quota,
            "ranking_preference": self.ranking_preference.value,
            "via_stations": list(self.via_stations),
            "min_transfer_buffer_minutes": self.min_transfer_buffer_minutes,
            "max_transfer_wait_minutes": self.max_transfer_wait_minutes,
            "engines": [engine.value for engine in self.engines],
            "budget": (
                self.budget.model_dump(exclude_none=True) if self.budget is not None else None
            ),
            "max_results": self.max_results,
        }


# ----------------------------------------------------------------- outputs


class UnifiedSegmentOut(BaseModel):
    """One individual ticket segment of a unified journey.

    The phase 3/4 segment shape (30 fields). Phase 2 segments are
    normalized into this shape: ``segment_type`` becomes FIRST_SEGMENT /
    SAME_TRAIN_SEGMENT, ``train_name`` comes from the combination, absolute
    timestamps are derived with the phase 2 engine's own overnight
    boarding-date semantics, and ``booking_url`` stays None (RailCore
    provides none). Phase 3 segments are re-labelled FIRST_SEGMENT /
    TRAIN_CHANGE to match the unified typing.
    """

    segment_number: int = Field(description="1-based position in the journey.")
    segment_type: str = Field(
        description=(
            "FIRST_SEGMENT (journey's first ticket) | SAME_TRAIN_SEGMENT "
            "(continues the same train - seat change at boarding) | "
            "TRAIN_CHANGE (boards a different train after a transfer)."
        )
    )
    train_number: str
    train_name: str | None = None
    from_station: StationPointOut
    to_station: StationPointOut
    departure_time: str | None = Field(None, description="Scheduled departure time (HH:MM, IST).")
    departure_day: int | None = Field(None, description="Journey day number (1-based) at boarding.")
    departure_datetime: str | None = Field(
        None, description="Absolute departure timestamp (ISO 8601 with IST offset)."
    )
    arrival_time: str | None = Field(None, description="Scheduled arrival time (HH:MM, IST).")
    arrival_day: int | None = Field(None, description="Journey day number (1-based) at alighting.")
    arrival_datetime: str | None = Field(
        None, description="Absolute arrival timestamp (ISO 8601 with IST offset)."
    )
    boarding_query_date: str = Field(
        description=(
            "Calendar date used for this segment's availability check (the "
            "date the train departs the boarding station; advances by one "
            "day per overnight rollover)."
        )
    )
    halt_minutes_at_to_station: int | None = None
    distance_km_at_to_station: float | None = None
    segment_distance_km: float | None = Field(
        None, description="Actual distance travelled on this segment (to - from km)."
    )
    travel_class: str
    quota: str
    status: InternalAvailabilityStatus = Field(description="Explicit availability status of THIS segment.")
    provider_status: str | None = None
    availability_text: str | None = None
    available_count: int | None = None
    waitlist_count: int | None = None
    rac_count: int | None = None
    fare: float | None = None
    total_fare: float | None = None
    last_updated_at: str | None = None
    checked_at: str | None = Field(None, description="When this segment's availability was last checked.")
    request_id: str | None = None
    booking_url: str | None = Field(
        None, description="Provider booking URL, when the provider supplies one (RailCore does not)."
    )
    error: dict | None = Field(None, description="Provider/item failure details, never an availability claim.")


class UnifiedTransitionOut(BaseModel):
    """The connection between two consecutive ticket segments.

    A ``TRAIN_CHANGE`` transition is a genuine transfer block. A
    ``SAME_TRAIN_SEAT_CHANGE`` transition is NOT a transfer: the passenger
    stays onboard the same train run while the ticket boundary changes.
    Phase 2 combinations carry derived seat-change transitions; phase 3
    transfers and phase 4 transitions map directly.
    """

    transition_number: int = Field(description="1-based position in the journey.")
    transition_type: str = Field(
        description=(
            "TRAIN_CHANGE (genuine transfer to another train) | "
            "SAME_TRAIN_SEAT_CHANGE (same train run continues - the "
            "passenger stays onboard; never a transfer)."
        )
    )
    is_train_change: bool = Field(
        description="True only for genuine train changes; false for same-train seat changes."
    )
    station: StationPointOut = Field(description="The station where the connection happens.")
    from_train: str
    to_train: str
    arrival_datetime: str | None = None
    departure_datetime: str | None = None
    wait_minutes: int | None = Field(
        None, description="Minutes waited at the interchange (train changes only; null for seat changes)."
    )
    onboard_halt_minutes: int | None = Field(
        None, description=(
            "Minutes the train halts at the seat-change station while the "
            "passenger stays onboard (seat changes only; null for train changes)."
        )
    )
    within_min_buffer: bool | None = Field(
        None, description=(
            "True when wait >= the configured minimum transfer buffer "
            "(train changes only; null for seat changes - no buffer applies)."
        )
    )
    within_max_wait: bool | None = Field(
        None, description=(
            "True when wait <= the configured maximum transfer wait "
            "(train changes only; null for seat changes - no wait limit applies)."
        )
    )


class UnifiedJourneyOut(BaseModel):
    """A complete journey in the unified representation.

    The phase 4 journey shape (superset of phase 3) plus attribution and
    the phase 2 extras: ``source_engines`` lists every engine that
    discovered the same physical journey, ``engine_journey_ids`` preserves
    each engine's own id, and phase 2 fare-comparison information is kept
    when the journey came from (or was first found by) the same-train
    engine.
    """

    journey_id: str = Field(description="Stable id (the first-discovering engine's own id).")
    journey_type: str = Field(
        description=(
            "SAME_TRAIN_SEGMENTS (seat changes only) | TRAIN_CHANGE (train "
            "changes only) | MIXED (both kinds present)."
        )
    )
    journey_status: JourneyVerification
    from_station: str
    to_station: str
    journey_date: str = Field(description="Requested journey date (boarding date at from_station).")
    departure: JourneyEndpointOut
    arrival: JourneyEndpointOut
    departure_datetime: str | None = None
    arrival_datetime: str | None = None
    overnight: bool = Field(description="True when the journey crosses midnight.")
    total_duration_minutes: int | None = Field(
        None, description="Door-to-door duration including transfer waits (overnight-safe)."
    )
    total_distance_km: float | None = Field(
        None, description="Sum of segment distances; null when any segment distance is unknown."
    )
    total_fare: float | None = Field(None, description="Sum of segment fares; null when any fare is unknown.")
    fares_complete: bool = Field(description="False when any segment fare is unknown (never estimated).")
    segments_missing_fare: list[int] = Field(default_factory=list)
    segment_count: int = Field(description="Number of individual ticket segments.")
    train_changes: int = Field(description="Number of train changes (genuine transfers).")
    seat_changes: int = Field(
        description="Number of same-train seat changes (ticket boundaries while staying onboard)."
    )
    total_transfer_wait_minutes: int | None = Field(
        None, description="Sum of interchange waits (train changes only; seat-change halts do not count)."
    )
    distinct_train_count: int = Field(description="Number of distinct trains used.")
    distinct_trains: list[str] = Field(description="Distinct train numbers in order of first use.")
    transitions: list[UnifiedTransitionOut] = Field(default_factory=list)
    segments: list[UnifiedSegmentOut]
    all_segments_confirmed_available: bool = Field(
        description="True only when every segment is explicitly CONFIRMED_AVAILABLE (recomputed by the unified layer)."
    )
    unverified_segment_numbers: list[int] = Field(
        default_factory=list, description="Segments whose availability could NOT be determined."
    )
    availability_checked_at: str | None = None
    oldest_availability_update_at: str | None = Field(
        None, description="Oldest provider last_updated_at across segments (freshness indicator)."
    )
    source_engines: list[str] = Field(
        default_factory=list,
        description="Every engine that discovered this same physical journey (in engine order).",
    )
    engine_journey_ids: dict[str, str] = Field(
        default_factory=dict,
        description="Each source engine's own journey/combination id for this physical journey.",
    )
    train_number: str | None = Field(
        None, description="Phase 2 extra: the single train of a same-train combination."
    )
    train_run_origin_departure_date: str | None = Field(
        None,
        description="Phase 2 extra: date this train run departs its origin station.",
    )
    fare_comparison: FareComparisonOut | None = Field(
        None,
        description="Phase 2 extra: segmented fare vs the direct through-route fare of the same train.",
    )


class UnifiedEngineReportOut(BaseModel):
    """Abridged per-engine diagnostics (concise but useful).

    ``status`` is the engine's own verbatim status string (phase 2 and
    phase 3/4 use different status enums; both are preserved honestly).
    Skipped engines report SKIPPED_SHARED_BUDGET_EXHAUSTED or
    SKIPPED_TIME_BUDGET_EXHAUSTED; an engine that raised an unexpected
    exception reports ENGINE_ERROR.
    """

    engine: str = Field(description="SAME_TRAIN | TRAIN_CHANGE | MIXED.")
    status: str = Field(description="The engine's own status string (verbatim).")
    http_status: int = Field(description="HTTP status the engine's own endpoint would have returned.")
    total_results: int = Field(0, description="Fully confirmed journeys the engine returned.")
    results_before_merge: int = Field(0, description="Engine result count before merging/deduplication.")
    total_unverified_candidates: int = Field(0, description="Unverified candidates the engine returned.")
    complete: bool = Field(False, description="True only when the engine's own search was exhaustive.")
    incomplete_reasons: list[str] = Field(default_factory=list)
    message: str | None = Field(None, description="The engine's own status message (abridged context).")


class UnifiedDeduplicationOut(BaseModel):
    """Cross-engine merge report for one unified search."""

    journeys_before_merge: int = Field(description="Journey count collected from all engines.")
    journeys_after_merge: int = Field(description="Distinct physical journeys after deduplication.")
    duplicates_removed: int = Field(description="Journey copies removed (identical segment signatures).")
    duplicates_removed_unverified_candidates: int = Field(
        0, description="Duplicate removals in the unverified-candidate channel."
    )
    cross_engine_duplicates: int = Field(
        0, description="Removals where another engine had already found the same physical journey."
    )
    signature_collisions: list[dict] = Field(
        default_factory=list,
        description=(
            "Abridged collision entries: kept journey id, duplicate engine, "
            "duplicate id and the shared segment signature."
        ),
    )


class UnifiedBudgetUsageOut(BaseModel):
    """Shared budget consumption for the WHOLE unified search."""

    requests_used: int
    requests_max: int
    credits_used_estimate: int = Field(description="Item-count credit estimate (authoritative value in api_usage).")
    credits_max: int
    duration_ms: int
    time_budget_ms: int


class UnifiedCompletenessOut(BaseModel):
    """Was the unified search exhaustive, and if not, why?"""

    complete: bool
    budget: UnifiedBudgetUsageOut
    segments_checked: int = Field(description="Availability items sent to the provider across all engines.")
    distinct_availability_queries: int
    deduplicated_queries: int
    schedule_cache_hits: int = 0
    discovery_cache_hits: int = 0
    incomplete_reasons: list[str] = Field(
        default_factory=list,
        description=(
            "Aggregated reasons across engines (deduplicated), plus "
            "SHARED_BUDGET_STOPPED_ENGINE / TIME_LIMIT entries for engines "
            "skipped after the shared envelope ran out. Each reason is "
            "clearly reported, never silent."
        ),
    )
    engines_reporting_incomplete: list[str] = Field(
        default_factory=list,
        description="Engines whose own search was incomplete or skipped.",
    )
    search_scope: dict = Field(
        default_factory=dict,
        description=(
            "Configured scope actually used: engines (fixed order), transfer "
            "window (train-change engines only), horizon, via hints."
        ),
    )


class UnifiedSearchResponse(BaseModel):
    """The unified search response."""

    success: bool = Field(
        description="True only when at least one fully confirmed journey was found."
    )
    status: UnifiedSearchStatus
    message: str
    search: dict
    ranked_by: UnifiedRankingPreference
    results: list[UnifiedJourneyOut] = Field(default_factory=list)
    total_results: int = 0
    results_truncated: bool = Field(False, description="True when max_results capped the response.")
    unverified_candidates: list[UnifiedJourneyOut] = Field(
        default_factory=list,
        description=(
            "Schedule-valid journeys whose every segment was checked but at "
            "least one could NOT be verified (provider error / unknown). "
            "Never confirmed results; diagnostics preserved per segment."
        ),
    )
    total_unverified_candidates: int = 0
    deduplication: UnifiedDeduplicationOut
    per_engine: list[UnifiedEngineReportOut] = Field(
        default_factory=list,
        description="Abridged per-engine diagnostics, in engine execution order.",
    )
    direct_trains: list[DirectTrainRefOut] = Field(
        default_factory=list,
        description=(
            "Direct through trains found during discovery, unioned across "
            "engines (reference only, availability NOT checked)."
        ),
    )
    search_completeness: UnifiedCompletenessOut | None = None
    api_usage: dict | None = None
    availability_checked_at: str | None = None
    errors: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


__all__ = [
    "ALLOWED_CLASSES",
    "ALLOWED_QUOTAS",
    "UnifiedEngineSelection",
    "UnifiedRankingPreference",
    "UnifiedSearchStatus",
    "UnifiedBudgetOverrides",
    "UnifiedSearchRequest",
    "UnifiedSearchResponse",
    "UnifiedSegmentOut",
    "UnifiedTransitionOut",
    "UnifiedJourneyOut",
    "UnifiedEngineReportOut",
    "UnifiedDeduplicationOut",
    "UnifiedBudgetUsageOut",
    "UnifiedCompletenessOut",
]
