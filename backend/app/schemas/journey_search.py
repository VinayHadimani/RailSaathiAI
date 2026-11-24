"""Request/response schemas for the same-train combination search.

Endpoint: POST /api/v1/journeys/same-train/search

Given source, destination, date, class and quota, the engine finds trains
whose complete source -> destination journey can be covered by MULTIPLE
consecutive station-to-station segments on the SAME train, with every
segment explicitly reporting confirmed availability.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models.schedule import ScheduleStop
from ..providers.base import InternalAvailabilityStatus
from .segment_test import ALLOWED_CLASSES, ALLOWED_QUOTAS, IST

# Indian Railways reservations open 60 days before the journey (provider
# docs). One extra day covers segments boarded after midnight on day 2.
MAX_BOOKING_WINDOW_DAYS = 62


class RankingPreference(str, Enum):
    """How results are ordered (ties broken deterministically)."""

    CHEAPEST = "CHEAPEST"
    FASTEST = "FASTEST"
    FEWEST_CHANGES = "FEWEST_CHANGES"


class SearchStatus(str, Enum):
    """Outcome of a combination search (kept separate from success)."""

    COMPLETED = "COMPLETED"
    COMPLETED_NO_COMBINATIONS = "COMPLETED_NO_COMBINATIONS"
    PARTIAL_BUDGET_EXHAUSTED = "PARTIAL_BUDGET_EXHAUSTED"
    PARTIAL_TIME_LIMIT = "PARTIAL_TIME_LIMIT"
    PARTIAL_PROVIDER_ERROR = "PARTIAL_PROVIDER_ERROR"
    NO_CANDIDATE_TRAINS = "NO_CANDIDATE_TRAINS"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"


class SameTrainSearchRequest(BaseModel):
    """Input for the same-train segment-combination search."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "from_station": "MYS",
                "to_station": "DWR",
                "journey_date": "2026-11-26",
                "travel_class": "SL",
                "quota": "GN",
                "ranking_preference": "FEWEST_CHANGES",
            }
        }
    )

    from_station: str = Field(..., description="Boarding station code, e.g. 'MYS'.")
    to_station: str = Field(..., description="Destination station code, e.g. 'DWR'.")
    journey_date: date = Field(..., description="Journey date (YYYY-MM-DD): the date the train is boarded at from_station (IST).")
    travel_class: str = Field("SL", description="Travel class: 1A, 2A, 3A, 3E, CC, SL, 2S, FC, EC, EA or GS.")
    quota: str = Field("GN", description="Reservation quota: GN, TQ, SS or LD.")
    ranking_preference: RankingPreference = Field(
        RankingPreference.FEWEST_CHANGES,
        description="Result ordering: CHEAPEST, FASTEST or FEWEST_CHANGES.",
    )

    @field_validator("from_station", "to_station")
    @classmethod
    def _clean_station(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned.isalnum() or not (2 <= len(cleaned) <= 6):
            raise ValueError(f"'{value}' is not a valid station code (2-6 alphanumeric characters)")
        return cleaned

    @field_validator("travel_class", "quota")
    @classmethod
    def _clean_code(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _validate_semantics(self) -> "SameTrainSearchRequest":
        if self.travel_class not in ALLOWED_CLASSES:
            raise ValueError(f"travel_class must be one of {sorted(ALLOWED_CLASSES)}")
        if self.quota not in ALLOWED_QUOTAS:
            raise ValueError(f"quota must be one of {sorted(ALLOWED_QUOTAS)}")
        if self.from_station == self.to_station:
            raise ValueError("from_station and to_station must be different stations")
        today_ist = datetime.now(tz=IST).date()
        if self.journey_date < today_ist:
            raise ValueError("journey_date cannot be in the past (Indian Standard Time)")
        if self.journey_date > today_ist + timedelta(days=MAX_BOOKING_WINDOW_DAYS):
            raise ValueError(
                f"journey_date is beyond the ~60-day Indian Railways booking window "
                f"(max {today_ist + timedelta(days=MAX_BOOKING_WINDOW_DAYS)})"
            )
        return self

    def search_echo(self) -> dict:
        return {
            "from_station": self.from_station,
            "to_station": self.to_station,
            "journey_date": self.journey_date.isoformat(),
            "travel_class": self.travel_class,
            "quota": self.quota,
            "ranking_preference": self.ranking_preference.value,
        }


# ----------------------------------------------------------------- outputs


class StationPointOut(BaseModel):
    """A boarding or alighting point of a segment."""

    code: str
    name: str | None = None
    sequence: int | None = None


class SegmentDetailOut(BaseModel):
    """One station-to-station segment of a combination."""

    segment_number: int = Field(description="1-based position in the journey.")
    train_number: str
    from_station: StationPointOut
    to_station: StationPointOut
    departure_time: str | None = None
    departure_day: int | None = Field(None, description="Journey day number (1-based) at boarding.")
    arrival_time: str | None = None
    arrival_day: int | None = Field(None, description="Journey day number (1-based) at alighting.")
    halt_minutes_at_to_station: int | None = None
    distance_km_at_to_station: float | None = None
    travel_class: str
    quota: str
    boarding_query_date: str = Field(
        description="Calendar date used for this segment's availability check "
        "(the date the train departs the boarding station; advances by one "
        "day per overnight rollover)."
    )
    status: InternalAvailabilityStatus
    provider_status: str | None = None
    availability_text: str | None = None
    available_count: int | None = None
    waitlist_count: int | None = None
    rac_count: int | None = None
    fare: float | None = None
    total_fare: float | None = None
    last_updated_at: str | None = None
    checked_at: str | None = None
    request_id: str | None = None
    error: dict | None = None


class FareComparisonOut(BaseModel):
    """Segmented fare vs the direct through-route fare of the same train."""

    direct_total_fare: float | None = None
    direct_segment_status: InternalAvailabilityStatus | None = None
    direct_availability_text: str | None = None
    difference: float | None = Field(None, description="segmented total minus direct fare (positive = costlier).")
    difference_percent: float | None = None
    note: str | None = None


class JourneyEndpointOut(BaseModel):
    station: StationPointOut
    time: str | None = None
    day: int | None = None


class CombinationOut(BaseModel):
    """A complete same-train, multi-segment journey combination.

    Every segment individually reports CONFIRMED_AVAILABLE availability;
    anything less (RAC, waitlist, unavailable, unknown, provider error)
    disqualifies the whole combination.
    """

    combination_id: str = Field(description="Stable id: train-date-boundaries, e.g. '17301-2026-11-26-MYS-ASK-DWR'.")
    journey_type: str = Field("SAME_TRAIN_SEGMENTS", description="Always SAME_TRAIN_SEGMENTS in this phase.")
    train_number: str
    train_name: str | None = None
    journey_date: str = Field(description="Requested journey date (boarding date at from_station).")
    train_run_origin_departure_date: str | None = Field(
        None, description="Date this train run departs its origin station (may be one day earlier for overnight boardings)."
    )
    segments: list[SegmentDetailOut]
    segment_count: int
    seat_changes: int = Field(description="segment_count - 1 seat/ticket changes within the same train.")
    departure: JourneyEndpointOut
    arrival: JourneyEndpointOut
    overnight: bool = Field(description="True when the journey crosses midnight.")
    total_duration_minutes: int | None = None
    total_fare: float | None = Field(None, description="Sum of segment fares; null when any segment fare is unknown.")
    fares_complete: bool
    segments_missing_fare: list[int] = Field(default_factory=list)
    fare_comparison: FareComparisonOut | None = None
    all_segments_confirmed_available: bool = True
    availability_checked_at: str | None = None
    oldest_availability_update_at: str | None = Field(
        None, description="Oldest provider last_updated_at across segments (freshness indicator)."
    )


class DirectSegmentOut(BaseModel):
    """Reference: the direct through-route segment of the same train."""

    status: InternalAvailabilityStatus
    provider_status: str | None = None
    availability_text: str | None = None
    total_fare: float | None = None
    checked_at: str | None = None


class TrainSearchReportOut(BaseModel):
    """Per-candidate-train outcome of the search."""

    train_number: str
    train_name: str | None = None
    processed: bool = Field(True, description="False when skipped before availability checking.")
    skip_reason: str | None = Field(
        None,
        description="CLASS_NOT_OFFERED | NOT_RUNNING_ON_DATE | STATIONS_NOT_ON_ROUTE | "
        "STATION_NOT_A_STOP | STATION_ORDER_INVALID | SCHEDULE_FETCH_FAILED | TRAIN_LIMIT_REACHED | BUDGET_EXHAUSTED",
    )
    skip_detail: str | None = None
    window_stop_count: int | None = Field(None, description="Scheduled stops between source and destination.")
    segments_checked: int = Field(0, description="Distinct segment availability queries performed for this train.")
    combinations_found: int = 0
    combinations_truncated: bool = Field(False, description="True when the per-train enumeration cap was hit.")
    direct_segment: DirectSegmentOut | None = None
    errors: list[dict] = Field(default_factory=list)


class BudgetUsageOut(BaseModel):
    """Search budget consumption (computational safeguards, not change limits)."""

    requests_used: int
    requests_max: int
    credits_used_estimate: int = Field(description="Item-count credit estimate (authoritative value in api_usage).")
    credits_max: int
    duration_ms: int
    time_budget_ms: int
    trains_processed: int
    trains_total: int = Field(description="Candidate trains considered after pre-filtering.")


class SearchCompletenessOut(BaseModel):
    """Was the search exhaustive, and if not, why?"""

    complete: bool
    budget: BudgetUsageOut
    segments_checked: int
    distinct_availability_queries: int
    deduplicated_queries: int
    combinations_found: int
    schedule_cache_hits: int = 0
    discovery_cache_hits: int = 0
    incomplete_reasons: list[str] = Field(
        default_factory=list,
        description="TRAIN_LIMIT_REACHED | BUDGET_REQUESTS_EXHAUSTED | BUDGET_CREDITS_EXHAUSTED | "
        "TIME_LIMIT_REACHED | PROVIDER_ERROR (each clearly reported, never silent).",
    )


class ApiUsageOut(BaseModel):
    """RailCore usage for this search (from response headers where available)."""

    provider: str = "railcore"
    requests_sent: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    credits_cost: int = Field(0, description="Credits reported by provider response headers for this search.")
    credits_remaining: int | None = None
    rate_limit_remaining_per_minute: int | None = None
    rate_limit_day_remaining: int | None = None


class SameTrainSearchResponse(BaseModel):
    success: bool = Field(description="True only when at least one fully confirmed combination was found.")
    status: SearchStatus
    message: str
    search: dict
    ranked_by: RankingPreference
    results: list[CombinationOut] = Field(default_factory=list)
    total_results: int = 0
    results_truncated: bool = Field(False, description="True when max_results capped the response.")
    candidates: list[TrainSearchReportOut] = Field(default_factory=list)
    search_completeness: SearchCompletenessOut | None = None
    api_usage: ApiUsageOut | None = None
    availability_checked_at: str | None = None
    errors: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
