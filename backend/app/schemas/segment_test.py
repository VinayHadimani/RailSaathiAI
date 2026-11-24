"""Request/response schemas for the segment-availability feasibility test.

Endpoint: POST /api/v1/test/segment-availability

The workflow fetches one real train's schedule, verifies that both
segments' stations are on the route in the correct order, then checks
seat availability for each segment independently with the SAME train
number, journey date, travel class and quota.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models.schedule import ScheduleStop
from ..providers.base import InternalAvailabilityStatus

# Travel class codes accepted by the provider (IRCTC-style).
ALLOWED_CLASSES: set[str] = {"1A", "2A", "3A", "3E", "CC", "SL", "2S", "FC", "EC", "EA", "GS"}
# Quotas documented by RailCore: General, Tatkal, Senior/Lower berth, Ladies.
ALLOWED_QUOTAS: set[str] = {"GN", "TQ", "SS", "LD"}

# Indian Railways operate on IST regardless of server timezone.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class SegmentAvailabilityTestRequest(BaseModel):
    """Input for the same-train, two-segment availability feasibility test."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "train_number": "17301",
                "from_station_1": "MYS",
                "to_station_1": "ASK",
                "from_station_2": "ASK",
                "to_station_2": "DWR",
                "journey_date": "2026-11-24",
                "travel_class": "SL",
                "quota": "GN",
            }
        }
    )

    train_number: str = Field(
        ..., description="Real train number, 4-6 digits/letters, e.g. '17301'."
    )
    from_station_1: str = Field(..., description="Boarding station code of segment 1, e.g. 'MYS'.")
    to_station_1: str = Field(..., description="Destination station code of segment 1, e.g. 'ASK'.")
    from_station_2: str = Field(..., description="Boarding station code of segment 2, e.g. 'ASK'.")
    to_station_2: str = Field(..., description="Destination station code of segment 2, e.g. 'DWR'.")
    journey_date: date = Field(..., description="Journey date (YYYY-MM-DD), shared by both segments.")
    travel_class: str = Field("SL", description="Travel class: 1A, 2A, 3A, 3E, CC, SL, 2S, FC, EC, EA or GS.")
    quota: str = Field("GN", description="Reservation quota: GN, TQ, SS or LD.")

    @field_validator("train_number")
    @classmethod
    def _clean_train_number(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.isalnum() or not (4 <= len(cleaned) <= 6):
            raise ValueError("train_number must be 4-6 alphanumeric characters")
        return cleaned

    @field_validator("from_station_1", "to_station_1", "from_station_2", "to_station_2")
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
    def _validate_semantics(self) -> "SegmentAvailabilityTestRequest":
        if self.travel_class not in ALLOWED_CLASSES:
            raise ValueError(f"travel_class must be one of {sorted(ALLOWED_CLASSES)}")
        if self.quota not in ALLOWED_QUOTAS:
            raise ValueError(f"quota must be one of {sorted(ALLOWED_QUOTAS)}")
        if self.from_station_1 == self.to_station_1:
            raise ValueError("from_station_1 and to_station_1 must be different stations")
        if self.from_station_2 == self.to_station_2:
            raise ValueError("from_station_2 and to_station_2 must be different stations")
        today_ist = datetime.now(tz=IST).date()
        if self.journey_date < today_ist:
            raise ValueError("journey_date cannot be in the past (Indian Standard Time)")
        return self

    def search_echo(self) -> dict:
        return {
            "train_number": self.train_number,
            "from_station_1": self.from_station_1,
            "to_station_1": self.to_station_1,
            "from_station_2": self.from_station_2,
            "to_station_2": self.to_station_2,
            "journey_date": self.journey_date.isoformat(),
            "travel_class": self.travel_class,
            "quota": self.quota,
        }


class WorkflowStatus(str, Enum):
    """Outcome of the feasibility workflow (kept separate from success)."""

    COMPLETED_BOTH_CONFIRMED = "COMPLETED_BOTH_CONFIRMED"
    COMPLETED_NOT_CONFIRMED = "COMPLETED_NOT_CONFIRMED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    SCHEDULE_INVALID = "SCHEDULE_INVALID"
    CONFIG_ERROR = "CONFIG_ERROR"


class TrainInfoOut(BaseModel):
    number: str
    name: str | None = None
    source: str | None = None
    destination: str | None = None
    running_days: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    total_stops: int = 0


class SegmentSequenceOut(BaseModel):
    from_sequence: int | None = None
    to_sequence: int | None = None


class ScheduleValidationOut(BaseModel):
    same_train: bool = True
    stations_on_route: bool = False
    missing_stations: list[str] = Field(default_factory=list)
    first_segment_valid: bool = False
    second_segment_valid: bool = False
    first_segment_precedes_second: bool = False
    segments_contiguous: bool = False
    first_segment_sequence: SegmentSequenceOut | None = None
    second_segment_sequence: SegmentSequenceOut | None = None
    message: str = ""


class SegmentAvailabilityOut(BaseModel):
    segment: str = Field(description="'segment_1' or 'segment_2'")
    train_number: str
    from_station: str
    to_station: str
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
    provider: str = "railcore"
    request_id: str | None = None
    provider_raw: dict | None = None
    error: dict | None = None


class ApiUsageOut(BaseModel):
    provider: str = "railcore"
    requests_sent: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    credits_cost: int = 0
    credits_remaining: int | None = None
    rate_limit_remaining_per_minute: int | None = None
    rate_limit_day_remaining: int | None = None


class SegmentAvailabilityTestResponse(BaseModel):
    success: bool = Field(description="True only when both segments are explicitly confirmed available.")
    status: WorkflowStatus
    message: str
    search: dict
    train: TrainInfoOut | None = None
    schedule_validation: ScheduleValidationOut | None = None
    schedule: list[ScheduleStop] | None = None
    segments: list[SegmentAvailabilityOut] = Field(default_factory=list)
    both_segments_confirmed_available: bool = False
    availability_checked_at: str | None = None
    duration_ms: int = 0
    api_usage: ApiUsageOut | None = None
    errors: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
