"""Pydantic schemas for the RailSaathi AI conversational agent (Phase 6B).

Defines the contract for station resolution, date normalization, slot
extraction, and the agent chat API.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentStateEnum(str, Enum):
    """Runtime conversational states for the agent."""

    NEED_INFORMATION = "NEED_INFORMATION"
    SEARCH_COMPLETED = "SEARCH_COMPLETED"
    NO_RESULTS = "NO_RESULTS"
    SEARCH_FAILED = "SEARCH_FAILED"


class StationCandidate(BaseModel):
    """A plausible railway station candidate."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(..., description="Indian Railways station code (e.g. 'SBC').")
    name: str = Field(..., description="Full official station name.")
    city: str = Field(..., description="City or urban agglomeration.")
    state: str = Field("", description="State or union territory.")


class StationResolutionStatus(str, Enum):
    """Status outcomes for station resolution."""

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNVERIFIED_CODE = "UNVERIFIED_CODE"
    UNKNOWN = "UNKNOWN"
    INVALID_CODE = "INVALID_CODE"


class StationResolutionResult(BaseModel):
    """Result of resolving a raw station query."""

    status: StationResolutionStatus
    code: str | None = Field(None, description="Resolved IR station code if unambiguous or unverified code.")
    name: str | None = Field(None, description="Resolved station name (None if unverified).")
    city: str | None = Field(None, description="Resolved station city (None if unverified).")
    candidates: list[StationCandidate] = Field(
        default_factory=list,
        description="Candidate stations when ambiguous.",
    )
    error: str | None = Field(None, description="Explanation when unknown, unverified, or invalid.")


class DateResolutionStatus(str, Enum):
    """Status outcomes for date normalization."""

    RESOLVED = "RESOLVED"
    PAST_DATE = "PAST_DATE"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    INVALID_FORMAT = "INVALID_FORMAT"


class DateResolutionResult(BaseModel):
    """Result of normalizing a raw date expression."""

    status: DateResolutionStatus
    resolved_date: date | None = Field(None, description="Normalized Python date object.")
    iso_date: str | None = Field(None, description="ISO-8601 formatted date (YYYY-MM-DD).")
    error: str | None = Field(None, description="Explanation when invalid or past.")


class AgentSlots(BaseModel):
    """Journey parameters accumulated across conversational turns."""

    from_station: str | None = Field(None, description="Boarding station code (e.g. 'MYS').")
    to_station: str | None = Field(None, description="Destination station code (e.g. 'DWR').")
    journey_date: str | None = Field(None, description="Travel date in YYYY-MM-DD format.")
    travel_class: str = Field("SL", description="Travel class (default 'SL').")
    quota: str = Field("GN", description="Quota code (default 'GN').")
    ranking_preference: str = Field("FASTEST", description="Ranking preference (default 'FASTEST').")
    via_stations: list[str] = Field(default_factory=list, description="Optional intermediate station hints.")


