"""Segment availability test workflow (Phase 1 feasibility core).

Orchestrates the three provider operations that must be proven before the
journey-discovery engine is built:

1. Fetch a real train's complete schedule.
2. Check seat availability between two stations on that train.
3. Check seat availability between two different stations on the SAME
   train (same train number, journey date, class and quota).

Validation rules implemented (spec section 8 of the task):
- Both segments belong to the same train (single schedule is fetched).
- The first segment must precede the second in the train schedule.
- Only explicit provider "AVAILABLE" counts as confirmed availability;
  RAC / WAITLIST / REGRET / NOT_AVAILABLE / UNKNOWN never count.
- An API error is never interpreted as unavailable OR available
  (PROVIDER_ERROR status, distinct from NOT_AVAILABLE).
- The last availability check timestamp is always displayed.
- Availability is never presented as a booked ticket.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from ..models.schedule import TrainSchedule
from ..providers.base import (
    InternalAvailabilityStatus,
    ProviderNotConfiguredError,
    RailwayProviderError,
)
from ..providers.railcore import RailCoreClient
from ..schemas.segment_test import (
    ApiUsageOut,
    ScheduleValidationOut,
    SegmentAvailabilityOut,
    SegmentAvailabilityTestRequest,
    SegmentAvailabilityTestResponse,
    SegmentSequenceOut,
    TrainInfoOut,
    WorkflowStatus,
)

DISCLAIMER_NOTES = [
    "This result is a point-in-time availability check, not a booked ticket. "
    "RailRoute AI does not book tickets.",
    "Two independently available segments do not by themselves guarantee that "
    "Indian Railways permits combining separate tickets on the same train; "
    "ticketing rules must be verified before booking.",
    "A provider error is never interpreted as unavailability (or availability).",
]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class SegmentAvailabilityTestService:
    """Runs the same-train two-segment availability workflow."""

    def __init__(self, client: RailCoreClient) -> None:
        self.client = client

    def run(self, request: SegmentAvailabilityTestRequest) -> tuple[dict, int]:
        """Execute the workflow.

        Returns:
            (response_body_dict, http_status_code)
        """
        started = time.monotonic()
        stats_before = self.client.stats_snapshot()
        errors: list[dict] = []
        search = request.search_echo()

        # ---- Step 1: fetch the real train schedule -----------------------
        try:
            schedule_call = self.client.get_train_schedule(request.train_number)
        except ProviderNotConfiguredError as exc:
            return self._build(
                status=WorkflowStatus.CONFIG_ERROR,
                message=exc.message,
                search=search,
                errors=[exc.to_dict()],
                duration_ms=self._elapsed_ms(started),
            ), 503
        except RailwayProviderError as exc:
            errors.append(exc.to_dict())
            return self._build(
                status=WorkflowStatus.PROVIDER_ERROR,
                message=(
                    "The train schedule could not be retrieved from RailCore, so "
                    "no availability claims are made."
                ),
                search=search,
                errors=errors,
                api_usage=self._api_usage(stats_before),
                duration_ms=self._elapsed_ms(started),
            ), 502

        schedule = schedule_call.data

        # ---- Step 2: validate stations and sequence ----------------------
        validation, schedule_ok = self._validate_schedule(schedule, request)

        if not schedule_ok:
            return self._build(
                status=WorkflowStatus.SCHEDULE_INVALID,
                message=(
                    "Schedule validation failed: the requested segments are not "
                    "valid on this train. Availability checks were skipped to "
                    "conserve API credits."
                ),
                search=search,
                train=self._train_info(schedule),
                schedule_validation=validation,
                schedule=schedule.stops,
                errors=[
                    {
                        "code": "SCHEDULE_VALIDATION_FAILED",
                        "category": "VALIDATION",
                        "message": validation.message,
                        "retryable": False,
                        "details": {"missing_stations": validation.missing_stations},
                    }
                ],
                api_usage=self._api_usage(stats_before),
                duration_ms=self._elapsed_ms(started),
            ), 200

        # ---- Steps 3-5: independent availability checks --------------------
        # Both requests are constructed with the SAME train number, journey
        # date, travel class and quota (only the station pair differs).
        segments: list[SegmentAvailabilityOut] = []
        checked_times: list[str] = []
        segment_specs = [
            ("segment_1", request.from_station_1, request.to_station_1),
            ("segment_2", request.from_station_2, request.to_station_2),
        ]
        for segment_name, from_station, to_station in segment_specs:
            checked_at = _utc_now_iso()
            try:
                availability_call = self.client.get_seat_availability(
                    train_number=request.train_number,
                    from_station=from_station,
                    to_station=to_station,
                    journey_date=request.journey_date.isoformat(),
                    travel_class=request.travel_class,
                    quota=request.quota,
                )
                availability = availability_call.data
                requested = availability.requested_class
                entry = SegmentAvailabilityOut(
                    segment=segment_name,
                    train_number=availability.train_number or request.train_number,
                    from_station=availability.from_station or from_station,
                    to_station=availability.to_station or to_station,
                    status=availability.status,
                    provider_status=requested.provider_status if requested else None,
                    availability_text=requested.availability_text if requested else None,
                    available_count=requested.available_count if requested else None,
                    waitlist_count=requested.waitlist_count if requested else None,
                    rac_count=requested.rac_count if requested else None,
                    fare=requested.fare if requested else None,
                    total_fare=requested.total_fare if requested else None,
                    last_updated_at=requested.last_updated_at if requested else None,
                    checked_at=checked_at,
                    provider="railcore",
                    request_id=availability_call.request_id,
                    provider_raw=availability_call.raw,
                )
                if availability.class_not_offered:
                    errors.append(
                        {
                            "code": "CLASS_NOT_OFFERED",
                            "category": "VALIDATION",
                            "message": (
                                f"Class {request.travel_class} is not offered for "
                                f"{from_station}->{to_station} on train "
                                f"{request.train_number}; status is UNKNOWN and "
                                "must not be treated as available."
                            ),
                            "retryable": False,
                            "details": {"segment": segment_name},
                        }
                    )
            except RailwayProviderError as exc:
                error_dict = exc.to_dict()
                error_dict["details"] = {**error_dict.get("details", {}), "segment": segment_name}
                errors.append(error_dict)
                entry = SegmentAvailabilityOut(
                    segment=segment_name,
                    train_number=request.train_number,
                    from_station=from_station,
                    to_station=to_station,
                    status=InternalAvailabilityStatus.PROVIDER_ERROR,
                    checked_at=checked_at,
                    provider="railcore",
                    error=error_dict,
                )
            segments.append(entry)
            checked_times.append(checked_at)

        # ---- Step 6: verdict ------------------------------------------------
        statuses = [entry.status for entry in segments]
        any_provider_error = any(s == InternalAvailabilityStatus.PROVIDER_ERROR for s in statuses)
        both_confirmed = all(
            s == InternalAvailabilityStatus.CONFIRMED_AVAILABLE for s in statuses
        ) and len(statuses) == 2

        if any_provider_error:
            workflow_status = WorkflowStatus.PROVIDER_ERROR
            message = (
                "At least one segment could not be verified due to a provider "
                "error. An API error is not treated as unavailable, so no "
                "confirmed-availability conclusion is reported."
            )
        elif both_confirmed:
            workflow_status = WorkflowStatus.COMPLETED_BOTH_CONFIRMED
            message = (
                "Both segments of the same train explicitly report confirmed "
                "availability for the requested class and quota."
            )
        else:
            workflow_status = WorkflowStatus.COMPLETED_NOT_CONFIRMED
            message = (
                "The check completed, but at least one segment does not have "
                "explicitly confirmed availability (RAC/waitlist/unavailable/"
                "unknown), so the combination is not reported as confirmed."
            )

        return self._build(
            status=workflow_status,
            message=message,
            search=search,
            train=self._train_info(schedule),
            schedule_validation=validation,
            schedule=schedule.stops,
            segments=segments,
            both_confirmed=both_confirmed,
            availability_checked_at=max(checked_times) if checked_times else None,
            api_usage=self._api_usage(stats_before),
            errors=errors,
            duration_ms=self._elapsed_ms(started),
        ), 200

    # ------------------------------------------------------------ internals

    def _validate_schedule(
        self, schedule: TrainSchedule, request: SegmentAvailabilityTestRequest
    ) -> tuple[ScheduleValidationOut, bool]:
        stops = {
            stop.station_code: stop for stop in schedule.stops
        }
        codes = [
            request.from_station_1,
            request.to_station_1,
            request.from_station_2,
            request.to_station_2,
        ]
        missing = [code for code in codes if code not in stops]

        def seq(code: str) -> int | None:
            stop = stops.get(code)
            return stop.sequence if stop else None

        first_valid = (
            request.from_station_1 in stops
            and request.to_station_1 in stops
            and seq(request.from_station_1) < seq(request.to_station_1)  # type: ignore[operator]
        )
        second_valid = (
            request.from_station_2 in stops
            and request.to_station_2 in stops
            and seq(request.from_station_2) < seq(request.to_station_2)  # type: ignore[operator]
        )
        precedes = (
            request.to_station_1 in stops
            and request.from_station_2 in stops
            and seq(request.to_station_1) <= seq(request.from_station_2)  # type: ignore[operator]
        )
        contiguous = request.to_station_1 == request.from_station_2 and precedes

        problems: list[str] = []
        if missing:
            problems.append(f"stations not on this train's route: {', '.join(missing)}")
        if not missing and not first_valid:
            problems.append(
                f"segment 1 ({request.from_station_1}->{request.to_station_1}) is not "
                "in forward route order"
            )
        if not missing and not second_valid:
            problems.append(
                f"segment 2 ({request.from_station_2}->{request.to_station_2}) is not "
                "in forward route order"
            )
        if not missing and first_valid and second_valid and not precedes:
            problems.append("segment 2 does not start at or after the end of segment 1")

        validation = ScheduleValidationOut(
            same_train=True,
            stations_on_route=not missing,
            missing_stations=missing,
            first_segment_valid=first_valid,
            second_segment_valid=second_valid,
            first_segment_precedes_second=precedes,
            segments_contiguous=contiguous,
            first_segment_sequence=SegmentSequenceOut(
                from_sequence=seq(request.from_station_1),
                to_sequence=seq(request.to_station_1),
            ),
            second_segment_sequence=SegmentSequenceOut(
                from_sequence=seq(request.from_station_2),
                to_sequence=seq(request.to_station_2),
            ),
            message=(
                "Both segments are valid consecutive sections of the same train."
                if (not problems and contiguous)
                else "Schedule is valid for both segments (non-contiguous but ordered)."
                if not problems
                else "; ".join(problems)
            ),
        )
        return validation, not problems

    def _train_info(self, schedule: TrainSchedule) -> TrainInfoOut:
        return TrainInfoOut(
            number=schedule.train_number,
            name=schedule.train_name,
            source=schedule.source_station_code,
            destination=schedule.destination_station_code,
            running_days=schedule.running_days,
            classes=schedule.classes,
            total_stops=len(schedule.stops),
        )

    def _api_usage(self, stats_before: dict[str, int]) -> ApiUsageOut:
        after = self.client.stats_snapshot()
        return ApiUsageOut(
            provider="railcore",
            requests_sent=after["requests_sent"] - stats_before["requests_sent"],
            successful_requests=after["successful_requests"] - stats_before["successful_requests"],
            failed_requests=after["failed_requests"] - stats_before["failed_requests"],
            credits_cost=self.client.credits_cost_total,
            credits_remaining=self.client.credits_remaining,
            rate_limit_remaining_per_minute=self.client.rate_limit_remaining,
            rate_limit_day_remaining=self.client.rate_limit_day_remaining,
        )

    def _elapsed_ms(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _build(
        self,
        *,
        status: WorkflowStatus,
        message: str,
        search: dict,
        train: TrainInfoOut | None = None,
        schedule_validation: ScheduleValidationOut | None = None,
        schedule: list | None = None,
        segments: list[SegmentAvailabilityOut] | None = None,
        both_confirmed: bool = False,
        availability_checked_at: str | None = None,
        api_usage: ApiUsageOut | None = None,
        errors: list[dict] | None = None,
        duration_ms: int = 0,
    ) -> dict:
        response = SegmentAvailabilityTestResponse(
            success=status == WorkflowStatus.COMPLETED_BOTH_CONFIRMED,
            status=status,
            message=message,
            search=search,
            train=train,
            schedule_validation=schedule_validation,
            schedule=schedule,
            segments=segments or [],
            both_segments_confirmed_available=both_confirmed,
            availability_checked_at=availability_checked_at,
            duration_ms=duration_ms,
            api_usage=api_usage,
            errors=errors or [],
            notes=DISCLAIMER_NOTES,
        )
        return response.model_dump(mode="json", exclude_none=True)
