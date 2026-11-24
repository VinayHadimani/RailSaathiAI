"""Request/response schemas for the mixed journey search.

Endpoint: POST /api/v1/journeys/mixed/search

Given source, destination, date, class and quota, the engine finds complete
journeys that combine SAME-TRAIN seat changes (multiple consecutive ticket
segments on one train run, phase 2 semantics) and TRAIN CHANGES at
interchange stations (phase 3 semantics) in any order and any number, with
every individual ticket segment explicitly reporting confirmed
availability (provider status AVAILABLE).

Segment and transition typing (explicit, never conflated):

- Every individual ticket segment carries ``segment_type``:
  ``FIRST_SEGMENT`` (the journey's first ticket), ``SAME_TRAIN_SEGMENT``
  (this ticket continues the SAME train the passenger just alighted from -
  a seat change happened at its boarding station while staying onboard) or
  ``TRAIN_CHANGE`` (this ticket starts on a different train after a real
  transfer).
- Every gap between consecutive segments carries a transition block with
  ``transition_type``: ``SAME_TRAIN_SEAT_CHANGE`` (same train run, the
  passenger stays onboard through the halt - NOT a transfer) or
  ``TRAIN_CHANGE`` (a genuine interchange, validated against the
  configurable transfer buffer / maximum wait). Only train changes are
  transfers; a same-train seat change is never labelled a transfer.

``journey_status`` is AVAILABLE_COMBINATION only when EVERY segment is
CONFIRMED_AVAILABLE; journeys with any unverified segment are
PARTIALLY_VERIFIED and are returned separately, never as confirmed
results.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..providers.base import InternalAvailabilityStatus
from .journey_search import (
    JourneyEndpointOut,
    MAX_BOOKING_WINDOW_DAYS,
    RankingPreference,
    StationPointOut,
)
from .segment_test import ALLOWED_CLASSES, ALLOWED_QUOTAS
from .train_change_search import (
    DirectTrainRefOut,
    JourneyVerification,
    PairDiscoveryOut,
    StationReportOut,
    TrainChangeSearchRequest,
    TrainSkipReportOut,
)


class MixedRankingPreference(str, Enum):
    """How mixed-journey results are ordered (ties broken deterministically).

    Extends the three phase-2/3 preferences with FEWEST_SEAT_CHANGES.
    """

    CHEAPEST = "CHEAPEST"
    FASTEST = "FASTEST"
    FEWEST_TRAIN_CHANGES = "FEWEST_TRAIN_CHANGES"
    FEWEST_SEAT_CHANGES = "FEWEST_SEAT_CHANGES"


class MixedSearchStatus(str, Enum):
    """Outcome of a mixed-journey search (semantics preserved from the
    earlier engines; kept separate from success)."""

    AVAILABLE_COMBINATION = "AVAILABLE_COMBINATION"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    NO_COMPLETE_JOURNEY = "NO_COMPLETE_JOURNEY"
    SEARCH_LIMIT_REACHED = "SEARCH_LIMIT_REACHED"
    NO_CANDIDATE_TRAINS = "NO_CANDIDATE_TRAINS"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"


class MixedSearchRequest(TrainChangeSearchRequest):
    """Input for the mixed journey search.

    Inherits the train-change request's validation (station codes, class,
    quota, booking window, via_stations hints, transfer-window overrides)
    and only widens the ranking preference to the four mixed orderings.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "from_station": "MYS",
                "to_station": "DWR",
                "journey_date": "2026-11-27",
                "travel_class": "3A",
                "quota": "GN",
                "ranking_preference": "FASTEST",
            }
        }
    )

    ranking_preference: MixedRankingPreference = Field(
        MixedRankingPreference.FASTEST,
        description=(
            "Result ordering: CHEAPEST, FASTEST, FEWEST_TRAIN_CHANGES or "
            "FEWEST_SEAT_CHANGES."
        ),
    )


# ----------------------------------------------------------------- outputs


class MixedSegmentOut(BaseModel):
    """One individual ticket segment of a mixed journey.

    Self-contained: train, stations, timestamps (clock, day offset and
    absolute), class, quota, explicit availability status, fare and the
    boarding date actually used for the availability check (IRCTC
    semantics: the calendar date the train departs the boarding station,
    which advances by one day per overnight rollover).
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


class MixedTransitionOut(BaseModel):
    """The connection between two consecutive ticket segments.

    A ``TRAIN_CHANGE`` transition is a genuine transfer block (arrival
    train, departure train, wait, buffer compliance). A
    ``SAME_TRAIN_SEAT_CHANGE`` transition is NOT a transfer: the passenger
    stays onboard the same train through the halt while the ticket changes;
    it deliberately carries no transfer-window claims.
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


