"""Internal domain models (provider-agnostic, normalized)."""

from .schedule import ScheduleStop, TrainSchedule
from .availability import (
    ClassAvailability,
    ConfirmationProbability,
    SegmentAvailability,
)
from .route_search import AvailabilityMini, RouteTrainSummary, RouteTrainsResult

__all__ = [
    "ScheduleStop",
    "TrainSchedule",
    "ClassAvailability",
    "ConfirmationProbability",
    "SegmentAvailability",
    "AvailabilityMini",
    "RouteTrainSummary",
    "RouteTrainsResult",
]
