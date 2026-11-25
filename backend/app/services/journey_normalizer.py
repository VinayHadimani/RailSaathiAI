"""Journey normalization for the unified search (Phase 5).

Converts the three engines' public response payloads (the dicts their
``run()`` methods return, exactly what their own endpoints serialize) into
ONE unified journey representation (``UnifiedJourneyOut``):

- Phase 3 ``TrainChangeJourneyOut`` and phase 4 ``MixedJourneyOut`` are
  already field-compatible; their segments and transitions map directly
  (phase 3 segments are re-labelled FIRST_SEGMENT / TRAIN_CHANGE to match
  the unified typing).
- Phase 2 ``CombinationOut`` is a strict subset; the missing pieces are
  derived mechanically and semantics-preserving:
  * ``segment_type``: FIRST_SEGMENT for the first ticket, SAME_TRAIN_SEGMENT
    for the rest (a phase 2 combination is single-train by definition);
  * ``train_name``: the combination-level field;
  * ``booking_url``: None (RailCore provides none, same as phases 3/4);
  * absolute datetimes: ``boarding_query_date`` IS the calendar date the
    train departs the boarding station (the phase 2 engine's own overnight
    semantics - ``_boarding_date``), so departure_datetime = that date +
    the scheduled departure clock, and arrival_datetime advances by the
    engine's own day-offset difference. No new date calculation is
    invented; overnight boardings advance exactly as the engine does;
  * transitions: derived seat-change blocks (the passenger stays onboard;
    NOT transfers).

Nothing is dropped: fares, fare completeness, missing-fare lists, seat and
train changes, transfer waits, distinct trains, availability statuses with
their diagnostic error payloads, check timestamps and the phase 2 extras
(``train_number``, ``train_run_origin_departure_date``, ``fare_comparison``)
all survive normalization.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from ..providers.base import InternalAvailabilityStatus
from ..schemas.journey_search import (
    FareComparisonOut,
    JourneyEndpointOut,
    StationPointOut,
)
from ..schemas.segment_test import IST
from ..schemas.train_change_search import JourneyVerification
from ..schemas.unified_search import (
    UnifiedEngineSelection,
    UnifiedJourneyOut,
    UnifiedSegmentOut,
    UnifiedTransitionOut,
)

logger = logging.getLogger("railroute.services.journey_normalizer")


def _parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def journey_signature(journey: UnifiedJourneyOut) -> tuple:
    """Deduplication identity of a journey: its ordered physical segments.

    ``(train_number, from_sequence, to_sequence, boarding_date)`` per
    segment - stop sequences (not just station codes) participate so that
    circular services and different partitions of the same train remain
    distinguishable, while the same physical journey discovered by several
    engines collapses to one entry.
    """
    return tuple(
        (
            segment.train_number,
            segment.from_station.sequence,
            segment.to_station.sequence,
            segment.boarding_query_date,
        )
        for segment in journey.segments
    )


# ------------------------------------------------------------- segments


def _segment_from_p2(segment: dict, *, index: int, train_name: str | None) -> UnifiedSegmentOut:
    """One phase 2 ``SegmentDetailOut`` dict -> unified segment."""
    departure_datetime, arrival_datetime = _p2_segment_datetimes(segment)
    return UnifiedSegmentOut(
        segment_number=segment["segment_number"],
        segment_type="FIRST_SEGMENT" if index == 1 else "SAME_TRAIN_SEGMENT",
        train_number=segment["train_number"],
        train_name=train_name,
        from_station=StationPointOut(**segment["from_station"]),
        to_station=StationPointOut(**segment["to_station"]),
        departure_time=segment.get("departure_time"),
        departure_day=segment.get("departure_day"),
        departure_datetime=departure_datetime,
        arrival_time=segment.get("arrival_time"),
        arrival_day=segment.get("arrival_day"),
        arrival_datetime=arrival_datetime,
        boarding_query_date=segment["boarding_query_date"],
        halt_minutes_at_to_station=segment.get("halt_minutes_at_to_station"),
        distance_km_at_to_station=segment.get("distance_km_at_to_station"),
        segment_distance_km=segment.get("segment_distance_km"),
        travel_class=segment["travel_class"],
        quota=segment["quota"],
        status=InternalAvailabilityStatus(segment["status"]),
        provider_status=segment.get("provider_status"),
        availability_text=segment.get("availability_text"),
        available_count=segment.get("available_count"),
        waitlist_count=segment.get("waitlist_count"),
        rac_count=segment.get("rac_count"),
        fare=segment.get("fare"),
        total_fare=segment.get("total_fare"),
        last_updated_at=segment.get("last_updated_at"),
        checked_at=segment.get("checked_at"),
        request_id=segment.get("request_id"),
        booking_url=None,  # RailCore does not provide booking URLs
        error=segment.get("error"),
    )


def _p2_segment_datetimes(segment: dict) -> tuple[str | None, str | None]:
    """Absolute timestamps for one phase 2 segment.

    Reuses the phase 2 engine's own semantics: ``boarding_query_date`` is
    the calendar date the train departs the boarding station (the engine
    computed it with its overnight day-offset rules), the departure clock
    is the boarding station's scheduled departure time, and the arrival
    advances by the engine's arrival-day minus departure-day difference.
    Both datetimes are None when the schedule does not carry the needed
    pieces - no date is ever guessed.
    """
    departure_datetime: datetime | None = None
    arrival_datetime: datetime | None = None
    boarding = segment.get("boarding_query_date")
    if boarding:
        try:
            base_date = date.fromisoformat(boarding)
        except ValueError:
            return None, None
        departure_clock = _parse_hhmm(segment.get("departure_time"))
        arrival_clock = _parse_hhmm(segment.get("arrival_time"))
        departure_day = segment.get("departure_day")
        arrival_day = segment.get("arrival_day")
        if departure_clock is not None:
            departure_datetime = datetime.combine(base_date, departure_clock, tzinfo=IST)
        if arrival_clock is not None and departure_day is not None and arrival_day is not None:
            arrival_datetime = datetime.combine(
                base_date + timedelta(days=arrival_day - departure_day),
                arrival_clock,
                tzinfo=IST,
            )
    return (
        departure_datetime.isoformat() if departure_datetime is not None else None,
        arrival_datetime.isoformat() if arrival_datetime is not None else None,
    )


def _segment_from_p34(segment: dict, *, segment_type: str) -> UnifiedSegmentOut:
    """One phase 3 ``TrainSegmentOut`` / phase 4 ``MixedSegmentOut`` dict."""
    return UnifiedSegmentOut(
        segment_number=segment["segment_number"],
        segment_type=segment_type,
        train_number=segment["train_number"],
        train_name=segment.get("train_name"),
        from_station=StationPointOut(**segment["from_station"]),
        to_station=StationPointOut(**segment["to_station"]),
        departure_time=segment.get("departure_time"),
        departure_day=segment.get("departure_day"),
        departure_datetime=segment.get("departure_datetime"),
        arrival_time=segment.get("arrival_time"),
        arrival_day=segment.get("arrival_day"),
        arrival_datetime=segment.get("arrival_datetime"),
        boarding_query_date=segment["boarding_query_date"],
        halt_minutes_at_to_station=segment.get("halt_minutes_at_to_station"),
        distance_km_at_to_station=segment.get("distance_km_at_to_station"),
        segment_distance_km=segment.get("segment_distance_km"),
        travel_class=segment["travel_class"],
        quota=segment["quota"],
        status=InternalAvailabilityStatus(segment["status"]),
        provider_status=segment.get("provider_status"),
        availability_text=segment.get("availability_text"),
        available_count=segment.get("available_count"),
        waitlist_count=segment.get("waitlist_count"),
        rac_count=segment.get("rac_count"),
        fare=segment.get("fare"),
        total_fare=segment.get("total_fare"),
        last_updated_at=segment.get("last_updated_at"),
        checked_at=segment.get("checked_at"),
        request_id=segment.get("request_id"),
        booking_url=segment.get("booking_url"),
        error=segment.get("error"),
    )


# ----------------------------------------------------------- transitions


def _p2_transitions(segments: list[UnifiedSegmentOut]) -> list[UnifiedTransitionOut]:
    """Derived seat-change transitions for a phase 2 combination.

    A phase 2 combination is consecutive tickets on ONE train run: every
    boundary is a SAME_TRAIN_SEAT_CHANGE (the passenger stays onboard; no
    transfer window applies). The onboard halt is taken from the engine's
    own schedule data (the alighting station's halt minutes), falling back
    to the derived absolute-timestamp difference when the halt field is
    missing. It is never labelled a transfer and carries no wait claims.
    """
    transitions: list[UnifiedTransitionOut] = []
    for index in range(len(segments) - 1):
        previous, following = segments[index], segments[index + 1]
        halt = previous.halt_minutes_at_to_station
        if halt is None:
            arrival = _parse_iso_datetime(previous.arrival_datetime)
            departure = _parse_iso_datetime(following.departure_datetime)
            if arrival is not None and departure is not None:
                halt = int((departure - arrival).total_seconds() // 60)
        transitions.append(
            UnifiedTransitionOut(
                transition_number=index + 1,
                transition_type="SAME_TRAIN_SEAT_CHANGE",
                is_train_change=False,
                station=previous.to_station,
                from_train=previous.train_number,
                to_train=following.train_number,
                arrival_datetime=previous.arrival_datetime,
                departure_datetime=following.departure_datetime,
                wait_minutes=None,
                onboard_halt_minutes=halt,
                within_min_buffer=None,
                within_max_wait=None,
            )
        )
    return transitions


def _transition_from_p34(transition: dict) -> UnifiedTransitionOut:
    """One phase 4 ``MixedTransitionOut`` dict -> unified transition."""
    return UnifiedTransitionOut(
        transition_number=transition["transition_number"],
        transition_type=transition.get("transition_type") or "TRAIN_CHANGE",
        is_train_change=bool(transition.get("is_train_change")),
        station=StationPointOut(**transition["station"]),
        from_train=transition["from_train"],
        to_train=transition["to_train"],
        arrival_datetime=transition.get("arrival_datetime"),
        departure_datetime=transition.get("departure_datetime"),
        wait_minutes=transition.get("wait_minutes"),
        onboard_halt_minutes=transition.get("onboard_halt_minutes"),
        within_min_buffer=transition.get("within_min_buffer"),
        within_max_wait=transition.get("within_max_wait"),
    )


def _transition_from_p3(transfer: dict) -> UnifiedTransitionOut:
    """One phase 3 ``TransferOut`` dict -> unified transition."""
    return UnifiedTransitionOut(
        transition_number=transfer["transfer_number"],
        transition_type="TRAIN_CHANGE",
        is_train_change=True,
        station=StationPointOut(**transfer["station"]),
        from_train=transfer["from_train"],
        to_train=transfer["to_train"],
        arrival_datetime=transfer.get("arrival_datetime"),
        departure_datetime=transfer.get("departure_datetime"),
        wait_minutes=transfer.get("wait_minutes"),
        onboard_halt_minutes=None,
        within_min_buffer=transfer.get("within_min_buffer"),
        within_max_wait=transfer.get("within_max_wait"),
    )


# ------------------------------------------------------------- journeys


def _total_distance_km(segments: list[UnifiedSegmentOut]) -> float | None:
    if not segments:
        return None
    if any(s.segment_distance_km is None for s in segments):
        return None
    return round(sum(float(s.segment_distance_km) for s in segments), 2)


def _p2_journey(combination: dict) -> UnifiedJourneyOut:
    """One phase 2 ``CombinationOut`` dict -> unified journey."""
    raw_segments = combination.get("segments") or []
    train_name = combination.get("train_name")
    segments = [
        _segment_from_p2(segment, index=index, train_name=train_name)
        for index, segment in enumerate(raw_segments, start=1)
    ]
    transitions = _p2_transitions(segments)
    departure_datetime = segments[0].departure_datetime if segments else None
    arrival_datetime = segments[-1].arrival_datetime if segments else None
    train_number = combination.get("train_number")
    fare_comparison = (
        FareComparisonOut(**combination["fare_comparison"])
        if combination.get("fare_comparison")
        else None
    )
    return UnifiedJourneyOut(
        journey_id=combination["combination_id"],
        journey_type="SAME_TRAIN_SEGMENTS",
        journey_status=JourneyVerification.AVAILABLE_COMBINATION,
        from_station=segments[0].from_station.code if segments else "",
        to_station=segments[-1].to_station.code if segments else "",
        journey_date=combination.get("journey_date") or "",
        departure=JourneyEndpointOut(**combination["departure"]),
        arrival=JourneyEndpointOut(**combination["arrival"]),
        departure_datetime=departure_datetime,
        arrival_datetime=arrival_datetime,
        overnight=bool(combination.get("overnight")),
        total_duration_minutes=combination.get("total_duration_minutes"),
        total_distance_km=_total_distance_km(segments),
        total_fare=combination.get("total_fare"),
        fares_complete=bool(combination.get("fares_complete")),
        segments_missing_fare=combination.get("segments_missing_fare") or [],
        segment_count=combination.get("segment_count") or len(segments),
        train_changes=0,
        seat_changes=combination.get("seat_changes") or max(0, len(segments) - 1),
        total_transfer_wait_minutes=0,
        distinct_train_count=1,
        distinct_trains=[train_number] if train_number else [],
        transitions=transitions,
        segments=segments,
        all_segments_confirmed_available=bool(
            combination.get("all_segments_confirmed_available", True)
        ),
        unverified_segment_numbers=[],
        availability_checked_at=combination.get("availability_checked_at"),
        oldest_availability_update_at=combination.get("oldest_availability_update_at"),
        train_number=train_number,
        train_run_origin_departure_date=combination.get("train_run_origin_departure_date"),
        fare_comparison=fare_comparison,
    )


def _p3_journey(journey: dict) -> UnifiedJourneyOut:
    """One phase 3 ``TrainChangeJourneyOut`` dict -> unified journey."""
    raw_segments = journey.get("segments") or []
    segments = [
        _segment_from_p34(segment, segment_type="FIRST_SEGMENT" if index == 1 else "TRAIN_CHANGE")
        for index, segment in enumerate(raw_segments, start=1)
    ]
    transitions = [_transition_from_p3(transfer) for transfer in journey.get("transfers") or []]
    distinct_trains = list(dict.fromkeys(segment.train_number for segment in segments))
    return UnifiedJourneyOut(
        journey_id=journey["journey_id"],
        journey_type="TRAIN_CHANGE",
        journey_status=JourneyVerification(journey["journey_status"]),
        from_station=journey["from_station"],
        to_station=journey["to_station"],
        journey_date=journey["journey_date"],
        departure=JourneyEndpointOut(**journey["departure"]),
        arrival=JourneyEndpointOut(**journey["arrival"]),
        departure_datetime=journey.get("departure_datetime"),
        arrival_datetime=journey.get("arrival_datetime"),
        overnight=bool(journey.get("overnight")),
        total_duration_minutes=journey.get("total_duration_minutes"),
        total_distance_km=_total_distance_km(segments),
        total_fare=journey.get("total_fare"),
        fares_complete=bool(journey.get("fares_complete")),
        segments_missing_fare=journey.get("segments_missing_fare") or [],
        segment_count=journey.get("segment_count") or len(segments),
        train_changes=journey.get("train_changes") or max(0, len(segments) - 1),
        seat_changes=0,
        total_transfer_wait_minutes=journey.get("total_transfer_wait_minutes"),
        distinct_train_count=len(distinct_trains),
        distinct_trains=distinct_trains,
        transitions=transitions,
        segments=segments,
        all_segments_confirmed_available=bool(journey.get("all_segments_confirmed_available")),
        unverified_segment_numbers=journey.get("unverified_segment_numbers") or [],
        availability_checked_at=journey.get("availability_checked_at"),
        oldest_availability_update_at=journey.get("oldest_availability_update_at"),
    )


def _p4_journey(journey: dict) -> UnifiedJourneyOut:
    """One phase 4 ``MixedJourneyOut`` dict -> unified journey (1:1)."""
    raw_segments = journey.get("segments") or []
    segments = [
        _segment_from_p34(segment, segment_type=segment.get("segment_type") or "TRAIN")
        for segment in raw_segments
    ]
    transitions = [_transition_from_p34(transition) for transition in journey.get("transitions") or []]
    distinct_trains = journey.get("distinct_trains") or list(
        dict.fromkeys(segment.train_number for segment in segments)
    )
    return UnifiedJourneyOut(
        journey_id=journey["journey_id"],
        journey_type=journey.get("journey_type") or "MIXED",
        journey_status=JourneyVerification(journey["journey_status"]),
        from_station=journey["from_station"],
        to_station=journey["to_station"],
        journey_date=journey["journey_date"],
        departure=JourneyEndpointOut(**journey["departure"]),
        arrival=JourneyEndpointOut(**journey["arrival"]),
        departure_datetime=journey.get("departure_datetime"),
        arrival_datetime=journey.get("arrival_datetime"),
        overnight=bool(journey.get("overnight")),
        total_duration_minutes=journey.get("total_duration_minutes"),
        total_distance_km=_total_distance_km(segments),
        total_fare=journey.get("total_fare"),
        fares_complete=bool(journey.get("fares_complete")),
        segments_missing_fare=journey.get("segments_missing_fare") or [],
        segment_count=journey.get("segment_count") or len(segments),
        train_changes=journey.get("train_changes") or 0,
        seat_changes=journey.get("seat_changes") or 0,
        total_transfer_wait_minutes=journey.get("total_transfer_wait_minutes"),
        distinct_train_count=journey.get("distinct_train_count") or len(distinct_trains),
        distinct_trains=distinct_trains,
        transitions=transitions,
        segments=segments,
        all_segments_confirmed_available=bool(journey.get("all_segments_confirmed_available")),
        unverified_segment_numbers=journey.get("unverified_segment_numbers") or [],
        availability_checked_at=journey.get("availability_checked_at"),
        oldest_availability_update_at=journey.get("oldest_availability_update_at"),
    )


# ------------------------------------------------------------------ public


def normalize_engine_response(
    engine: UnifiedEngineSelection, engine_body: dict
) -> tuple[list[UnifiedJourneyOut], list[UnifiedJourneyOut]]:
    """Normalize one engine's response body into unified journeys.

    Returns ``(confirmed_channel, unverified_channel)``. The phase 2
    engine has no unverified channel at all (its unavailable/unverified
    branches are simply never enumerated, so every combination is
    all-confirmed by construction) - the unverified list is always empty
    for it, which is a documented asymmetry, not a gap.
    """
    results: list[dict] = engine_body.get("results") or []
    unverified: list[dict] = engine_body.get("unverified_candidates") or []

    if engine == UnifiedEngineSelection.SAME_TRAIN:
        return [_p2_journey(combination) for combination in results], []
    if engine == UnifiedEngineSelection.TRAIN_CHANGE:
        return (
            [_p3_journey(journey) for journey in results],
            [_p3_journey(journey) for journey in unverified],
        )
    # MIXED
    return (
        [_p4_journey(journey) for journey in results],
        [_p4_journey(journey) for journey in unverified],
    )
