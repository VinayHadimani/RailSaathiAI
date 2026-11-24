"""Normalized availability models.

Internal statuses (spec sections 7 and 22) are deliberately separate from
provider status strings. Only ``CONFIRMED_AVAILABLE`` may ever feed the
recommendation engine. ``PROVIDER_ERROR`` is distinct from
``NOT_AVAILABLE``: an API failure is NOT evidence that seats are gone.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..providers.base import InternalAvailabilityStatus


class ConfirmationProbability(BaseModel):
    percentage: float | None = None
    label: str | None = None
    text: str | None = None


class ClassAvailability(BaseModel):
    """Availability for one travel class on one segment."""

    class_code: str
    provider_status: str | None = None  # exact provider status string, preserved
    status: InternalAvailabilityStatus = InternalAvailabilityStatus.UNKNOWN
    availability_text: str | None = None  # e.g. "AVAILABLE-0045", "GNWL21/WL8"
    available_count: int | None = None
    waitlist_count: int | None = None
    rac_count: int | None = None
    fare: float | None = None
    total_fare: float | None = None
    catering_charge: float | None = None
    last_updated_at: str | None = None
    confirmation_probability: ConfirmationProbability | None = None


class SegmentAvailability(BaseModel):
    """Availability of one station-to-station segment on one train.

    ``status`` is the internal status of the REQUESTED class. If the
    requested class is not offered for this segment, ``status`` is UNKNOWN
    (never confirmed) and ``class_not_offered`` is True.
    """

    train_number: str
    from_station: str
    to_station: str
    journey_date: str
    travel_class: str
    quota: str
    status: InternalAvailabilityStatus = InternalAvailabilityStatus.UNKNOWN
    requested_class: ClassAvailability | None = None
    class_not_offered: bool = False
    all_classes: list[ClassAvailability] = Field(default_factory=list)
    provider: str = "railcore"
    request_id: str | None = None
