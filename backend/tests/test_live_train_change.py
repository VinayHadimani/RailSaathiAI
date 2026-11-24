"""OPT-IN live API tests (Phase 3) - SKIPPED BY DEFAULT.

These tests call the REAL RailCore API and therefore consume credits.
They are completely separated from the normal test suite and only run
when BOTH of the following hold:

1. The environment variable RUN_LIVE_API_TESTS=1 is set (explicit opt-in).
2. A real RAILCORE_API_KEY is configured (backend/.env or environment).

They never run as part of normal development or CI (``python -m pytest``
skips them). Budgets are deliberately tiny: a full run costs only a
handful of credits. Phase 3 was developed and verified entirely with
mocked transports; these tests exist only for explicit live validation.

Run them consciously:

    RUN_LIVE_API_TESTS=1 python -m pytest tests/test_live_train_change.py -m live_api
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.schemas.train_change_search import TrainChangeSearchRequest
from app.services.search_budgets import TrainChangeBudgets
from app.services.train_change_connection_service import TrainChangeConnectionService

from app.api.deps import get_railcore_client

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

pytestmark = [
    pytest.mark.live_api,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_API_TESTS") != "1",
        reason="opt-in only: set RUN_LIVE_API_TESTS=1 to run live API tests (they consume credits)",
    ),
    pytest.mark.skipif(
        not get_settings().railcore_api_key_is_configured,
        reason="RAILCORE_API_KEY is not configured",
    ),
]


def test_live_train_change_search_tiny_budget():
    """One real search with a deliberately tiny budget (about 6 requests).

    Proves the endpoint works against the live provider end to end while
    spending only a handful of credits. Availability claims in the
    response are whatever the provider says for the requested date -
    no outcome is asserted as 'available'; only the honesty of the
    response contract is checked.
    """
    client = get_railcore_client()
    try:
        settings = get_settings()
        budgets = TrainChangeBudgets(
            max_api_requests=6,
            max_credits=6,
            max_discovery_pairs=3,
            max_trains=3,
            max_expanded_states=10,
            max_journeys=5,
            max_results=5,
            time_budget_seconds=20.0,
            schedule_cache_ttl_seconds=settings.search_schedule_cache_ttl_seconds,
            discovery_cache_ttl_seconds=settings.search_discovery_cache_ttl_seconds,
        )
        service = TrainChangeConnectionService(client, budgets)
        request = TrainChangeSearchRequest(
            from_station="MYS",
            to_station="DWR",
            journey_date=(datetime.now(tz=IST).date() + timedelta(days=4)).isoformat(),
            travel_class="3A",
            quota="GN",
            ranking_preference="FEWEST_CHANGES",
        )
        body, http_status = service.run(request)
        assert http_status in (200, 502, 503)

        # Budget honesty: never more requests than configured.
        usage = body["search_completeness"]["budget"]
        assert usage["requests_used"] <= budgets.max_api_requests
        assert usage["credits_used_estimate"] <= budgets.max_credits
        assert usage["discovery_pairs_used"] <= budgets.max_discovery_pairs

        # Result honesty: whatever came back respects the contract.
        for journey in body["results"]:
            assert journey["all_segments_confirmed_available"] is True
            assert journey["journey_status"] == "AVAILABLE_COMBINATION"
            for segment in journey["segments"]:
                assert segment["status"] == "CONFIRMED_AVAILABLE"
        for candidate in body["unverified_candidates"]:
            assert candidate["journey_status"] == "PARTIALLY_VERIFIED"
            assert candidate["all_segments_confirmed_available"] is False
        assert "BOOKING_CONFIRMED" not in body["status"]
    finally:
        client.close()
