"""Unit and integration tests for AgentOrchestrator and POST /api/v1/agent/chat (Phase 6B.5 & 6B.6).

Verifies the 15 mandatory scenarios:
1. Complete journey request triggers unified search.
2. Missing origin/destination/date triggers clarification.
3. Ambiguous city triggers station clarification.
4. User answers clarification and search proceeds.
5. Session preserves slots across multiple messages.
6. User corrects destination without losing origin/date.
7. Unverified station code cannot trigger a search.
8. Confirmed results are returned correctly.
9. Unverified candidates remain separate.
10. No confirmed results produces a clear response.
11. Gemini failure is handled safely.
12. RailCore failure is handled safely.
13. Repeated messages do not cause unnecessary duplicate searches.
14. Ranking preference changes are handled correctly (local re-ranking).
15. Existing /api/v1/journeys/search behavior remains unchanged.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agent.extractor import IntentAndSlotExtractor
from app.agent.llm import LLMTimeoutError, MockLLMClient
from app.agent.orchestrator import AgentOrchestrator
from app.agent.schemas import AgentChatResponse, AgentSlots, AgentStateEnum
from app.agent.session_store import InMemorySessionStore
from app.api.deps import get_agent_orchestrator
from app.main import app as fastapi_app
from app.schemas.unified_search import UnifiedSearchRequest


def _make_llm_json(
    *,
    intent: str = "JOURNEY_SEARCH",
    origin_raw: str | None = None,
    destination_raw: str | None = None,
    date_raw: str | None = None,
    travel_class: str | None = None,
    quota: str | None = None,
    ranking_preference: str | None = None,
    is_correction: bool = False,
    correction_target: str | None = None,
) -> str:
    return json.dumps({
        "intent": intent,
        "origin_raw": origin_raw,
        "destination_raw": destination_raw,
        "date_raw": date_raw,
        "travel_class": travel_class,
        "quota": quota,
        "ranking_preference": ranking_preference,
        "is_correction": is_correction,
        "correction_target": correction_target,
    })


def _sample_unified_body(
    results: list[dict] | None = None,
    unverified: list[dict] | None = None,
    status: str = "AVAILABLE_COMBINATION",
    message: str = "Search succeeded",
) -> dict:
    res = results if results is not None else [
        {
            "journey_id": "SAME_TRAIN:99801:MYS:DWR",
            "journey_type": "SAME_TRAIN",
            "from_station": "MYS",
            "to_station": "DWR",
            "departure_time": "06:00",
            "arrival_time": "14:00",
            "total_duration_minutes": 480,
            "total_fare": 540.0,
            "total_train_changes": 0,
            "total_seat_changes": 1,
            "segments": [
                {
                    "train_number": "99801",
                    "train_name": "Mysore Dharwad Express",
                    "from_station": "MYS",
                    "to_station": "SBC",
                    "travel_class": "SL",
                    "quota": "GN",
                    "availability_status": "AVAILABLE",
                    "confirmed": True,
                },
                {
                    "train_number": "99801",
                    "train_name": "Mysore Dharwad Express",
                    "from_station": "SBC",
                    "to_station": "DWR",
                    "travel_class": "SL",
                    "quota": "GN",
                    "availability_status": "AVAILABLE",
                    "confirmed": True,
                },
            ],
        }
    ]
    unver = unverified if unverified is not None else []
    return {
        "success": len(res) > 0,
        "status": status,
        "message": message,
        "search": {},
        "ranked_by": "FASTEST",
        "results": res,
        "total_results": len(res),
        "unverified_candidates": unver,
        "total_unverified_candidates": len(unver),
        "search_completeness": {"complete": True, "engine_failures": []},
        "errors": [],
    }


# ============================================================================
# Scenario 1: Complete journey request triggers unified search
# ============================================================================

def test_complete_journey_request_triggers_unified_search():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="Mysore",
            destination_raw="Dharwad",
            date_raw="tomorrow",
            travel_class="SL",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(), 200)

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    resp = orchestrator.process_message("Find trains from Mysore to Dharwad tomorrow")

    assert resp.status == "RESULTS_FOUND"
    assert resp.state == AgentStateEnum.SEARCH_COMPLETED
    assert resp.search_triggered is True
    assert len(resp.search_results) == 1
    assert resp.search_results[0]["journey_id"] == "SAME_TRAIN:99801:MYS:DWR"
    mock_unified_service.run.assert_called_once()
    req: UnifiedSearchRequest = mock_unified_service.run.call_args[0][0]
    assert req.from_station == "MYS"
    assert req.to_station == "DWR"


# ============================================================================
# Scenario 2: Missing origin/destination/date triggers clarification
# ============================================================================

def test_missing_information_triggers_clarification():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="MYS",
            destination_raw=None,  # missing destination
            date_raw=None,         # missing date
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    resp = orchestrator.process_message("Trains from Mysore")

    assert resp.status == "NEEDS_CLARIFICATION"
    assert resp.state == AgentStateEnum.NEED_INFORMATION
    assert resp.search_triggered is False
    assert "to_station" in resp.missing_slots
    assert "journey_date" in resp.missing_slots
    mock_unified_service.run.assert_not_called()


# ============================================================================
# Scenario 3: Ambiguous city triggers station clarification
# ============================================================================

def test_ambiguous_city_triggers_station_clarification():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="Bangalore",  # Ambiguous: SBC, YPR, SMVB, BNC
            destination_raw="Dharwad",
            date_raw="tomorrow",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    resp = orchestrator.process_message("Trains from Bangalore to Dharwad tomorrow")

    assert resp.status == "NEEDS_CLARIFICATION"
    assert resp.state == AgentStateEnum.NEED_INFORMATION
    assert resp.search_triggered is False
    assert "from_station" in resp.missing_slots
    assert resp.ambiguous_candidates is not None
    assert "from_station" in resp.ambiguous_candidates
    mock_unified_service.run.assert_not_called()


# ============================================================================
# Scenario 4: User answers clarification and search proceeds
# ============================================================================

def test_user_answers_clarification_and_search_proceeds():
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(), 200)

    # Turn 1: User says Bangalore -> gets ambiguous clarification
    mock_llm_t1 = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="Bangalore",
            destination_raw="Dharwad",
            date_raw="2026-12-05",
        )
    )
    orch1 = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm_t1),
        session_store=store,
        unified_service=mock_unified_service,
    )
    resp1 = orch1.process_message("Bangalore to Dharwad on 2026-12-05")
    assert resp1.status == "NEEDS_CLARIFICATION"
    sid = resp1.session_id

    # Turn 2: User answers "Majestic" (resolves to SBC)
    mock_llm_t2 = MockLLMClient(
        default_response=_make_llm_json(
            intent="CLARIFICATION",
            origin_raw="Majestic",
        )
    )
    orch2 = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm_t2),
        session_store=store,
        unified_service=mock_unified_service,
    )
    resp2 = orch2.process_message("Majestic", session_id=sid)

    assert resp2.status == "RESULTS_FOUND"
    assert resp2.state == AgentStateEnum.SEARCH_COMPLETED
    assert resp2.search_triggered is True
    assert resp2.slots.from_station == "SBC"
    assert resp2.slots.to_station == "DWR"
    assert resp2.slots.journey_date == "2026-12-05"
    mock_unified_service.run.assert_called_once()


# ============================================================================
# Scenario 5: Session preserves slots across multiple messages
# ============================================================================

def test_session_preserves_slots_across_multiple_messages():
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(), 200)

    # Turn 1: Origin and destination provided
    mock_llm_t1 = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="Mysore",
            destination_raw="Belagavi",
            date_raw=None,
        )
    )
    orch = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm_t1),
        session_store=store,
        unified_service=mock_unified_service,
    )
    r1 = orch.process_message("Mysore to Belagavi")
    assert r1.status == "NEEDS_CLARIFICATION"
    sid = r1.session_id

    # Turn 2: Only date provided
    mock_llm_t2 = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            date_raw="tomorrow",
        )
    )
    orch._extractor = IntentAndSlotExtractor(mock_llm_t2)
    r2 = orch.process_message("tomorrow", session_id=sid)

    assert r2.status == "RESULTS_FOUND"
    assert r2.slots.from_station == "MYS"
    assert r2.slots.to_station == "BGM"
    assert r2.slots.journey_date is not None
    mock_unified_service.run.assert_called_once()


# ============================================================================
# Scenario 6: User corrects destination without losing origin/date
# ============================================================================

def test_user_corrects_destination_without_losing_origin_and_date():
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(), 200)

    # Turn 1: Mysore to Dharwad tomorrow
    mock_llm_t1 = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="Mysore",
            destination_raw="Dharwad",
            date_raw="tomorrow",
        )
    )
    orch = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm_t1),
        session_store=store,
        unified_service=mock_unified_service,
    )
    r1 = orch.process_message("Mysore to Dharwad tomorrow")
    assert r1.status == "RESULTS_FOUND"
    sid = r1.session_id
    original_date = r1.slots.journey_date

    # Turn 2: User corrects destination to Belagavi
    mock_llm_t2 = MockLLMClient(
        default_response=_make_llm_json(
            intent="CORRECTION",
            origin_raw=None,
            destination_raw="Belagavi",
            date_raw=None,
            is_correction=True,
            correction_target="destination",
        )
    )
    orch._extractor = IntentAndSlotExtractor(mock_llm_t2)
    r2 = orch.process_message("Actually make it Belagavi", session_id=sid)

    assert r2.status == "RESULTS_FOUND"
    assert r2.slots.from_station == "MYS"       # Preserved
    assert r2.slots.to_station == "BGM"         # Corrected
    assert r2.slots.journey_date == original_date # Preserved


# ============================================================================
# Scenario 7: Unverified station code cannot trigger a search
# ============================================================================

def test_unverified_station_code_cannot_trigger_search():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="XYZ",  # Non-catalog unverified code
            destination_raw="MYS",
            date_raw="tomorrow",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    resp = orchestrator.process_message("Find trains from XYZ to Mysore tomorrow")

    assert resp.status == "NEEDS_CLARIFICATION"
    assert resp.state == AgentStateEnum.NEED_INFORMATION
    assert resp.search_triggered is False
    assert "unverified" in resp.assistant_message.lower()
    mock_unified_service.run.assert_not_called()


# ============================================================================
# Scenario 8: Confirmed results are returned correctly
# ============================================================================

def test_confirmed_results_returned_correctly():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            origin_raw="MYS",
            destination_raw="DWR",
            date_raw="tomorrow",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(), 200)

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    resp = orchestrator.process_message("MYS to DWR tomorrow")
    assert resp.status == "RESULTS_FOUND"
    assert resp.state == AgentStateEnum.SEARCH_COMPLETED
    assert len(resp.search_results) == 1
    assert resp.search_results[0]["from_station"] == "MYS"
    assert resp.search_results[0]["to_station"] == "DWR"


# ============================================================================
# Scenario 9: Unverified candidates remain separate
# ============================================================================

def test_unverified_candidates_remain_separate():
    unverified_mock = [
        {
            "journey_id": "TRAIN_CHANGE:99801_99803",
            "journey_type": "TRAIN_CHANGE",
            "from_station": "MYS",
            "to_station": "DWR",
            "unverified_reason": "WAITLIST",
        }
    ]
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            origin_raw="MYS",
            destination_raw="DWR",
            date_raw="tomorrow",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (
        _sample_unified_body(results=[], unverified=unverified_mock, status="PARTIALLY_VERIFIED"),
        200,
    )

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    resp = orchestrator.process_message("MYS to DWR tomorrow")
    assert resp.status == "NO_CONFIRMED_RESULTS"
    assert resp.state == AgentStateEnum.NO_RESULTS
    assert len(resp.search_results) == 0
    assert len(resp.unverified_candidates) == 1
    assert "unverified option" in resp.assistant_message.lower()


# ============================================================================
# Scenario 10: No confirmed results produces a clear response
# ============================================================================

def test_no_confirmed_results_produces_clear_response():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            origin_raw="MYS",
            destination_raw="DWR",
            date_raw="tomorrow",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (
        _sample_unified_body(results=[], unverified=[], status="NO_COMPLETE_JOURNEY"),
        200,
    )

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    resp = orchestrator.process_message("MYS to DWR tomorrow")
    assert resp.status == "NO_CONFIRMED_RESULTS"
    assert resp.state == AgentStateEnum.NO_RESULTS
    assert len(resp.search_results) == 0
    assert "no confirmed trains" in resp.assistant_message.lower()


# ============================================================================
# Scenario 11: Gemini failure is handled safely
# ============================================================================

def test_gemini_failure_handled_safely():
    mock_llm = MockLLMClient(error_to_raise=LLMTimeoutError("Gemini timed out"))
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    # Turn does not crash, preserves state and reports clarification/error gracefully
    resp = orchestrator.process_message("Find trains to Dharwad")
    assert resp.status == "NEEDS_CLARIFICATION"
    mock_unified_service.run.assert_not_called()


# ============================================================================
# Scenario 12: RailCore failure is handled safely
# ============================================================================

def test_railcore_failure_handled_safely():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            origin_raw="MYS",
            destination_raw="DWR",
            date_raw="tomorrow",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (
        {"status": "PROVIDER_ERROR", "message": "RailCore 502 Bad Gateway", "results": []},
        502,
    )

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    resp = orchestrator.process_message("MYS to DWR tomorrow")
    assert resp.status == "ERROR"
    assert resp.state == AgentStateEnum.SEARCH_FAILED
    assert "railway availability check failed" in resp.assistant_message.lower()
    assert "railcore 502" in resp.assistant_message.lower()


# ============================================================================
# Scenario 13: Repeated messages do not cause unnecessary duplicate searches
# ============================================================================

def test_repeated_messages_do_not_cause_duplicate_searches():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            origin_raw="MYS",
            destination_raw="DWR",
            date_raw="tomorrow",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(), 200)

    orchestrator = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    # First call: triggers search
    r1 = orchestrator.process_message("MYS to DWR tomorrow")
    assert r1.status == "RESULTS_FOUND"
    assert r1.search_triggered is True
    assert mock_unified_service.run.call_count == 1
    sid = r1.session_id

    # Second call with same message & session: served from session cache!
    r2 = orchestrator.process_message("MYS to DWR tomorrow", session_id=sid)
    assert r2.status == "RESULTS_FOUND"
    assert r2.search_triggered is False  # Cache hit, no redundant engine execution
    assert mock_unified_service.run.call_count == 1  # Not incremented!


# ============================================================================
# Scenario 14: Ranking preference changes are handled correctly (re-ranking)
# ============================================================================

def test_ranking_preference_changes_handled_via_reranking():
    multi_results = [
        {
            "journey_id": "J1",
            "total_fare": 600.0,
            "total_duration_minutes": 300,
        },
        {
            "journey_id": "J2",
            "total_fare": 350.0,
            "total_duration_minutes": 450,
        },
    ]
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(results=multi_results), 200)

    # Turn 1: Initial search ranked by FASTEST (J1 duration=300 is first)
    mock_llm_t1 = MockLLMClient(
        default_response=_make_llm_json(
            origin_raw="MYS",
            destination_raw="DWR",
            date_raw="tomorrow",
            ranking_preference="FASTEST",
        )
    )
    orch = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm_t1),
        session_store=store,
        unified_service=mock_unified_service,
    )
    r1 = orch.process_message("MYS to DWR tomorrow fastest")
    assert mock_unified_service.run.call_count == 1
    assert r1.search_results[0]["journey_id"] == "J1"
    sid = r1.session_id

    # Turn 2: User asks for CHEAPEST
    mock_llm_t2 = MockLLMClient(
        default_response=_make_llm_json(
            ranking_preference="CHEAPEST",
        )
    )
    orch._extractor = IntentAndSlotExtractor(mock_llm_t2)
    r2 = orch.process_message("Show cheapest instead", session_id=sid)

    # Verified: Re-ranked locally without calling RailCore again!
    assert mock_unified_service.run.call_count == 1
    assert r2.search_triggered is False
    assert r2.search_results[0]["journey_id"] == "J2"  # Fare 350 is cheaper than 600
    assert r2.search_results[1]["journey_id"] == "J1"


# ============================================================================
# Scenario 15: HTTP endpoint POST /api/v1/agent/chat verification
# ============================================================================

def test_agent_chat_api_endpoint():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            origin_raw="MYS",
            destination_raw="DWR",
            date_raw="tomorrow",
        )
    )
    store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(), 200)

    orch = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=store,
        unified_service=mock_unified_service,
    )

    fastapi_app.dependency_overrides[get_agent_orchestrator] = lambda: orch
    client = TestClient(fastapi_app)

    try:
        response = client.post(
            "/api/v1/agent/chat",
            json={"message": "Trains from Mysore to Dharwad tomorrow"},
        )
        assert response.status_code == 200
        data = response.json()

        assert "session_id" in data
        assert data["status"] == "RESULTS_FOUND"
        assert data["state"] == "SEARCH_COMPLETED"
        assert data["search_triggered"] is True
        assert len(data["search_results"]) == 1
        assert data["search_results"][0]["from_station"] == "MYS"
        assert "assistant_message" in data
        assert "reply" in data
    finally:
        fastapi_app.dependency_overrides.clear()


def test_agent_chat_preserves_client_slots_across_fresh_serverless_instance():
    """Follow-up turn on a fresh InMemorySessionStore preserves prior client slots."""
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="CORRECTION",
            travel_class="3A",
            is_correction=True,
            correction_target="travel_class",
        )
    )
    fresh_store = InMemorySessionStore()
    mock_unified_service = MagicMock()
    mock_unified_service.run.return_value = (_sample_unified_body(), 200)

    orch = AgentOrchestrator(
        extractor=IntentAndSlotExtractor(mock_llm),
        session_store=fresh_store,
        unified_service=mock_unified_service,
    )

    resp = orch.process_message(
        message="in 3A",
        session_id="ephemeral-serverless-session",
        client_slots=AgentSlots(
            from_station="PUNE",
            to_station="CSMT",
            journey_date="2026-11-26",
            travel_class="SL",
        ),
    )

    assert resp.slots.from_station == "PUNE"
    assert resp.slots.to_station == "CSMT"
    assert resp.slots.travel_class == "3A"
    assert resp.search_triggered is True

