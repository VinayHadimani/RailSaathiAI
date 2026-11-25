"""Intent and slot extraction engine for RailSaathi AI (Phase 6B.4).

Combines LLM semantic understanding with deterministic station resolution
and date normalization. Never invents station codes, never invents travel
dates, and never overrides deterministic validation with LLM output.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final

from ..services.local_timetable_store import get_local_timetable_store
from .date_normalizer import DateNormalizer, get_current_date_ist
from .llm.base import LLMClient
from .llm.exceptions import LLMError
from .schemas import (
    AgentSlots,
    DateResolutionStatus,
    ExtractedIntentEnum,
    ExtractionResult,
    RawLLMExtraction,
    SessionMessage,
    StationCandidate,
    StationResolutionStatus,
)
from .station_resolver import StationResolver

logger = logging.getLogger(__name__)

VALID_CLASSES: Final[set[str]] = {
    "SL", "3A", "2A", "1A", "CC", "2S", "3E", "EC", "EA", "EV", "FC",
}

VALID_QUOTAS: Final[set[str]] = {
    "GN", "TQ", "PT", "LD", "SS", "HP", "DF", "FT",
}

VALID_RANKINGS: Final[set[str]] = {
    "FASTEST", "CHEAPEST", "FEWEST_TRANSFERS", "MAX_CONFIRMATION",
}

EXTRACTION_SYSTEM_PROMPT: Final[str] = """You are the natural language intent and slot extraction engine for RailSaathi AI, an intelligent Indian Railways journey assistant.

Your task is to analyze the user's message in the context of any existing journey slots and conversation history, and extract structured parameters.

Output MUST be a single raw JSON object with these exact keys:
{
  "intent": "JOURNEY_SEARCH" | "CORRECTION" | "GREETING" | "CLARIFICATION" | "OTHER",
  "origin_raw": "string or null",
  "destination_raw": "string or null",
  "date_raw": "string or null",
  "travel_class": "SL" | "3A" | "2A" | "1A" | "CC" | "2S" | null,
  "quota": "GN" | "TQ" | "PT" | "LD" | null,
  "ranking_preference": "FASTEST" | "CHEAPEST" | "FEWEST_TRANSFERS" | "MAX_CONFIRMATION" | null,
  "is_correction": true | false,
  "correction_target": "origin" | "destination" | "date" | "class" | "quota" | null
}

CRITICAL RULES:
1. Do NOT guess or invent railway station codes. Return the user's raw words for station/city in origin_raw and destination_raw (e.g., 'Bangalore', 'Mysore', 'NDLS', 'Majestic').
2. If the user does not specify an origin, return null for origin_raw.
3. If the user does not specify a destination, return null for destination_raw.
4. If the user does not mention any travel date, return null for date_raw. NEVER invent a date.
5. If the user provides a relative date (e.g. 'tomorrow', 'next Monday', 'day after tomorrow', '28th Dec'), return the exact raw expression in date_raw.
6. Set is_correction to true if the user is explicitly correcting or modifying an already-stated parameter (e.g. 'Actually make it Dharwad', 'No, from Mysore', 'Change date to Friday').
7. If the user is just answering a clarification (e.g. selecting a station like 'Majestic' or 'SBC'), intent is 'CLARIFICATION'.
8. If the user is saying hello/hi, intent is 'GREETING'.
9. Output valid JSON only, without any explanation or conversational text.
"""


def _extract_json_from_llm_text(text: str) -> dict[str, Any] | None:
    """Robustly extract and parse a JSON dictionary from raw LLM output text."""
    if not text or not text.strip():
        return None

    cleaned = text.strip()

    # Strip markdown code fences if present
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # Locate outermost { and } if surrounded by prose
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("Failed to parse JSON from LLM text: %s. Raw: %r", exc, text[:200])

    return None


class IntentAndSlotExtractor:
    """Orchestrates LLM-based slot extraction combined with deterministic validation."""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        booking_window_enforce: bool = False,
    ) -> None:
        self._llm = llm_client
        self._booking_window_enforce = booking_window_enforce

    @property
    def llm_client(self) -> LLMClient:
        return self._llm

    def _build_prompt(
        self,
        message: str,
        current_slots: AgentSlots,
        history: list[SessionMessage],
        ambiguous_candidates: dict[str, list[StationCandidate]],
    ) -> str:
        history_lines = [f"{msg.role}: {msg.content}" for msg in history[-6:]]
        history_str = "\n".join(history_lines) if history_lines else "None (first turn)"

        ambiguities_lines = []
        for slot, candidates in ambiguous_candidates.items():
            c_str = ", ".join(f"{c.name} ({c.code})" for c in candidates)
            ambiguities_lines.append(f"- {slot}: {c_str}")
        ambiguities_str = "\n".join(ambiguities_lines) if ambiguities_lines else "None"

        return f"""Conversation History:
{history_str}

