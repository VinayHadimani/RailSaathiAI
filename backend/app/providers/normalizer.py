"""Normalizes raw RailCore JSON payloads into internal domain models.

RailCore response envelope (documented shape, verified 2026-11-23):

    {
      "success": true,
      "data": { ... endpoint payload ... },
      "meta": {
        "api_version": "v1",
        "request_id": "req_...",
        "trace_id": "trc_...",
        "timestamp": "...",
        "freshness": {...}
      }
    }

Any deviation from the documented shape raises
ProviderUnexpectedResponseError - a malformed response is never interpreted
as availability information.
"""

from __future__ import annotations

from typing import Any

from ..models.availability import (
    ClassAvailability,
    ConfirmationProbability,
    SegmentAvailability,
)
from ..models.route_search import AvailabilityMini, RouteTrainSummary, RouteTrainsResult
from ..models.schedule import ScheduleStop, TrainSchedule
from .base import InternalAvailabilityStatus, ProviderUnexpectedResponseError

# Provider status string -> internal status.
# Deliberately strict: anything absent from this map becomes UNKNOWN
# and can never be treated as confirmed availability (spec sections 7, 8).
STATUS_MAP: dict[str, InternalAvailabilityStatus] = {
    "AVAILABLE": InternalAvailabilityStatus.CONFIRMED_AVAILABLE,
    "RAC": InternalAvailabilityStatus.RAC,
    "WAITLIST": InternalAvailabilityStatus.WAITLIST,
    "WL": InternalAvailabilityStatus.WAITLIST,
    "REGRET": InternalAvailabilityStatus.NOT_AVAILABLE,
    "NOT_AVAILABLE": InternalAvailabilityStatus.NOT_AVAILABLE,
    "TRAIN_CANCELLED": InternalAvailabilityStatus.NOT_AVAILABLE,
    "TRAIN_DEPARTED": InternalAvailabilityStatus.NOT_AVAILABLE,
    "UNKNOWN": InternalAvailabilityStatus.UNKNOWN,
}


def map_provider_status(raw: str | None) -> InternalAvailabilityStatus:
    """Map a provider status string to the internal status.

    Unknown, missing or ambiguous values map to UNKNOWN - never confirmed.
    """
    if not raw or not isinstance(raw, str):
        return InternalAvailabilityStatus.UNKNOWN
    return STATUS_MAP.get(raw.strip().upper(), InternalAvailabilityStatus.UNKNOWN)


