"""HTTP routes.

Phase 1 feasibility test:

    POST /api/v1/test/segment-availability

Phase 2 same-train combination search:

    POST /api/v1/journeys/same-train/search

Phase 3 train-change connection search:

    POST /api/v1/journeys/train-change/search

Phase 4 mixed journey search (same-train seat changes + train changes):

    POST /api/v1/journeys/mixed/search

Phase 5 unified journey search (all three engines, one merged answer):

    POST /api/v1/journeys/search

Auxiliary routes: GET /api/v1/health and GET /api/v1/config/status
(the latter exposes booleans only - never the API key).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from ..agent.orchestrator import AgentOrchestrator
from ..agent.schemas import AgentChatRequest, AgentChatResponse
from ..core.config import get_settings
from ..providers.railcore import RailCoreClient
from ..schemas.journey_search import (
    SameTrainSearchRequest,
    SameTrainSearchResponse,
)
from ..schemas.segment_test import (
    SegmentAvailabilityTestRequest,
    SegmentAvailabilityTestResponse,
)
from ..schemas.train_change_search import (
    TrainChangeSearchRequest,
    TrainChangeSearchResponse,
)
from ..schemas.mixed_search import (
    MixedSearchRequest,
    MixedSearchResponse,
)
from ..schemas.unified_search import (
    UnifiedSearchRequest,
    UnifiedSearchResponse,
)
from ..services.same_train_combination_service import SameTrainCombinationService
from ..services.segment_availability_service import SegmentAvailabilityTestService
from ..services.train_change_connection_service import TrainChangeConnectionService
from ..services.mixed_journey_service import MixedJourneyCombinationService
from ..services.unified_journey_service import UnifiedJourneyOrchestrationService
from .deps import get_agent_orchestrator, get_railcore_client

router = APIRouter(prefix="/api/v1")


@router.get("/health", tags=["system"], summary="Liveness and configuration check")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "railcore_configured": settings.railcore_api_key_is_configured,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


@router.get(
    "/config/status",
    tags=["system"],
    summary="Provider configuration status (booleans only, no secrets)",
)
def config_status() -> dict:
    settings = get_settings()
    return {
        "provider": "railcore",
        "base_url": settings.railcore_base_url,
        "api_key_configured": settings.railcore_api_key_is_configured,
        "hmac_signing_enabled": settings.railcore_hmac_enabled,
        "timeout_seconds": settings.railcore_timeout_seconds,
        "max_retries": settings.railcore_max_retries,
        "note": (
            "Set RAILCORE_API_KEY in backend/.env (see .env.example) if "
            "api_key_configured is false."
        ),
    }


@router.post(
    "/journeys/same-train/search",
    tags=["journeys"],
    summary="Same-train segment combination search",
    description=(
        "Finds trains whose complete from_station -> to_station journey can be "
        "covered by MULTIPLE consecutive station-to-station segments on the SAME "
        "train, with EVERY segment explicitly reporting confirmed availability "
        "(provider status AVAILABLE).\n\n"
        "**How it works**\n\n"
        "1. Candidate trains are discovered for the route and date (trains that "
        "do not carry the requested class are skipped).\n"
        "2. Each candidate's full schedule is fetched and the stop window "
        "between the requested stations is extracted (forward order, actual "
        "stops only).\n"
        "3. Segment availability is checked with deduplicated, batched queries. "
        "A stop is only expanded when a fully confirmed partition reaches it, "
        "so unavailable branches never cost credits.\n"
        "4. Every valid partition of ANY size is enumerated (no fixed maximum "
        "number of segments or seat changes); configurable budgets cap the "
        "search and are clearly reported when hit.\n"
        "5. Results include per-segment availability and fares, total fare, "
        "duration (overnight-safe via day offsets), seat changes and a "
        "comparison against the direct through-route fare, ranked by "
        "cheapest / fastest / fewest changes.\n\n"
        "**Per-segment journey dates.** A segment boarded after midnight is "
        "queried with the calendar date the train departs that boarding "
        "station (IRCTC semantics); all segments still belong to the same "
        "train run.\n\n"
        "**What never qualifies:** RAC, waitlist, regret, not available, "
        "unknown, class not offered, failed items and provider errors. A "
        "provider error is never treated as unavailability - affected "
        "possibilities are reported as unverified.\n\n"
        "**This endpoint never books tickets** and does not claim that "
        "separate segment tickets on one train are officially permitted; "
        "railway ticketing rules must be verified before booking."
    ),
    response_model=SameTrainSearchResponse,
    responses={
        502: {"description": "RailCore provider failure (candidate discovery could not run)"},
        503: {"description": "RailCore API key is not configured"},
    },
)
def same_train_search(
    payload: SameTrainSearchRequest,
    client: RailCoreClient = Depends(get_railcore_client),
) -> Response:
    service = SameTrainCombinationService(client)
    body, status_code = service.run(payload)
    return JSONResponse(status_code=status_code, content=body)


@router.post(
    "/journeys/train-change/search",
    tags=["journeys"],
    summary="Train-change connection search",
    description=(
        "Finds complete journeys that require CHANGING TRAINS at "
        "intermediate stations (any number of changes - no fixed limit), "
        "where EVERY segment explicitly reports confirmed availability "
        "(provider status AVAILABLE).\n\n"
        "**How it works**\n\n"
        "1. Through trains are discovered for the route; their schedules "
        "reveal the corridor's stations. Discovery pairs are only ever "
        "built from stations that appear in fetched schedules - never by "
        "blindly enumerating station pairs - and every pair costs one "
        "budgeted request.\n"
        "2. A chronological (earliest-arrival) search expands connections "
        "from the source toward the destination over the schedule-derived "
        "leg graph. Each train change is validated at the SAME station "
        "code (both trains must actually stop there), inside a configurable "
        "minimum transfer buffer and maximum transfer wait, within a "
        "configurable arrival horizon, with overnight-safe day offsets, "
        "correct per-segment boarding dates, valid station order, no "
        "station revisits, and consecutive legs on different trains.\n"
        "3. Availability is checked per segment with deduplicated, batched "
        "queries through the shared budget-aware planner; budgets are "
        "enforced BEFORE any request. Only an explicit AVAILABLE extends "
        "the search. RAC, waitlist, regret, not-available and unknown "
        "never qualify.\n"
        "4. A provider error is never treated as an unavailable seat: "
        "journeys whose availability could not be fully determined are "
        "returned separately as PARTIALLY_VERIFIED candidates, never as "
        "confirmed results.\n"
        "5. Results are ranked cheapest / fastest / fewest changes with "
        "totals for fare (never estimated), door-to-door duration, "
        "transfers and waiting time. Direct single-train journeys are not "
        "results of this engine; discovered through trains are listed "
        "without any availability claim.\n\n"
        "When a configured limit (budget, time, horizon or exploration "
        "cap) stops the search early, the response says so explicitly "
        "(status SEARCH_LIMIT_REACHED + incomplete_reasons) - it never "
        "claims all possibilities were checked.\n\n"
        "**This endpoint never books tickets.**"
    ),
    response_model=TrainChangeSearchResponse,
    responses={
        502: {"description": "RailCore provider failure (discovery could not run)"},
        503: {"description": "RailCore API key is not configured"},
    },
)
def train_change_search(
    payload: TrainChangeSearchRequest,
    client: RailCoreClient = Depends(get_railcore_client),
) -> Response:
    service = TrainChangeConnectionService(client)
    body, status_code = service.run(payload)
    return JSONResponse(status_code=status_code, content=body)


@router.post(
    "/journeys/mixed/search",
    tags=["journeys"],
    summary="Mixed journey search (same-train seat changes + train changes)",
    description=(
        "Finds complete journeys that combine SAME-TRAIN seat changes (multiple "
        "consecutive ticket segments on one train run - phase 2 semantics) with "
        "TRAIN CHANGES at interchange stations (phase 3 semantics), in any order "
        "and ANY number of either kind, where EVERY individual ticket segment "
        "explicitly reports confirmed availability (provider status AVAILABLE).\n\n"
        "**How it works**\n\n"
        "1. An orchestration layer reuses the train-change engine's discovery, "
        "corridor learning, schedule pool, transfer validation, budget "
        "enforcement and deduplicated batched availability queries unchanged.\n"
        "2. The chronological earliest-arrival search is extended with ONE new "
        "edge type: after alighting at station X on train T (run R), the "
        "passenger may keep riding the SAME run R onward from X - a same-train "
        "SEAT CHANGE (the passenger stays onboard through the halt; no transfer "
        "buffer applies, the ticket boundary changes). Boarding a different "
        "train remains a validated TRAIN CHANGE inside the configurable "
        "transfer window.\n"
        "3. Journeys are chronological sequences of individual ticket segments: "
        "consecutive segments on the same train run form seat-change groups; "
        "transitions to other trains are transfers with explicit transfer "
        "blocks. Per-segment boarding dates follow IRCTC semantics (the calendar "
        "date the train departs the boarding station), overnight-safe via day "
        "offsets.\n"
        "4. Only CONFIRMED_AVAILABLE legs extend the search. RAC, waitlist, "
        "regret, not-available and unknown never qualify. A provider error is "
        "never treated as an unavailable seat: journeys whose availability "
        "could not be fully determined are returned separately as "
        "PARTIALLY_VERIFIED candidates, never as confirmed results.\n"
        "5. There is NO fixed maximum of seat changes or train changes; "
        "computational budgets (requests, credits, time, discovery pairs, "
        "expanded states, journeys, horizon) cap the search and are clearly "
        "reported when hit (status SEARCH_LIMIT_REACHED + incomplete_reasons) - "
        "the response never claims all possibilities were checked.\n"
        "6. Results are ranked cheapest / fastest / fewest train changes / "
        "fewest seat changes with totals for fare (never estimated), "
        "door-to-door duration, transfer waiting time, segment count and "
        "distinct trains. A single-segment source -> destination journey is a "
        "direct train: listed for reference without any availability claim.\n\n"
        "**This endpoint never books tickets.** Same-train seat changes are "
        "separate tickets on one train: Indian Railways ticketing rules for "
        "split journeys must be verified before relying on them."
    ),
    response_model=MixedSearchResponse,
    responses={
        502: {"description": "RailCore provider failure (discovery could not run)"},
        503: {"description": "RailCore API key is not configured"},
    },
)
def mixed_search(
    payload: MixedSearchRequest,
    client: RailCoreClient = Depends(get_railcore_client),
) -> Response:
    service = MixedJourneyCombinationService(client)
    body, status_code = service.run(payload)
    return JSONResponse(status_code=status_code, content=body)


@router.post(
    "/journeys/search",
    tags=["journeys"],
    summary="Unified journey search (all engines, one merged answer)",
    description=(
        "One search coordinates the three existing engines - SAME_TRAIN "
        "(same-train segment combinations), TRAIN_CHANGE (train-change "
        "connections) and MIXED (mixed journeys) - normalizes their outputs "
        "into ONE unified journey representation, merges the results, removes "
        "duplicates, re-validates availability and re-ranks everything with "
        "one deterministic rule. The engines, their algorithms and their "
        "standalone endpoints are reused unchanged; select a subset through "
        "the request's engines field.\n\n"
        "**How it works**\n\n"
        "1. All engines run inside ONE shared budget envelope (maximum API "
        "requests, maximum estimated credits, one wall-clock deadline): the "
        "shared budget-aware query planner enforces the limits BEFORE any "
        "provider call, the counters are never reset between engines, and an "
        "engine whose turn arrives after the envelope is exhausted is skipped "
        "with an honestly reported reason. Identical availability queries "
        "made by different engines are deduplicated against one shared memo, "
        "so the same physical segment is checked (and paid for) once.\n"
        "2. Results are merged and deduplicated by segment signature "
        "(train number, from sequence, to sequence, boarding date): the same "
        "physical journey found by several engines appears once with every "
        "source engine recorded; different segment partitions remain "
        "distinct journeys.\n"
        "3. Availability is re-validated from the normalized segments "
        "themselves: only journeys whose EVERY segment explicitly reports "
        "confirmed availability (provider status AVAILABLE) stay in results. "
        "Journeys containing WAITLIST, RAC, NOT_AVAILABLE, UNKNOWN or "
        "PROVIDER_ERROR segments are returned separately as unverified "
        "candidates with their diagnostic information preserved - a provider "
        "error is never treated as unavailability, and an unverified journey "
        "is never labelled AVAILABLE_COMBINATION.\n"
        "4. Results are re-ranked over the merged set (cheapest / fastest / "
        "fewest train changes / fewest seat changes) with one deterministic "
        "tie-break; per-engine orderings are never concatenated. Unknown "
        "fares rank below journeys with known fares for CHEAPEST; fares are "
        "never estimated.\n"
        "5. If one engine fails, its error is recorded and the other engines "
        "continue (while the shared budget permits); confirmed results from "
        "successful engines are preserved and partial search completeness "
        "is reported honestly.\n\n"
        "**This endpoint never books tickets.** Same-train seat changes are "
        "separate tickets on one train; Indian Railways ticketing rules for "
        "split journeys must be verified before relying on them."
    ),
    response_model=UnifiedSearchResponse,
    responses={
        502: {"description": "RailCore provider failure (no engine produced results)"},
        503: {"description": "RailCore API key is not configured"},
    },
)
def unified_search(
    payload: UnifiedSearchRequest,
    client: RailCoreClient = Depends(get_railcore_client),
) -> Response:
    service = UnifiedJourneyOrchestrationService(client)
    body, status_code = service.run(payload)
    return JSONResponse(status_code=status_code, content=body)


@router.post(
    "/test/segment-availability",
    tags=["feasibility"],
    summary="Same-train, two-segment seat availability feasibility test",
    description=(
        "Fetches the real schedule of the requested train, verifies that all "
        "four stations are on the route with segment 1 preceding segment 2, "
        "then checks seat availability for both segments independently using "
        "the SAME train number, journey date, travel class and quota.\n\n"
        "Only an explicit provider AVAILABLE status counts as confirmed "
        "availability. RAC, waitlist, regret, unavailable and unknown statuses "
        "never count. A provider error is reported as PROVIDER_ERROR and is "
        "never interpreted as unavailability."
    ),
    response_model=SegmentAvailabilityTestResponse,
    responses={
        502: {"description": "RailCore provider failure (schedule could not be fetched)"},
        503: {"description": "RailCore API key is not configured"},
    },
)
def segment_availability_test(
    payload: SegmentAvailabilityTestRequest,
    client: RailCoreClient = Depends(get_railcore_client),
) -> Response:
    service = SegmentAvailabilityTestService(client)
    body, status_code = service.run(payload)
    return JSONResponse(status_code=status_code, content=body)


@router.post(
    "/agent/chat",
    tags=["agent"],
    summary="Conversational journey discovery assistant",
    description=(
        "Conversational entry point for RailSaathi AI. Understands natural-language "
        "journey inquiries, manages conversation state across turns, clarifies "
        "ambiguities or missing parameters, and deterministically coordinates with "
        "the unified journey search engine once all mandatory parameters are verified."
    ),
    response_model=AgentChatResponse,
)
def agent_chat(
    payload: AgentChatRequest,
    orchestrator: AgentOrchestrator = Depends(get_agent_orchestrator),
) -> AgentChatResponse:
    return orchestrator.process_message(
        message=payload.message,
        session_id=payload.session_id,
        client_slots=payload.slots,
    )
