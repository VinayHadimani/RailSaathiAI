"""Conversational Agent Orchestrator for RailSaathi AI (Phase 6B.5).

Orchestrates multi-turn conversation sessions, intent/slot extraction,
ambiguity clarification, and deterministic invocation of the Unified Journey
Search engine. Never invents trains, fares, availability, or station codes.
"""

from __future__ import annotations

import logging
from typing import Any

from ..schemas.unified_search import (
    UnifiedEngineSelection,
    UnifiedRankingPreference,
    UnifiedSearchRequest,
)
from ..services.unified_journey_service import UnifiedJourneyOrchestrationService
from .extractor import IntentAndSlotExtractor
from .schemas import (
    AgentChatResponse,
    AgentSlots,
    AgentStateEnum,
    ExtractedIntentEnum,
    ExtractionResult,
    SessionState,
)
from .session_store import InMemorySessionStore

logger = logging.getLogger(__name__)


def _map_ranking_preference(pref_str: str | None) -> UnifiedRankingPreference:
    """Map slot ranking preference string to UnifiedRankingPreference enum."""
    if not pref_str:
        return UnifiedRankingPreference.FASTEST

    upper = pref_str.upper().strip()
    if upper == "CHEAPEST":
        return UnifiedRankingPreference.CHEAPEST
    if upper in ("FEWEST_TRANSFERS", "FEWEST_TRAIN_CHANGES"):
        return UnifiedRankingPreference.FEWEST_TRAIN_CHANGES
    if upper == "FEWEST_SEAT_CHANGES":
        return UnifiedRankingPreference.FEWEST_SEAT_CHANGES

    return UnifiedRankingPreference.FASTEST


def _rerank_results(
    results: list[dict[str, Any]],
    preference: UnifiedRankingPreference,
) -> list[dict[str, Any]]:
    """Deterministically re-rank confirmed journey results in memory."""
    copied = [dict(j) for j in results]
    if preference == UnifiedRankingPreference.CHEAPEST:
        copied.sort(
            key=lambda j: (
                0 if j.get("total_fare") is not None else 1,
                j.get("total_fare") or 0.0,
                j.get("total_duration_minutes") or 0,
                str(j.get("journey_id", "")),
            )
        )
    elif preference == UnifiedRankingPreference.FASTEST:
        copied.sort(
            key=lambda j: (
                j.get("total_duration_minutes") or 0,
                j.get("total_train_changes") or 0,
                j.get("total_seat_changes") or 0,
                str(j.get("journey_id", "")),
            )
        )
    elif preference == UnifiedRankingPreference.FEWEST_TRAIN_CHANGES:
        copied.sort(
            key=lambda j: (
                j.get("total_train_changes") or 0,
                j.get("total_duration_minutes") or 0,
                str(j.get("journey_id", "")),
            )
        )
    elif preference == UnifiedRankingPreference.FEWEST_SEAT_CHANGES:
        copied.sort(
            key=lambda j: (
                j.get("total_seat_changes") or 0,
                j.get("total_duration_minutes") or 0,
                str(j.get("journey_id", "")),
            )
        )
    return copied


