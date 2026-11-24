"""Budget-aware, deduplicating provider query layer for the search engine.

Responsibilities (spec section 18 - API credit optimization):
- Deduplicate identical availability queries within one search (memo).
- Batch availability queries through POST /availability/seats/batch
  (up to 25 items per request, one travel class per item).
- Enforce API-request and credit budgets BEFORE spending them.
- Cache schedule data (stable timetables, long TTL) and route discovery
  results (short TTL) across searches within the process.
- Optional client-side pacing between provider HTTP calls to stay under
  the provider's per-minute rate limit.
- NEVER interpret a provider failure as availability or unavailability:
  failed segments come back with status UNKNOWN / PROVIDER_ERROR and an
  error payload, and the engine treats them as unverified.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..models.availability import SegmentAvailability
from ..models.route_search import RouteTrainsResult
from ..models.schedule import TrainSchedule
from ..providers.base import (
    InternalAvailabilityStatus,
    ProviderNotConfiguredError,
    RailwayProviderError,
)
from ..providers.railcore import RailCoreClient
from .search_budgets import SearchBudgets

logger = logging.getLogger("railroute.services.query_planner")


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class SegmentQueryOutcome:
    """The engine-facing result of one segment availability query."""

    from_station: str
    to_station: str
    journey_date: str  # the per-segment boarding date actually queried
    status: InternalAvailabilityStatus
    availability: SegmentAvailability | None = None
    error: dict | None = None  # provider/item failure details (never a claim)
    checked_at: str | None = None


# Reasons a batch round stopped early (never silent; surfaced by the engine).
STOP_BUDGET_REQUESTS = "BUDGET_REQUESTS_EXHAUSTED"
STOP_BUDGET_CREDITS = "BUDGET_CREDITS_EXHAUSTED"
STOP_TIME_LIMIT = "TIME_LIMIT_REACHED"
STOP_PROVIDER_ERROR = "PROVIDER_ERROR"


class SegmentQueryPlanner:
    """Provider-facing query layer used by the combination engines.

    Availability queries are deduplicated per search (instance memo) but
    never cached across searches (freshness). Schedules and route
    discovery results are cached process-wide with a TTL, because
    timetables are stable and discovery is train-list data, not live
    availability.
    """

    # Process-wide caches shared by all planner instances.
    _shared_schedule_cache: dict[str, tuple[float, TrainSchedule]] = {}
    _shared_discovery_cache: dict[
        tuple[str, str, str, str | None], tuple[float, RouteTrainsResult]
    ] = {}

    @classmethod
    def clear_shared_caches(cls) -> None:
        """Test hook: drop all cross-search schedule/discovery caches."""
        cls._shared_schedule_cache.clear()
        cls._shared_discovery_cache.clear()

    def __init__(self, client: RailCoreClient, budgets: SearchBudgets) -> None:
        self.client = client
        self.budgets = budgets
        # (train, from, to, date, class, quota) -> outcome; dedup within a search.
        self._memo: dict[tuple[str, str, str, str, str, str], SegmentQueryOutcome] = {}
        self.provider_errors: list[dict] = []
        self.stats: dict[str, int] = {
            "requests_sent": 0,  # provider HTTP calls made through the planner
            "availability_batches": 0,
            "availability_items_sent": 0,
            "distinct_availability_queries": 0,
            "deduplicated_queries": 0,
            "schedule_cache_hits": 0,
            "schedule_fetches": 0,
            "discovery_cache_hits": 0,
            "discovery_fetches": 0,
            "estimated_credits": 0,  # item-count estimate (batch items + calls)
        }
        self._last_request_at = 0.0

    # ------------------------------------------------------------- budgets

    def requests_available(self) -> bool:
        return self.stats["requests_sent"] < self.budgets.max_api_requests

    def credits_available(self, needed: int = 1) -> bool:
        return self.stats["estimated_credits"] + needed <= self.budgets.max_credits

    def _respect_rate_limit(self) -> None:
        """Optional client-side pacing between provider HTTP calls."""
        interval = self.budgets.min_request_interval_seconds
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_at = time.monotonic()

    # -------------------------------------------------------------- caches

    def get_schedule(self, train_number: str) -> TrainSchedule:
        """Fetch (or reuse a cached) train schedule.

        Raises RailwayProviderError on failure - never a fake schedule.
        """
        train_number = train_number.strip()
        cached = self._shared_schedule_cache.get(train_number)
        if cached is not None and (time.monotonic() - cached[0]) <= self.budgets.schedule_cache_ttl_seconds:
            self.stats["schedule_cache_hits"] += 1
            return cached[1]
        if not self.requests_available():
            raise _BudgetExhaustedError(STOP_BUDGET_REQUESTS)
        self._respect_rate_limit()
        call = self.client.get_train_schedule(train_number)  # raises on failure
        self.stats["requests_sent"] += 1
        self.stats["schedule_fetches"] += 1
        self.stats["estimated_credits"] += call.credits_cost or 1
        schedule = call.data
        self._shared_schedule_cache[train_number] = (time.monotonic(), schedule)
        return schedule

    def get_trains_between(
        self, *, from_station: str, to_station: str, journey_date: str, quota: str
    ) -> RouteTrainsResult:
        """Discover trains serving the route (or reuse a fresh cached result).

        Raises RailwayProviderError on failure (including NO_TRAINS_FOUND
        as a ProviderClientError) - never an invented train list.
        """
        cache_key = (from_station, to_station, journey_date, quota)
        cached = self._shared_discovery_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) <= self.budgets.discovery_cache_ttl_seconds:
            self.stats["discovery_cache_hits"] += 1
            return cached[1]
        if not self.requests_available():
            raise _BudgetExhaustedError(STOP_BUDGET_REQUESTS)
        self._respect_rate_limit()
        call = self.client.get_trains_between_stations(
            from_station=from_station,
            to_station=to_station,
            journey_date=journey_date,
            quota=quota,
        )
        self.stats["requests_sent"] += 1
        self.stats["discovery_fetches"] += 1
        self.stats["estimated_credits"] += call.credits_cost or 1
        result = call.data
        self._shared_discovery_cache[cache_key] = (time.monotonic(), result)
        return result

    # ------------------------------------------------------- availability

    def query_segments(
        self,
        *,
        train_number: str,
        segments: list[tuple[str, str, str]],
        travel_class: str,
        quota: str,
        deadline: float | None = None,
    ) -> tuple[dict[tuple[str, str], SegmentQueryOutcome], str | None]:
        """Check availability for segment queries, deduplicated and batched.

        Args:
            segments: (from_station, to_station, boarding_query_date) tuples.
                Multiple tuples may share a station pair (deduplicated).
            travel_class: single class for every item (1 credit per item).
            quota: reservation quota for every item.
            deadline: monotonic timestamp after which querying must stop.

        Returns:
            (outcomes by (from, to) station pair, stop_reason or None).
            Stop reasons are budget/time/provider failures - the engine
            must treat unqueried segments as UNVERIFIED, never unavailable.
        """
        outcomes: dict[tuple[str, str], SegmentQueryOutcome] = {}
        pending: list[tuple[str, str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()

        for frm, to, query_date in segments:
            key = (train_number, frm, to, query_date, travel_class, quota)
            memoized = self._memo.get(key)
            if memoized is not None:
                outcomes[(frm, to)] = memoized
                continue
            if (frm, to) in seen_pairs:
                continue  # same pair with a different date: first wins (documented)
            seen_pairs.add((frm, to))
            pending.append((frm, to, query_date))

        if not pending:
            return outcomes, None

        self.stats["distinct_availability_queries"] += len(pending)
        stop_reason: str | None = None

        while pending:
            if deadline is not None and time.monotonic() > deadline:
                stop_reason = STOP_TIME_LIMIT
                break
            if not self.requests_available():
                stop_reason = STOP_BUDGET_REQUESTS
                break
            credits_room = self.budgets.max_credits - self.stats["estimated_credits"]
            if credits_room <= 0:
                stop_reason = STOP_BUDGET_CREDITS
                break
            size = min(self.budgets.batch_size, credits_room)
            chunk, pending = pending[:size], pending[size:]

            items = [
                {
                    "train_number": train_number,
                    "from": frm,
                    "to": to,
                    "date": query_date,
                    "classes": [travel_class],
                    "quota": quota,
                }
                for frm, to, query_date in chunk
            ]
            self._respect_rate_limit()
            try:
                batch = self.client.get_batch_availability(items)
            except ProviderNotConfiguredError:
                raise
            except RailwayProviderError as exc:
                error_dict = exc.to_dict()
                self.provider_errors.append(error_dict)
                logger.warning(
                    "batch_availability_failed train=%s items=%d code=%s",
                    train_number, len(chunk), error_dict.get("code"),
                )
                checked_at = _utc_now_iso()
                for frm, to, query_date in chunk:
                    outcome = SegmentQueryOutcome(
                        from_station=frm,
                        to_station=to,
                        journey_date=query_date,
                        status=InternalAvailabilityStatus.PROVIDER_ERROR,
                        error=error_dict,
                        checked_at=checked_at,
                    )
                    memo_key = (train_number, frm, to, query_date, travel_class, quota)
                    self._memo[memo_key] = outcome
                    outcomes[(frm, to)] = outcome
                stop_reason = STOP_PROVIDER_ERROR
                break

            # Request succeeded: count usage. Budget accounting uses the
            # documented cost model (1 credit per distinct class per item)
            # because budgets must be enforced BEFORE the request is sent;
            # header-reported credits are surfaced separately in api_usage.
            self.stats["requests_sent"] += 1
            self.stats["availability_batches"] += 1
            self.stats["availability_items_sent"] += len(chunk)
            self.stats["estimated_credits"] += len(chunk)

            by_pair: dict[tuple[str, str], SegmentAvailability] = {}
            for item in batch.items:
                by_pair[(item.from_station, item.to_station)] = item
            failed_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
            for failed in batch.failed_items:
                pair = (
                    str(failed.get("from_station_code") or ""),
                    str(failed.get("to_station_code") or ""),
                )
                failed_by_pair[pair] = failed

            for frm, to, query_date in chunk:
                memo_key = (train_number, frm, to, query_date, travel_class, quota)
                availability = by_pair.get((frm, to))
                if availability is not None:
                    status = availability.status
                    error = None
                elif (frm, to) in failed_by_pair:
                    # In-place item failure (e.g. BOOKING_NOT_OPEN): carries
                    # NO availability information whatsoever. Surfaced as an
                    # error so the response never hides it.
                    failed = failed_by_pair[(frm, to)]
                    status = InternalAvailabilityStatus.UNKNOWN
                    error = {
                        "code": str(failed.get("error") or "ITEM_FAILED"),
                        "category": "PROVIDER_ITEM",
                        "message": str(failed.get("message") or "The provider could not answer this item."),
                        "retryable": True,
                        "details": {
                            "train_number": train_number,
                            "from_station": frm,
                            "to_station": to,
                            "journey_date": query_date,
                        },
                    }
                    self.provider_errors.append(error)
                else:
                    status = InternalAvailabilityStatus.UNKNOWN
                    error = {
                        "code": "MISSING_BATCH_RESULT",
                        "category": "PROTOCOL",
                        "message": "The batch response did not contain a result for this item.",
                        "retryable": True,
                        "details": {"train_number": train_number, "from_station": frm, "to_station": to},
                    }
                outcome = SegmentQueryOutcome(
                    from_station=frm,
                    to_station=to,
                    journey_date=query_date,
                    status=status,
                    availability=availability,
                    error=error,
                    checked_at=batch.checked_at,
                )
                self._memo[memo_key] = outcome
                outcomes[(frm, to)] = outcome

        self.stats["deduplicated_queries"] += (
            self.stats["distinct_availability_queries"] - self.stats["availability_items_sent"]
        )
        return outcomes, stop_reason


class _BudgetExhaustedError(RailwayProviderError):
    """Internal signal: a budget stopped a cache-miss fetch.

    Not a provider failure - the engine converts it into a partial-search
    status instead of a provider error.
    """

    category = "BUDGET"

    def __init__(self, code: str) -> None:
        super().__init__(
            "The search budget stopped this provider call before it was sent.",
            code=code,
            retryable=False,
        )
