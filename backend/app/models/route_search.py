"""Normalized route-search (train discovery) models.

Provider-agnostic representation of "trains serving a route on a date"
(RailCore: GET /routes/trains). Used by the same-train combination
engine to find candidate trains for a requested source -> destination
journey before their schedules are fetched.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AvailabilityMini(BaseModel):
    """Compact class-level availability context from route search.

    NOTE: deliberately informational only - it can be days old (the
    provider documents last_updated_at as potentially hours or days
    stale). The search engine never uses it as availability evidence;
    every segment is re-checked through the seat availability endpoint.
    """

    class_code: str
    status: str | None = None
    text: str | None = None
    fare: float | None = None
    confidence: str | None = None
    last_updated_at: str | None = None


class RouteTrainSummary(BaseModel):
    """One train serving the requested route on the requested date."""

    train_number: str
    train_name: str | None = None
    extended_train_name: str | None = None
    from_station_code: str | None = None
    from_station_name: str | None = None
    to_station_code: str | None = None
    to_station_name: str | None = None
    departure_time: str | None = None
    arrival_time: str | None = None
    duration_minutes: int | None = None
    distance_km: float | None = None
    train_type: str | None = None
    classes: list[str] = Field(default_factory=list)
    running_days: list[str] = Field(default_factory=list)
    has_pantry: bool | None = None
    availability_summary: list[AvailabilityMini] = Field(default_factory=list)


class RouteTrainsResult(BaseModel):
    """Normalized result of a trains-between-stations search."""

    from_station_code: str
    to_station_code: str
    journey_date: str
    quota: str | None = None
    matched_by: str | None = None
    trains: list[RouteTrainSummary] = Field(default_factory=list)
    provider: str = "railcore"
    request_id: str | None = None