class RailCoreNormalizer:
    """Converts RailCore endpoint payloads to internal models."""

    def train_schedule(self, payload: dict[str, Any]) -> TrainSchedule:
        data = self._data(payload, context="train schedule")
        stops_raw = data.get("stops")
        if not isinstance(stops_raw, list):
            raise ProviderUnexpectedResponseError(
                "RailCore schedule response is missing the 'stops' array",
                code="MALFORMED_SCHEDULE",
            )
        stops: list[ScheduleStop] = []
        for entry in stops_raw:
            if not isinstance(entry, dict):
                raise ProviderUnexpectedResponseError(
                    "RailCore schedule contains a non-object stop entry",
                    code="MALFORMED_SCHEDULE",
                )
            sequence = entry.get("sequence")
            station_code = entry.get("station_code")
            if sequence is None or not station_code:
                raise ProviderUnexpectedResponseError(
                    "RailCore schedule stop is missing 'sequence' or 'station_code'",
                    code="MALFORMED_SCHEDULE",
                )
            stops.append(
                ScheduleStop(
                    sequence=int(sequence),
                    station_code=str(station_code).strip().upper(),
                    station_name=_opt_str(entry.get("station_name")),
                    stop_type=_opt_str(entry.get("stop_type")),
                    arrival_time=_opt_str(entry.get("arrival_time")),
                    departure_time=_opt_str(entry.get("departure_time")),
                    halt_minutes=_opt_int(entry.get("halt_minutes")),
                    day=_opt_int(entry.get("day")),
                    distance_km=_opt_float(entry.get("distance_km")),
                    platform_number=_opt_str(entry.get("platform_number")),
                    is_stop=entry.get("is_stop") if isinstance(entry.get("is_stop"), bool) else None,
                )
            )
        request_id = _request_id(payload)
        return TrainSchedule(
            train_number=str(data.get("train_number") or "").strip(),
            train_name=_opt_str(data.get("train_name")),
            source_station_code=_opt_str(data.get("source_station_code")),
            destination_station_code=_opt_str(data.get("destination_station_code")),
            running_days=_str_list(data.get("running_days")),
            classes=_str_list(data.get("classes")),
            total_duration_minutes=_opt_int(data.get("total_duration_minutes")),
            total_distance_km=_opt_float(data.get("distance_km")),
            stops=stops,
            provider="railcore",
            request_id=request_id,
        )

    def route_trains(
        self,
        payload: dict[str, Any],
        *,
        from_station: str,
        to_station: str,
        journey_date: str,
        quota: str | None,
    ) -> RouteTrainsResult:
        """Normalize a GET /routes/trains (trains between stations) payload.

        The availability_summary entries are informational context only
        (possibly days old) and are never used as availability evidence.
        """
        data = self._data(payload, context="route trains")
        trains_raw = data.get("trains")
        if not isinstance(trains_raw, list):
            raise ProviderUnexpectedResponseError(
                "RailCore route trains response is missing the 'trains' array",
                code="MALFORMED_ROUTE_TRAINS",
            )
        trains: list[RouteTrainSummary] = []
        for entry in trains_raw:
            if not isinstance(entry, dict):
                raise ProviderUnexpectedResponseError(
                    "RailCore route trains contains a non-object train entry",
                    code="MALFORMED_ROUTE_TRAINS",
                )
            train_number = entry.get("train_number")
            if not train_number:
                raise ProviderUnexpectedResponseError(
                    "RailCore route train entry is missing 'train_number'",
                    code="MALFORMED_ROUTE_TRAINS",
                )
            summary_raw = entry.get("availability_summary")
            summary = [
                AvailabilityMini(
                    class_code=str(item.get("class_code") or "").strip().upper(),
                    status=_opt_str(item.get("status")),
                    text=_opt_str(item.get("text")),
                    fare=_opt_float(item.get("fare")),
                    confidence=_opt_str(item.get("confidence")),
                    last_updated_at=_opt_str(item.get("last_updated_at")),
                )
                for item in (summary_raw if isinstance(summary_raw, list) else [])
                if isinstance(item, dict) and item.get("class_code")
            ]
            trains.append(
                RouteTrainSummary(
                    train_number=str(train_number).strip(),
                    train_name=_opt_str(entry.get("train_name")),
                    extended_train_name=_opt_str(entry.get("extended_train_name")),
                    from_station_code=_opt_str(entry.get("from_station_code")),
                    from_station_name=_opt_str(entry.get("from_station_name")),
                    to_station_code=_opt_str(entry.get("to_station_code")),
                    to_station_name=_opt_str(entry.get("to_station_name")),
                    departure_time=_opt_str(entry.get("departure_time")),
                    arrival_time=_opt_str(entry.get("arrival_time")),
                    duration_minutes=_opt_int(entry.get("duration_minutes")),
                    distance_km=_opt_float(entry.get("distance_km")),
                    train_type=_opt_str(entry.get("train_type")),
                    classes=_str_list(entry.get("classes")),
                    running_days=_str_list(entry.get("running_days")),
                    has_pantry=entry.get("has_pantry") if isinstance(entry.get("has_pantry"), bool) else None,
                    availability_summary=summary,
                )
            )
        return RouteTrainsResult(
            from_station_code=str(data.get("from_station_code") or from_station).strip().upper(),
            to_station_code=str(data.get("to_station_code") or to_station).strip().upper(),
            journey_date=str(data.get("journey_date") or journey_date),
            quota=_opt_str(data.get("quota")) or quota,
            matched_by=_opt_str(data.get("matched_by")),
            trains=trains,
            provider="railcore",
            request_id=_request_id(payload),
        )

    def segment_availability(
        self,
        payload: dict[str, Any],
        *,
        train_number: str,
        from_station: str,
        to_station: str,
        journey_date: str,
        travel_class: str,
        quota: str,
    ) -> SegmentAvailability:
        data = self._data(payload, context="seat availability")
        classes_raw = data.get("classes")
        if not isinstance(classes_raw, list):
            raise ProviderUnexpectedResponseError(
                "RailCore availability response is missing the 'classes' array",
                code="MALFORMED_AVAILABILITY",
            )
        all_classes = [self._class_availability(entry) for entry in classes_raw]
        requested_code = travel_class.strip().upper()
        requested = next(
            (c for c in all_classes if c.class_code == requested_code), None
        )
        return SegmentAvailability(
            train_number=str(data.get("train_number") or train_number).strip(),
            from_station=str(data.get("from_station_code") or from_station).strip().upper(),
            to_station=str(data.get("to_station_code") or to_station).strip().upper(),
            journey_date=str(data.get("journey_date") or journey_date),
            travel_class=requested_code,
            quota=str(data.get("quota") or quota),
            status=requested.status if requested else InternalAvailabilityStatus.UNKNOWN,
            requested_class=requested,
            class_not_offered=requested is None,
            all_classes=all_classes,
            provider="railcore",
            request_id=_request_id(payload),
        )

    def _class_availability(self, entry: Any) -> ClassAvailability:
        if not isinstance(entry, dict):
            raise ProviderUnexpectedResponseError(
                "RailCore availability 'classes' contains a non-object entry",
                code="MALFORMED_AVAILABILITY",
            )
        class_code = entry.get("class_code")
        if not class_code:
            raise ProviderUnexpectedResponseError(
                "RailCore availability entry is missing 'class_code'",
                code="MALFORMED_AVAILABILITY",
            )
        provider_status = _opt_str(entry.get("status"))
        probability_raw = entry.get("confirmation_probability")
        probability = None
        if isinstance(probability_raw, dict):
            probability = ConfirmationProbability(
                percentage=_opt_float(probability_raw.get("percentage")),
                label=_opt_str(probability_raw.get("label")),
                text=_opt_str(probability_raw.get("text")),
            )
        return ClassAvailability(
            class_code=str(class_code).strip().upper(),
            provider_status=provider_status,
            status=map_provider_status(provider_status),
            availability_text=_opt_str(entry.get("availability_text")),
            available_count=_opt_int(entry.get("available_count")),
            waitlist_count=_opt_int(entry.get("waitlist_count")),
            rac_count=_opt_int(entry.get("rac_count")),
            fare=_opt_float(entry.get("fare")),
            total_fare=_opt_float(entry.get("total_fare")),
            catering_charge=_opt_float(entry.get("catering_charge")),
            last_updated_at=_opt_str(entry.get("last_updated_at")),
            confirmation_probability=probability,
        )

    def _data(self, payload: Any, *, context: str) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ProviderUnexpectedResponseError(
                f"RailCore {context} response has success != true",
                code="UNEXPECTED_ENVELOPE",
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderUnexpectedResponseError(
                f"RailCore {context} response is missing the 'data' object",
                code="UNEXPECTED_ENVELOPE",
            )
        return data


def _request_id(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    if isinstance(meta, dict):
        return _opt_str(meta.get("request_id"))
    return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if item is not None]