Current Accumulated Slots:
- Origin: {current_slots.from_station or "Not specified"}
- Destination: {current_slots.to_station or "Not specified"}
- Date: {current_slots.journey_date or "Not specified"}
- Class: {current_slots.travel_class}
- Quota: {current_slots.quota}
- Ranking Preference: {current_slots.ranking_preference}

Active Ambiguities from Previous Turn:
{ambiguities_str}

New User Message:
"{message}"

Extract parameters as JSON:"""

    def _handle_clarification_shortcut(
        self,
        user_message: str,
        ambiguous_candidates: dict[str, list[StationCandidate]],
    ) -> tuple[str | None, str | None]:
        """Check if user message directly resolves an active ambiguity.

        Returns:
            Tuple of (slot_name, resolved_code) if directly resolved, else (None, None).
        """
        clean_msg = user_message.strip()
        if clean_msg.lower() in ("any", "anyone", "any one", "whichever", "first", "main", "default"):
            for slot_name, candidates in ambiguous_candidates.items():
                if candidates:
                    return slot_name, candidates[0].code

        # Test resolving directly with StationResolver
        res = StationResolver.resolve(clean_msg)
        if res.status == StationResolutionStatus.RESOLVED and res.code:
            for slot_name, candidates in ambiguous_candidates.items():
                if any(c.code == res.code for c in candidates):
                    return slot_name, res.code

        # Check candidate names/codes in message
        for slot_name, candidates in ambiguous_candidates.items():
            for c in candidates:
                if c.code.lower() == clean_msg.lower() or c.name.lower() in clean_msg.lower():
                    return slot_name, c.code

        return None, None

    def extract(
        self,
        message: str,
        *,
        current_slots: AgentSlots | None = None,
        history: list[SessionMessage] | None = None,
        ambiguous_candidates: dict[str, list[StationCandidate]] | None = None,
        unverified_stations: dict[str, str] | None = None,
    ) -> ExtractionResult:
        """Extract journey parameters from user message and apply deterministic validation.

        Args:
            message: User's natural-language inquiry.
            current_slots: Pre-existing journey slots from active session.
            history: Recent conversation history.
            ambiguous_candidates: Unresolved station clusters from previous turn.
            unverified_stations: Unverified station codes from previous turn.

        Returns:
            ExtractionResult with updated slots, validation status, and clarification items.
        """
        slots = (current_slots.model_copy(deep=True) if current_slots else AgentSlots())
        active_history = list(history) if history else []
        active_ambiguities = (
            {k: list(v) for k, v in ambiguous_candidates.items()}
            if ambiguous_candidates
            else {}
        )
        active_unverified = dict(unverified_stations) if unverified_stations else {}

        # 1. Fast-path check: Did user directly resolve an active ambiguity?
        clarified_slot, clarified_code = self._handle_clarification_shortcut(
            message, active_ambiguities
        )

        prompt = self._build_prompt(
            message=message,
            current_slots=slots,
            history=active_history,
            ambiguous_candidates=active_ambiguities,
        )

        # 2. Call LLM for structured extraction
        raw_llm_text: str | None = None
        raw_extraction = RawLLMExtraction()
        llm_error: str | None = None

        try:
            raw_llm_text = self._llm.generate_text(
                prompt=prompt,
                system_instruction=EXTRACTION_SYSTEM_PROMPT,
                temperature=0.0,
                max_output_tokens=600,
            )
            parsed_json = _extract_json_from_llm_text(raw_llm_text)
            if parsed_json:
                raw_extraction = RawLLMExtraction.model_validate(parsed_json)
            else:
                logger.warning("Malformed JSON from LLM: %r", raw_llm_text)
                llm_error = "Could not parse structured extraction from model response."
        except LLMError as exc:
            logger.warning("LLM client error during extraction: %s", exc)
            llm_error = f"LLM error: {exc.message}"
        except Exception as exc:
            logger.error("Unexpected error during extraction: %s", exc)
            llm_error = f"Unexpected extraction error: {exc}"

        # 3. Apply direct clarification shortcut if matched
        if clarified_slot == "from_station" and clarified_code:
            slots.from_station = clarified_code
            active_ambiguities.pop("from_station", None)
            active_unverified.pop("from_station", None)
        elif clarified_slot == "to_station" and clarified_code:
            slots.to_station = clarified_code
            active_ambiguities.pop("to_station", None)
            active_unverified.pop("to_station", None)

        # 4. Deterministic Origin Validation
        if raw_extraction.origin_raw and clarified_slot != "from_station":
            res_orig = StationResolver.resolve(raw_extraction.origin_raw)
            if res_orig.status == StationResolutionStatus.RESOLVED and res_orig.code:
                slots.from_station = res_orig.code
                active_ambiguities.pop("from_station", None)
                active_unverified.pop("from_station", None)
            elif res_orig.status == StationResolutionStatus.AMBIGUOUS:
                slots.from_station = None
                active_ambiguities["from_station"] = res_orig.candidates
                active_unverified.pop("from_station", None)
            elif res_orig.status == StationResolutionStatus.UNVERIFIED_CODE and res_orig.code:
                db_match = get_local_timetable_store().has_station(res_orig.code)
                slots.from_station = res_orig.code
                active_ambiguities.pop("from_station", None)
                if db_match is not None:
                    active_unverified.pop("from_station", None)
                else:
                    active_unverified["from_station"] = res_orig.code
            else:
                slots.from_station = None
                active_ambiguities.pop("from_station", None)
                active_unverified.pop("from_station", None)

        # 5. Deterministic Destination Validation
        if raw_extraction.destination_raw and clarified_slot != "to_station":
            res_dest = StationResolver.resolve(raw_extraction.destination_raw)
            if res_dest.status == StationResolutionStatus.RESOLVED and res_dest.code:
                slots.to_station = res_dest.code
                active_ambiguities.pop("to_station", None)
                active_unverified.pop("to_station", None)
            elif res_dest.status == StationResolutionStatus.AMBIGUOUS:
                slots.to_station = None
                active_ambiguities["to_station"] = res_dest.candidates
                active_unverified.pop("to_station", None)
            elif res_dest.status == StationResolutionStatus.UNVERIFIED_CODE and res_dest.code:
                db_match = get_local_timetable_store().has_station(res_dest.code)
                slots.to_station = res_dest.code
                active_ambiguities.pop("to_station", None)
                if db_match is not None:
                    active_unverified.pop("to_station", None)
                else:
                    active_unverified["to_station"] = res_dest.code
            else:
                slots.to_station = None
                active_ambiguities.pop("to_station", None)
                active_unverified.pop("to_station", None)

        # 6. Deterministic Date Normalization
        date_error: str | None = None
        if raw_extraction.date_raw:
            ref_date = get_current_date_ist()
            res_date = DateNormalizer.normalize(
                raw_extraction.date_raw,
                reference_date=ref_date,
                enforce_booking_window=self._booking_window_enforce,
            )
            if res_date.status == DateResolutionStatus.RESOLVED:
                slots.journey_date = res_date.iso_date
                date_error = None
            else:
                slots.journey_date = None
                date_error = res_date.error or f"Invalid date: '{raw_extraction.date_raw}'."

        # 7. Travel Class, Quota, Ranking Preference
        if raw_extraction.travel_class:
            clean_class = raw_extraction.travel_class.upper().strip()
            if clean_class in VALID_CLASSES:
                slots.travel_class = clean_class

        if raw_extraction.quota:
            clean_quota = raw_extraction.quota.upper().strip()
            if clean_quota in VALID_QUOTAS:
                slots.quota = clean_quota

        if raw_extraction.ranking_preference:
            clean_ranking = raw_extraction.ranking_preference.upper().strip()
            if clean_ranking in VALID_RANKINGS:
                slots.ranking_preference = clean_ranking

        # 8. Check Missing Slots and Clarifications
        missing_slots: list[str] = []
        clarifications_needed: list[str] = []

        if not slots.from_station or "from_station" in active_ambiguities:
            missing_slots.append("from_station")
            if "from_station" in active_ambiguities:
                candidates_str = ", ".join(
                    f"{c.name} ({c.code})" for c in active_ambiguities["from_station"]
                )
                clarifications_needed.append(
                    f"Departure city has multiple stations: {candidates_str}. Please choose one."
                )
            else:
                clarifications_needed.append("Please specify your departure station.")
        elif "from_station" in active_unverified:
            clarifications_needed.append(
                f"Station code '{active_unverified['from_station']}' is unverified. Please confirm departure station."
            )

        if not slots.to_station or "to_station" in active_ambiguities:
            missing_slots.append("to_station")
            if "to_station" in active_ambiguities:
                candidates_str = ", ".join(
                    f"{c.name} ({c.code})" for c in active_ambiguities["to_station"]
                )
                clarifications_needed.append(
                    f"Destination city has multiple stations: {candidates_str}. Please choose one."
                )
            else:
                clarifications_needed.append("Please specify your destination station.")
        elif "to_station" in active_unverified:
            clarifications_needed.append(
                f"Station code '{active_unverified['to_station']}' is unverified. Please confirm destination station."
            )

        if not slots.journey_date:
            missing_slots.append("journey_date")
            if date_error:
                clarifications_needed.append(date_error)
            else:
                clarifications_needed.append("Please specify your travel date.")

        # Determine resolved intent
        intent = raw_extraction.intent
        if clarified_slot:
            intent = ExtractedIntentEnum.CLARIFICATION
        elif raw_extraction.is_correction:
            intent = ExtractedIntentEnum.CORRECTION

        return ExtractionResult(
            intent=intent,
            slots=slots,
            missing_slots=missing_slots,
            ambiguous_candidates=active_ambiguities,
            unverified_stations=active_unverified,
            date_error=date_error,
            clarifications_needed=clarifications_needed,
            is_correction=raw_extraction.is_correction,
            raw_llm_response=raw_llm_text,
            error=llm_error,
        )