class AgentChatRequest(BaseModel):
    """Client request for the conversational chat endpoint."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": "c1f7a012-706d-495d-9860-9dfc12658a2e",
                "message": "Find trains from Bangalore to Dharwad tomorrow morning in sleeper class",
            }
        }
    )

    session_id: str | None = Field(
        None,
        description="Client session UUID. If omitted or null, a new session is generated.",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="User's natural-language journey inquiry.",
    )
    slots: AgentSlots | None = Field(
        None,
        description="Optional client-held slots to preserve multi-turn continuity across serverless instances.",
    )


class AgentChatResponse(BaseModel):
    """Response returned by the conversational chat endpoint."""

    session_id: str = Field(..., description="Active session UUID.")
    state: AgentStateEnum = Field(..., description="Current agent state machine state.")
    status: str = Field("NEEDS_CLARIFICATION", description="Standardized status code.")
    intent: str = Field("JOURNEY_SEARCH", description="Detected intent.")
    assistant_message: str = Field(..., description="Assistant's natural language conversational reply.")
    reply: str = Field(..., description="Alias for assistant_message.")
    slots: AgentSlots = Field(..., description="Accumulated extracted journey slots.")
    missing_slots: list[str] = Field(
        default_factory=list,
        description="Mandatory slots still missing ('from_station', 'to_station', 'journey_date').",
    )
    ambiguous_candidates: dict[str, list[StationCandidate]] | None = Field(
        None,
        description="Candidate stations when from_station or to_station refers to an ambiguous city.",
    )
    search_triggered: bool = Field(False, description="True if backend unified search was executed.")
    search_results: list[dict[str, Any]] | None = Field(
        None,
        description="Confirmed journey alternatives from unified engine.",
    )
    unverified_candidates: list[dict[str, Any]] | None = Field(
        None,
        description="Unverified candidate journeys (waitlist/RAC/unknown) kept strictly separate from confirmed results.",
    )
    completeness: dict[str, Any] | None = Field(
        None,
        description="Search completeness details.",
    )
    suggested_actions: list[str] = Field(
        default_factory=list,
        description="Follow-up quick action prompts or buttons for the UI.",
    )


class SessionMessage(BaseModel):
    """A single turn in the conversation history."""

    role: str = Field(..., description="'user' or 'assistant'")
    content: str = Field(..., description="Message text.")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionState(BaseModel):
    """In-memory session state capturing conversation history and accumulated slots."""

    session_id: str = Field(..., description="Unique UUID identifier for the session.")
    slots: AgentSlots = Field(default_factory=AgentSlots, description="Current journey slots.")
    history: list[SessionMessage] = Field(default_factory=list, description="Recent conversation turns.")
    ambiguous_candidates: dict[str, list[StationCandidate]] = Field(
        default_factory=dict,
        description="Unresolved station clusters awaiting user selection ('from_station' or 'to_station').",
    )
    unverified_stations: dict[str, str] = Field(
        default_factory=dict,
        description="Unverified station codes awaiting user verification ('from_station' or 'to_station').",
    )
    last_search_request: dict[str, Any] | None = Field(
        None,
        description="Cached parameters of the most recent unified search payload.",
    )
    last_search_response: dict[str, Any] | None = Field(
        None,
        description="Cached verbatim response dictionary of the most recent unified search.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExtractedIntentEnum(str, Enum):
    """Categorized user intent."""

    JOURNEY_SEARCH = "JOURNEY_SEARCH"
    CORRECTION = "CORRECTION"
    GREETING = "GREETING"
    CLARIFICATION = "CLARIFICATION"
    OTHER = "OTHER"


class RawLLMExtraction(BaseModel):
    """Structured extraction directly produced by LLM JSON output."""

    intent: ExtractedIntentEnum = ExtractedIntentEnum.JOURNEY_SEARCH
    origin_raw: str | None = None
    destination_raw: str | None = None
    date_raw: str | None = None
    travel_class: str | None = None
    quota: str | None = None
    ranking_preference: str | None = None
    is_correction: bool = False
    correction_target: str | None = None


class ExtractionResult(BaseModel):
    """Validated intent and slot extraction result combining LLM output with deterministic validation."""

    intent: ExtractedIntentEnum
    slots: AgentSlots
    missing_slots: list[str] = Field(default_factory=list)
    ambiguous_candidates: dict[str, list[StationCandidate]] = Field(default_factory=dict)
    unverified_stations: dict[str, str] = Field(default_factory=dict)
    date_error: str | None = None
    clarifications_needed: list[str] = Field(default_factory=list)
    is_correction: bool = False
    raw_llm_response: str | None = None
    error: str | None = None


__all__ = [
    "AgentStateEnum",
    "StationCandidate",
    "StationResolutionStatus",
    "StationResolutionResult",
    "DateResolutionStatus",
    "DateResolutionResult",
    "AgentSlots",
    "AgentChatRequest",
    "AgentChatResponse",
    "SessionMessage",
    "SessionState",
    "ExtractedIntentEnum",
    "RawLLMExtraction",
    "ExtractionResult",
]