class MixedJourneyOut(BaseModel):
    """A complete mixed journey: chronological ticket segments grouped by train."""

    journey_id: str = Field(description="Stable id built from the segment sequence.")
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
    total_fare: float | None = Field(None, description="Sum of segment fares; null when any fare is unknown.")
    fares_complete: bool = Field(description="False when any segment fare is unknown (never estimated).")
    segments_missing_fare: list[int] = Field(default_factory=list)
    segment_count: int = Field(description="Number of individual ticket segments.")
    train_changes: int = Field(description="Number of train changes (genuine transfers).")
    seat_changes: int = Field(
        description=(
            "Number of same-train seat changes (ticket boundaries while "
            "staying onboard one train run)."
        )
    )
    total_transfer_wait_minutes: int | None = Field(
        None, description="Sum of interchange waits (train changes only; seat-change halts do not count)."
    )
    distinct_train_count: int = Field(description="Number of distinct trains used.")
    distinct_trains: list[str] = Field(
        description="Distinct train numbers in order of first use."
    )
    transitions: list[MixedTransitionOut] = Field(default_factory=list)
    segments: list[MixedSegmentOut]
    all_segments_confirmed_available: bool = Field(
        description="True only when every segment is explicitly CONFIRMED_AVAILABLE."
    )
    unverified_segment_numbers: list[int] = Field(
        default_factory=list, description="Segments whose availability could NOT be determined."
    )
    availability_checked_at: str | None = None
    oldest_availability_update_at: str | None = Field(
        None, description="Oldest provider last_updated_at across segments (freshness indicator)."
    )


class MixedBudgetUsageOut(BaseModel):
    """Mixed-journey search budget consumption (computational safeguards)."""

    requests_used: int
    requests_max: int
    credits_used_estimate: int = Field(description="Item-count credit estimate (authoritative value in api_usage).")
    credits_max: int
    duration_ms: int
    time_budget_ms: int
    discovery_pairs_used: int
    discovery_pairs_max: int
    trains_processed: int = Field(description="Train schedules in the pool (fetches + cache hits).")
    trains_total: int = Field(description="Distinct trains discovered during the search.")
    states_expanded: int
    states_max: int
    journeys_found: int = Field(description="Complete journeys recorded (results + unverified candidates).")
    journeys_max: int


class MixedCompletenessOut(BaseModel):
    """Was the search exhaustive, and if not, why?"""

    complete: bool
    budget: MixedBudgetUsageOut
    segments_checked: int = Field(description="Availability items sent to the provider (distinct per expansion).")
    distinct_availability_queries: int
    deduplicated_queries: int
    schedule_cache_hits: int = 0
    discovery_cache_hits: int = 0
    incomplete_reasons: list[str] = Field(
        default_factory=list,
        description=(
            "TRAIN_LIMIT_REACHED | DISCOVERY_PAIR_LIMIT_REACHED | STATE_LIMIT_REACHED | "
            "JOURNEY_LIMIT_REACHED | BUDGET_REQUESTS_EXHAUSTED | BUDGET_CREDITS_EXHAUSTED | "
            "TIME_LIMIT_REACHED | HORIZON_LIMIT_REACHED | PROVIDER_ERROR (each clearly "
            "reported, never silent)."
        ),
    )
    search_scope: dict = Field(
        default_factory=dict,
        description=(
            "Configured scope actually used: transfer buffer, max wait, horizon, "
            "seat-change semantics."
        ),
    )


class MixedSearchResponse(BaseModel):
    success: bool = Field(
        description="True only when at least one fully confirmed mixed journey was found."
    )
    status: MixedSearchStatus
    message: str
    search: dict
    ranked_by: MixedRankingPreference
    results: list[MixedJourneyOut] = Field(default_factory=list)
    total_results: int = 0
    results_truncated: bool = Field(False, description="True when max_results capped the response.")
    unverified_candidates: list[MixedJourneyOut] = Field(
        default_factory=list,
        description=(
            "Schedule-valid journeys whose every segment was checked but at "
            "least one could NOT be verified (provider error / unknown). "
            "Never confirmed results."
        ),
    )
    total_unverified_candidates: int = 0
    direct_trains: list[DirectTrainRefOut] = Field(
        default_factory=list,
        description=(
            "Direct through trains found during discovery (reference only, "
            "availability NOT checked)."
        ),
    )
    stations: list[StationReportOut] = Field(default_factory=list)
    skipped_trains: list[TrainSkipReportOut] = Field(default_factory=list)
    search_completeness: MixedCompletenessOut | None = None
    api_usage: dict | None = None
    availability_checked_at: str | None = None
    errors: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# Re-exported for the route module so the mixed endpoint reports the same
# shared preference enum used by phases 2 and 3 in cross-engine fields.
__all__ = [
    "ALLOWED_CLASSES",
    "ALLOWED_QUOTAS",
    "MAX_BOOKING_WINDOW_DAYS",
    "MixedRankingPreference",
    "MixedSearchRequest",
    "MixedSearchResponse",
    "MixedSearchStatus",
    "MixedSegmentOut",
    "MixedTransitionOut",
    "MixedJourneyOut",
    "MixedBudgetUsageOut",
    "MixedCompletenessOut",
    "RankingPreference",
    "JourneyVerification",
]
