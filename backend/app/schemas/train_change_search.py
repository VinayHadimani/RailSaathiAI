"""Request/response schemas for the train-change connection search.

Endpoint: POST /api/v1/journeys/train-change/search

Given source, destination, date, class and quota, the engine finds complete
journeys that require CHANGING TRAINS at intermediate stations (any number
of changes), where every segment explicitly reports confirmed availability
(provider status AVAILABLE). Only an explicit AVAILABLE qualifies: RAC,
waitlist, not-available, unknown statuses and provider errors never qualify,
and a provider error is never treated as unavailability (affected journeys
are reported as unverified candidates, never as confirmed results).

Segment types: every journey leg is ``TRAIN`` and every interchange between
two legs is a ``TRAIN_CHANGE`` transfer block. The response shape is
designed so a later phase can combine these journeys with same-train
segment combinations (phase 2) into mixed journeys: each segment is fully
self-contained (train, stations, timestamps, class, quota, availability,
fare) and journeys expose aggregate metrics.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..providers.base import InternalAvailabilityStatus
from .journey_search import (
    JourneyEndpointOut,
    MAX_BOOKING_WINDOW_DAYS,
    RankingPreference,
    StationPointOut,
)
from .segment_test import ALLOWED_CLASSES, ALLOWED_QUOTAS, IST


class TrainChangeSearchStatus(str, Enum):
    """Outcome of a train-change search (kept separate from success)."""

    AVAILABLE_COMBINATION = "AVAILABLE_COMBINATION"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    NO_COMPLETE_JOURNEY = "NO_COMPLETE_JOURNEY"
    SEARCH_LIMIT_REACHED = "SEARCH_LIMIT_REACHED"
    NO_CANDIDATE_TRAINS = "NO_CANDIDATE_TRAINS"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"


class JourneyVerification(str, Enum):
    """Per-journey verification level (journey_status field)."""

    AVAILABLE_COMBINATION = "AVAILABLE_COMBINATION"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"


class TrainChangeSearchRequest(BaseModel):
    """Input for the train-change connection search."""

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

    from_station: str = Field(..., description="Boarding station code, e.g. 'MYS'.")
    to_station: str = Field(..., description="Destination station code, e.g. 'DWR'.")
    journey_date: date = Field(
        ..., description="Journey date (YYYY-MM-DD): the date the first train is boarded at from_station (IST)."
    )
    travel_class: str = Field("SL", description="Travel class: 1A, 2A, 3A, 3E, CC, SL, 2S, FC, EC, EA or GS.")
    quota: str = Field("GN", description="Reservation quota: GN, TQ, SS or LD.")
    ranking_preference: RankingPreference = Field(
        RankingPreference.FEWEST_CHANGES,
        description="Result ordering: CHEAPEST, FASTEST or FEWEST_CHANGES.",
    )
    via_stations: list[str] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "Optional interchange hints (station codes, max 3). The engine also "
            "derives interchange candidates from fetched train schedules; hints "
            "are only needed when no through train exists to anchor the corridor."
        ),
    )
    min_transfer_buffer_minutes: int | None = Field(
        None,
        ge=0,
        le=360,
        description=(
            "Optional override of the configured minimum minutes between "
            "arrival at an interchange and the next train's departure."
        ),
    )
    max_transfer_wait_minutes: int | None = Field(
        None,
        ge=1,
        le=2880,
        description="Optional override of the configured maximum transfer wait in minutes.",
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

    @field_validator("via_stations")
    @classmethod
    def _clean_via(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            code = raw.strip().upper()
            if not code.isalnum() or not (2 <= len(code) <= 6):
                raise ValueError(f"'{raw}' is not a valid station code (2-6 alphanumeric characters)")
            if code in cleaned:
                raise ValueError("via_stations must not contain duplicates")
            cleaned.append(code)
        return cleaned

    @model_validator(mode="after")
    def _validate_semantics(self) -> "TrainChangeSearchRequest":
        if self.travel_class not in ALLOWED_CLASSES:
            raise ValueError(f"travel_class must be one of {sorted(ALLOWED_CLASSES)}")
        if self.quota not in ALLOWED_QUOTAS:
            raise ValueError(f"quota must be one of {sorted(ALLOWED_QUOTAS)}")
        if self.from_station == self.to_station:
            raise ValueError("from_station and to_station must be different stations")
        if self.from_station in self.via_stations or self.to_station in self.via_stations:
            raise ValueError("via_stations must not contain the source or destination station")
        if (
            self.min_transfer_buffer_minutes is not None
            and self.max_transfer_wait_minutes is not None
            and self.max_transfer_wait_minutes < self.min_transfer_buffer_minutes
        ):
            raise ValueError("max_transfer_wait_minutes must be >= min_transfer_buffer_minutes")
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
            "via_stations": list(self.via_stations),
        }


# ----------------------------------------------------------------- outputs


class TrainSegmentOut(BaseModel):
    """One train leg of a train-change journey (self-contained for phase 4)."""

    segment_number: int = Field(description="1-based position in the journey.")
    segment_type: str = Field("TRAIN", description="Always TRAIN for a leg on one train.")
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
    halt_minutes_at_to_station: int | None = None
    distance_km_at_to_station: float | None = None
    travel_class: str
    quota: str
    boarding_query_date: str = Field(
        description="Calendar date used for this segment's availability check "
        "(the date the train departs the boarding station; advances by one "
        "day per overnight rollover)."
    )
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


class TransferOut(BaseModel):
    """A train change between two consecutive legs at one interchange station."""

    transfer_number: int = Field(description="1-based position in the journey.")
    transfer_type: str = Field("TRAIN_CHANGE", description="Always TRAIN_CHANGE.")
    station: StationPointOut = Field(description="The interchange station (exact same station code).")
    from_train: str
    to_train: str
    arrival_datetime: str | None = None
    departure_datetime: str | None = None
    wait_minutes: int = Field(description="Minutes waited at the interchange station.")
    within_min_buffer: bool = Field(description="True when wait >= the configured minimum transfer buffer.")
    within_max_wait: bool = Field(description="True when wait <= the configured maximum transfer wait.")


class TrainChangeJourneyOut(BaseModel):
    """A complete train-change journey (every leg explicitly verified).

    ``journey_status`` is AVAILABLE_COMBINATION only when EVERY segment is
    CONFIRMED_AVAILABLE; journeys with any unverified segment are
    PARTIALLY_VERIFIED and are returned separately, never as confirmed
    results.
    """

    journey_id: str = Field(description="Stable id, e.g. '2026-11-27-99801@MYS-99802@UBL-DWR'.")
    journey_type: str = Field("TRAIN_CHANGE", description="Always TRAIN_CHANGE in this phase.")
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
    segment_count: int
    train_changes: int = Field(description="Number of train changes (segment_count - 1).")
    total_transfer_wait_minutes: int | None = Field(None, description="Sum of all interchange waits.")
    transfers: list[TransferOut] = Field(default_factory=list)
    segments: list[TrainSegmentOut]
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


class DirectTrainRefOut(BaseModel):
    """A direct through train discovered on the requested route.

    Reference information only: the train-change engine does NOT check its
    availability (it is not a train-change journey) and makes no claim
    about its seats.
    """

    train_number: str
    train_name: str | None = None
    from_station_code: str | None = None
    to_station_code: str | None = None
    departure_time: str | None = None
    arrival_time: str | None = None
    classes: list[str] = Field(default_factory=list)
    availability_checked: bool = Field(False, description="Always false here: seats were not checked.")


class PairDiscoveryOut(BaseModel):
    """One trains-between-stations discovery query made during the search."""

    from_station: str
    to_station: str
    journey_date: str
    trains_found: int = 0
    cache_hit: bool = False
    no_trains_found: bool = False
    error: dict | None = None


class StationReportOut(BaseModel):
    """Per-interchange-station processing report."""

    station_code: str
    station_name: str | None = None
    events_expanded: int = Field(0, description="Arrival events expanded at this station.")
    first_arrival_datetime: str | None = None
    pairs: list[PairDiscoveryOut] = Field(default_factory=list)
    legs_evaluated: int = Field(0, description="Schedule-valid legs considered for availability.")
    legs_confirmed: int = 0
    legs_rejected: int = Field(0, description="Legs with a definitive non-available answer (RAC/WL/NA).")
    legs_unverified: int = Field(0, description="Legs whose availability could not be determined.")


class TrainSkipReportOut(BaseModel):
    """A discovered train that could not be used (never 'unavailable')."""

    train_number: str
    train_name: str | None = None
    skip_reason: str = Field(
        description="CLASS_NOT_OFFERED | SCHEDULE_FETCH_FAILED | TRAIN_LIMIT_REACHED | DUPLICATE"
    )
    skip_detail: str | None = None
    error: dict | None = None


class TrainChangeBudgetUsageOut(BaseModel):
    """Train-change search budget consumption (computational safeguards)."""

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
    journeys_found: int
    journeys_max: int


class TrainChangeCompletenessOut(BaseModel):
    """Was the search exhaustive, and if not, why?"""

    complete: bool
    budget: TrainChangeBudgetUsageOut
    segments_checked: int = Field(description="Availability items sent to the provider (distinct per expansion).")
    distinct_availability_queries: int
    deduplicated_queries: int
    schedule_cache_hits: int = 0
    discovery_cache_hits: int = 0
    incomplete_reasons: list[str] = Field(
        default_factory=list,
        description="TRAIN_LIMIT_REACHED | DISCOVERY_PAIR_LIMIT_REACHED | STATE_LIMIT_REACHED | "
        "JOURNEY_LIMIT_REACHED | BUDGET_REQUESTS_EXHAUSTED | BUDGET_CREDITS_EXHAUSTED | "
        "TIME_LIMIT_REACHED | HORIZON_LIMIT_REACHED | PROVIDER_ERROR (each clearly reported, "
        "never silent).",
    )
    search_scope: dict = Field(
        default_factory=dict,
        description="Configured scope actually used: transfer buffer, max wait, horizon, trains considered.",
    )


class TrainChangeSearchResponse(BaseModel):
    success: bool = Field(description="True only when at least one fully confirmed train-change journey was found.")
    status: TrainChangeSearchStatus
    message: str
    search: dict
    ranked_by: RankingPreference
    results: list[TrainChangeJourneyOut] = Field(default_factory=list)
    total_results: int = 0
    results_truncated: bool = Field(False, description="True when max_results capped the response.")
    unverified_candidates: list[TrainChangeJourneyOut] = Field(
        default_factory=list,
        description="Schedule-valid journeys whose every segment was checked but at least one "
        "could NOT be verified (provider error / unknown). Never confirmed results.",
    )
    total_unverified_candidates: int = 0
    direct_trains: list[DirectTrainRefOut] = Field(
        default_factory=list,
        description="Direct through trains found during discovery (reference only, availability NOT checked).",
    )
    stations: list[StationReportOut] = Field(default_factory=list)
    skipped_trains: list[TrainSkipReportOut] = Field(default_factory=list)
    search_completeness: TrainChangeCompletenessOut | None = None
    api_usage: dict | None = None
    availability_checked_at: str | None = None
    errors: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