class AgentOrchestrator:
    """Orchestrates natural language railway inquiries and invokes unified search."""

    def __init__(
        self,
        *,
        extractor: IntentAndSlotExtractor,
        session_store: InMemorySessionStore,
        unified_service: UnifiedJourneyOrchestrationService,
    ) -> None:
        self._extractor = extractor
        self._session_store = session_store
        self._unified_service = unified_service

    @property
    def session_store(self) -> InMemorySessionStore:
        return self._session_store

    @property
    def extractor(self) -> IntentAndSlotExtractor:
        return self._extractor

    def process_message(
        self,
        message: str,
        session_id: str | None = None,
        client_slots: AgentSlots | None = None,
    ) -> AgentChatResponse:
        """Process one conversational turn from the user.

        Args:
            message: User's natural-language journey inquiry.
            session_id: Optional client session UUID.
            client_slots: Optional client-held slots for serverless instance continuity.

        Returns:
            AgentChatResponse with updated state, message, slots, and search results.
        """
        # 1. Retrieve or create session
        session, is_new = self._session_store.get_or_create_session(session_id)
        sid = session.session_id
        if is_new and client_slots is not None:
            self._session_store.update_slots(sid, client_slots)
            session.slots = client_slots

        # Record user message in session history
        self._session_store.add_message(sid, "user", message)

        # 2. Run intent and slot extraction
        history_before = session.history[:-1]  # Exclude current message
        extraction: ExtractionResult = self._extractor.extract(
            message=message,
            current_slots=session.slots,
            history=history_before,
            ambiguous_candidates=session.ambiguous_candidates,
            unverified_stations=session.unverified_stations,
        )

        # 3. Update session state
        self._session_store.update_slots(sid, extraction.slots)
        self._session_store.update_ambiguous_candidates(sid, extraction.ambiguous_candidates)
        self._session_store.update_unverified_stations(sid, extraction.unverified_stations)

        slots = extraction.slots
        intent_str = extraction.intent.value

        # 4. Handle Greeting without journey details
        if (
            extraction.intent == ExtractedIntentEnum.GREETING
            and not slots.from_station
            and not slots.to_station
            and not slots.journey_date
        ):
            reply = (
                "Hello! I am RailSaathi AI, your Indian Railways journey discovery assistant. "
                "Where would you like to travel?"
            )
            self._session_store.add_message(sid, "assistant", reply)
            return AgentChatResponse(
                session_id=sid,
                state=AgentStateEnum.NEED_INFORMATION,
                status="GREETING",
                intent=intent_str,
                assistant_message=reply,
                reply=reply,
                slots=slots,
                missing_slots=["from_station", "to_station", "journey_date"],
                suggested_actions=["Bengaluru to Chennai", "Pune to Mumbai CSMT", "New Delhi to Mumbai CSMT"],
            )

        # 5. Handle Ambiguous Station Clusters
        if extraction.ambiguous_candidates:
            clarifications: list[str] = []
            suggested_actions: list[str] = []

            if "from_station" in extraction.ambiguous_candidates:
                c_list = ", ".join(
                    f"{c.name} ({c.code})" for c in extraction.ambiguous_candidates["from_station"]
                )
                clarifications.append(
                    f"Your departure city has multiple stations: {c_list}. Which station would you prefer?"
                )
                suggested_actions.extend([c.name for c in extraction.ambiguous_candidates["from_station"]][:4])

            if "to_station" in extraction.ambiguous_candidates:
                c_list = ", ".join(
                    f"{c.name} ({c.code})" for c in extraction.ambiguous_candidates["to_station"]
                )
                clarifications.append(
                    f"Your destination city has multiple stations: {c_list}. Which station would you prefer?"
                )
                suggested_actions.extend([c.name for c in extraction.ambiguous_candidates["to_station"]][:4])

            reply = " ".join(clarifications)
            self._session_store.add_message(sid, "assistant", reply)
            return AgentChatResponse(
                session_id=sid,
                state=AgentStateEnum.NEED_INFORMATION,
                status="NEEDS_CLARIFICATION",
                intent=intent_str,
                assistant_message=reply,
                reply=reply,
                slots=slots,
                missing_slots=extraction.missing_slots,
                ambiguous_candidates=extraction.ambiguous_candidates,
                suggested_actions=suggested_actions,
            )

        # 6. Handle Unverified Station Codes
        if extraction.unverified_stations:
            unverified_codes = list(extraction.unverified_stations.values())
            reply = (
                f"Station code '{unverified_codes[0]}' is an unverified station code not found "
                "in our catalog. Please provide the full station name or city so I can find verified routes."
            )
            self._session_store.add_message(sid, "assistant", reply)
            return AgentChatResponse(
                session_id=sid,
                state=AgentStateEnum.NEED_INFORMATION,
                status="NEEDS_CLARIFICATION",
                intent=intent_str,
                assistant_message=reply,
                reply=reply,
                slots=slots,
                missing_slots=extraction.missing_slots,
            )

        # 7. Handle Missing Mandatory Information (Origin, Destination, or Date)
        if extraction.missing_slots:
            reply = " ".join(extraction.clarifications_needed) if extraction.clarifications_needed else (
                "Please provide your departure station, destination station, and travel date."
            )
            self._session_store.add_message(sid, "assistant", reply)
            return AgentChatResponse(
                session_id=sid,
                state=AgentStateEnum.NEED_INFORMATION,
                status="NEEDS_CLARIFICATION",
                intent=intent_str,
                assistant_message=reply,
                reply=reply,
                slots=slots,
                missing_slots=extraction.missing_slots,
            )

        # 7.5 Same-station guard: a search where origin == destination can
        # never produce results (and would fail request validation with a
        # raw HTTP 500). Ask for a different station instead.
        if (
            slots.from_station
            and slots.to_station
            and slots.from_station.upper() == slots.to_station.upper()
        ):
            reply = (
                f"Your departure and destination are the same station ({slots.from_station}). "
                "Please tell me a different destination station."
            )
            self._session_store.add_message(sid, "assistant", reply)
            return AgentChatResponse(
                session_id=sid,
                state=AgentStateEnum.NEED_INFORMATION,
                status="NEEDS_CLARIFICATION",
                intent=intent_str,
                assistant_message=reply,
                reply=reply,
                slots=slots,
                missing_slots=["to_station"],
            )

        # 8. All Mandatory Slots are Present and Verified -> Execute Unified Journey Search
        ranking_pref = _map_ranking_preference(slots.ranking_preference)

        current_request_spec = {
            "from_station": slots.from_station,
            "to_station": slots.to_station,
            "journey_date": slots.journey_date,
            "travel_class": slots.travel_class or "SL",
            "quota": slots.quota or "GN",
            "ranking_preference": ranking_pref.value,
        }

        # Check Cache: Repeat Request or Preference Change Only
        last_req = session.last_search_request
        last_resp = session.last_search_response

        if last_req and last_resp:
            # A) Exact Duplicate Request
            if last_req == current_request_spec:
                results = last_resp.get("results") or []
                unverified = last_resp.get("unverified_candidates") or []
                completeness = last_resp.get("search_completeness")
                reply = (
                    f"Showing confirmed journey options from {slots.from_station} to {slots.to_station} "
                    f"on {slots.journey_date} in {slots.travel_class} class ({slots.quota} quota). "
                    f"(Retrieved from active session)."
                )
                self._session_store.add_message(sid, "assistant", reply)
                return AgentChatResponse(
                    session_id=sid,
                    state=AgentStateEnum.SEARCH_COMPLETED if results else AgentStateEnum.NO_RESULTS,
                    status="RESULTS_FOUND" if results else "NO_CONFIRMED_RESULTS",
                    intent=intent_str,
                    assistant_message=reply,
                    reply=reply,
                    slots=slots,
                    search_triggered=False,
                    search_results=results,
                    unverified_candidates=unverified,
                    completeness=completeness,
                )

            # B) Ranking Preference Change Only -> Local In-Memory Re-Ranking
            same_journey = (
                last_req.get("from_station") == current_request_spec["from_station"]
                and last_req.get("to_station") == current_request_spec["to_station"]
                and last_req.get("journey_date") == current_request_spec["journey_date"]
                and last_req.get("travel_class") == current_request_spec["travel_class"]
                and last_req.get("quota") == current_request_spec["quota"]
            )
            if same_journey and last_req.get("ranking_preference") != current_request_spec["ranking_preference"]:
                re_ranked = _rerank_results(last_resp.get("results") or [], ranking_pref)
                updated_resp = dict(last_resp)
                updated_resp["results"] = re_ranked
                updated_resp["ranked_by"] = ranking_pref.value
                self._session_store.cache_search_result(sid, current_request_spec, updated_resp)

                unverified = updated_resp.get("unverified_candidates") or []
                completeness = updated_resp.get("search_completeness")
                reply = (
                    f"Re-ranked confirmed journey options from {slots.from_station} to {slots.to_station} "
                    f"by {ranking_pref.value}."
                )
                self._session_store.add_message(sid, "assistant", reply)
                return AgentChatResponse(
                    session_id=sid,
                    state=AgentStateEnum.SEARCH_COMPLETED if re_ranked else AgentStateEnum.NO_RESULTS,
                    status="RESULTS_FOUND" if re_ranked else "NO_CONFIRMED_RESULTS",
                    intent=intent_str,
                    assistant_message=reply,
                    reply=reply,
                    slots=slots,
                    search_triggered=False,
                    search_results=re_ranked,
                    unverified_candidates=unverified,
                    completeness=completeness,
                )

        # 9. Construct UnifiedSearchRequest (inside the guard so any
        # validation error degrades to a friendly reply instead of HTTP 500)
        try:
            unified_request = UnifiedSearchRequest(
                from_station=slots.from_station,
                to_station=slots.to_station,
                journey_date=slots.journey_date,
                travel_class=slots.travel_class or "SL",
                quota=slots.quota or "GN",
                ranking_preference=ranking_pref,
                engines=UnifiedEngineSelection.all_engines(),
            )
            body, status_code = self._unified_service.run(unified_request)
        except Exception as exc:
            logger.exception("Unified journey search crashed: %s", exc)
            reply = f"Railway search failed due to an internal error: {type(exc).__name__}. Please try again."
            self._session_store.add_message(sid, "assistant", reply)
            return AgentChatResponse(
                session_id=sid,
                state=AgentStateEnum.SEARCH_FAILED,
                status="ERROR",
                intent=intent_str,
                assistant_message=reply,
                reply=reply,
                slots=slots,
                search_triggered=True,
            )

        # 10. Process Unified Search Response
        if status_code != 200:
            err_msg = body.get("message") or "Provider check failed"
            reply = f"Railway availability check failed: {err_msg}."
            self._session_store.add_message(sid, "assistant", reply)
            return AgentChatResponse(
                session_id=sid,
                state=AgentStateEnum.SEARCH_FAILED,
                status="ERROR",
                intent=intent_str,
                assistant_message=reply,
                reply=reply,
                slots=slots,
                search_triggered=True,
            )

        # Cache search result in active session
        self._session_store.cache_search_result(sid, current_request_spec, body)

        results = body.get("results") or []
        unverified = body.get("unverified_candidates") or []
        completeness = body.get("search_completeness")

        if results:
            reply = (
                f"Found {len(results)} confirmed journey option(s) from {slots.from_station} to {slots.to_station} "
                f"on {slots.journey_date} in {slots.travel_class} class ({slots.quota} quota)."
            )
            status_str = "RESULTS_FOUND"
            state_enum = AgentStateEnum.SEARCH_COMPLETED
        else:
            status_str = "NO_CONFIRMED_RESULTS"
            state_enum = AgentStateEnum.NO_RESULTS
            if unverified:
                reply = (
                    f"No fully confirmed available seats found from {slots.from_station} to {slots.to_station} "
                    f"on {slots.journey_date}. Found {len(unverified)} unverified option(s) with waitlist/RAC or unconfirmed segments."
                )
            else:
                reply = (
                    f"No confirmed trains or combination routes available from {slots.from_station} to {slots.to_station} "
                    f"on {slots.journey_date} for class {slots.travel_class} ({slots.quota} quota)."
                )

        self._session_store.add_message(sid, "assistant", reply)
        return AgentChatResponse(
            session_id=sid,
            state=state_enum,
            status=status_str,
            intent=intent_str,
            assistant_message=reply,
            reply=reply,
            slots=slots,
            search_triggered=True,
            search_results=results,
            unverified_candidates=unverified,
            completeness=completeness,
        )
