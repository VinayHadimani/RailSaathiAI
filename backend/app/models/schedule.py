"""Normalized train schedule models.

The provider-agnostic representation of a train timetable. Overnight
travel is captured via the ``day`` field (journey day number, 1-based):
a train departing 23:30 on day 1 and arriving 04:30 on day 2 is a valid
five-hour journey, and this must never be compared as clock times only
(spec section 6, rule 6).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScheduleStop(BaseModel):
    """A single stop on a train's route, in route order."""

    sequence: int
    station_code: str
    station_name: str | None = None
    stop_type: str | None = None  # ORIGIN | HALT | PASSING | DESTINATION
    arrival_time: str | None = None  # "HH:MM" (None at origin)
    departure_time: str | None = None  # "HH:MM" (None at destination)
    halt_minutes: int | None = None
    day: int | None = None  # journey day number (1-based) - overnight support
    distance_km: float | None = None
    platform_number: str | None = None
    is_stop: bool | None = None


class TrainSchedule(BaseModel):
    """A complete normalized train schedule."""

    train_number: str
    train_name: str | None = None
    source_station_code: str | None = None
    destination_station_code: str | None = None
    running_days: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    total_duration_minutes: int | None = None
    total_distance_km: float | None = None
    stops: list[ScheduleStop] = Field(default_factory=list)
    provider: str = "railcore"
    request_id: str | None = None

    def stop_by_code(self, station_code: str) -> ScheduleStop | None:
        """Return the stop for a station code, or None if not on the route."""
        code = station_code.strip().upper()
        for stop in self.stops:
            if stop.station_code.strip().upper() == code:
                return stop
        return None
