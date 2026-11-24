"""Unit tests for IntentAndSlotExtractor (Phase 6B.4).

Verifies intent classification, slot extraction, deterministic station/date
validation, slot preservation, explicit corrections, ambiguities, unverified
codes, malformed LLM responses, and provider failure handling.
All tests use MockLLMClient; ZERO live API calls are made.
"""

from __future__ import annotations

from datetime import timedelta
import json
import pytest

from app.agent.date_normalizer import get_current_date_ist
from app.agent.extractor import IntentAndSlotExtractor
from app.agent.llm import LLMTimeoutError, MockLLMClient
from app.agent.schemas import (
    AgentSlots,
    ExtractedIntentEnum,
    SessionMessage,
    StationCandidate,
)


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


def test_extractor_complete_journey_search():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="Mysore",
            destination_raw="Dharwad",
            date_raw="tomorrow",
            travel_class="3A",
            quota="GN",
            ranking_preference="FASTEST",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Find trains from Mysore to Dharwad tomorrow in 3A")

    assert res.intent == ExtractedIntentEnum.JOURNEY_SEARCH
    assert res.slots.from_station == "MYS"
    assert res.slots.to_station == "DWR"
    tomorrow_iso = (get_current_date_ist() + timedelta(days=1)).isoformat()
    assert res.slots.journey_date == tomorrow_iso
    assert res.slots.travel_class == "3A"
    assert res.slots.quota == "GN"
    assert res.slots.ranking_preference == "FASTEST"
    assert res.missing_slots == []
    assert res.clarifications_needed == []


def test_extractor_slot_preservation_across_turns():
    # Previous turn had origin and destination, but date was missing
    existing_slots = AgentSlots(
        from_station="SBC",
        to_station="MYS",
        journey_date=None,
    )

    # In this turn, user only specifies the date
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw=None,
            destination_raw=None,
            date_raw="day after tomorrow",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract(
        message="Day after tomorrow please",
        current_slots=existing_slots,
    )

    # Origin and destination MUST be preserved
    assert res.slots.from_station == "SBC"
    assert res.slots.to_station == "MYS"
    day_after_iso = (get_current_date_ist() + timedelta(days=2)).isoformat()
    assert res.slots.journey_date == day_after_iso
    assert res.missing_slots == []


def test_extractor_explicit_destination_correction():
    # User originally asked for Mysore, now changes to Belagavi
    existing_slots = AgentSlots(
        from_station="SBC",
        to_station="MYS",
        journey_date="2026-12-05",
    )

    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="CORRECTION",
            origin_raw=None,
            destination_raw="Belagavi",
            date_raw=None,
            is_correction=True,
            correction_target="destination",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract(
        message="Actually make the destination Belagavi",
        current_slots=existing_slots,
    )

    assert res.intent == ExtractedIntentEnum.CORRECTION
    assert res.is_correction is True
    assert res.slots.from_station == "SBC"       # Preserved
    assert res.slots.to_station == "BGM"         # Corrected
    assert res.slots.journey_date == "2026-12-05" # Preserved


def test_extractor_missing_destination_detected():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="NDLS",
            destination_raw=None,
            date_raw="tomorrow",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Trains leaving from New Delhi tomorrow")

    assert res.slots.from_station == "NDLS"
    assert res.slots.to_station is None
    assert "to_station" in res.missing_slots
    assert any("destination" in c.lower() for c in res.clarifications_needed)


def test_extractor_missing_origin_detected():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw=None,
            destination_raw="Goa",
            date_raw="tomorrow",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Find trains to Goa tomorrow")

    assert res.slots.from_station is None
    assert "from_station" in res.missing_slots
    assert any("departure" in c.lower() for c in res.clarifications_needed)


def test_extractor_missing_date_detected():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="MYS",
            destination_raw="SBC",
            date_raw=None,
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Find trains from Mysore to SBC")

    assert res.slots.from_station == "MYS"
    assert res.slots.to_station == "SBC"
    assert res.slots.journey_date is None
    assert "journey_date" in res.missing_slots
    assert any("travel date" in c.lower() for c in res.clarifications_needed)


def test_extractor_ambiguous_station_handling():
    # User says "Bangalore" which is ambiguous (multiple stations: SBC, YPR, SMVB, BNC)
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="Bangalore",
            destination_raw="Dharwad",
            date_raw="tomorrow",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Trains from Bangalore to Dharwad tomorrow")

    # from_station must NOT be assumed/guessed!
    assert res.slots.from_station is None
    assert "from_station" in res.missing_slots
    assert "from_station" in res.ambiguous_candidates
    candidates = res.ambiguous_candidates["from_station"]
    candidate_codes = [c.code for c in candidates]
    assert "SBC" in candidate_codes
    assert "YPR" in candidate_codes
    assert any("multiple stations" in c.lower() for c in res.clarifications_needed)


def test_extractor_ambiguity_resolution_shortcut():
    # Previous turn left from_station ambiguous for Bengaluru
    ambiguities = {
        "from_station": [
            StationCandidate(code="SBC", name="KSR Bengaluru City (Majestic)", city="Bengaluru"),
            StationCandidate(code="YPR", name="Yesvantpur Junction", city="Bengaluru"),
        ]
    }
    existing_slots = AgentSlots(
        from_station=None,
        to_station="DWR",
        journey_date="2026-11-28",
    )

    # User answers "Majestic"
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="CLARIFICATION",
            origin_raw="Majestic",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract(
        message="Majestic",
        current_slots=existing_slots,
        ambiguous_candidates=ambiguities,
    )

    # Ambiguity resolved to SBC!
    assert res.slots.from_station == "SBC"
    assert res.slots.to_station == "DWR"
    assert res.slots.journey_date == "2026-11-28"
    assert "from_station" not in res.ambiguous_candidates
    assert res.missing_slots == []


def test_extractor_unverified_station_code_handling():
    # User provides non-catalog code 'XYZ'
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="XYZ",
            destination_raw="MYS",
            date_raw="tomorrow",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Find trains from XYZ to Mysore tomorrow")

    # Preserved as unverified, NOT marked as fully verified station!
    assert res.slots.from_station == "XYZ"
    assert "from_station" in res.unverified_stations
    assert res.unverified_stations["from_station"] == "XYZ"
    assert any("unverified" in c.lower() for c in res.clarifications_needed)


def test_extractor_relative_date_handling():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="MYS",
            destination_raw="SBC",
            date_raw="today",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Trains from Mysore to Bangalore today")
    today_iso = get_current_date_ist().isoformat()
    assert res.slots.journey_date == today_iso


def test_extractor_past_date_rejected():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="MYS",
            destination_raw="SBC",
            date_raw="yesterday",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Trains from Mysore to Bangalore yesterday")

    assert res.slots.journey_date is None
    assert "journey_date" in res.missing_slots
    assert "past" in res.date_error.lower()


def test_extractor_preference_and_quota_extraction():
    mock_llm = MockLLMClient(
        default_response=_make_llm_json(
            intent="JOURNEY_SEARCH",
            origin_raw="MYS",
            destination_raw="SBC",
            date_raw="tomorrow",
            travel_class="2S",
            quota="TQ",
            ranking_preference="CHEAPEST",
        )
    )
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Cheapest 2S Tatkal trains from Mysore to SBC tomorrow")

    assert res.slots.travel_class == "2S"
    assert res.slots.quota == "TQ"
    assert res.slots.ranking_preference == "CHEAPEST"


def test_extractor_malformed_llm_json_handled_gracefully():
    # LLM returns markdown fences + explanatory text + partial JSON
    malformed_response = """Here is your JSON:
```json
{
  "intent": "JOURNEY_SEARCH",
  "origin_raw": "MYS",
  "destination_raw": "DWR",
  "date_raw": "tomorrow"
}
```
Hope this helps!"""

    mock_llm = MockLLMClient(default_response=malformed_response)
    extractor = IntentAndSlotExtractor(mock_llm)

    res = extractor.extract("Mysore to Dharwad tomorrow")

    assert res.slots.from_station == "MYS"
    assert res.slots.to_station == "DWR"
    assert res.error is None


def test_extractor_completely_invalid_llm_response_handled_gracefully():
    # LLM returns totally non-JSON gibberish
    mock_llm = MockLLMClient(default_response="I am an AI and cannot help with this.")
    extractor = IntentAndSlotExtractor(mock_llm)

    existing_slots = AgentSlots(from_station="SBC", to_station="MYS")
    res = extractor.extract("Find trains", current_slots=existing_slots)

    # Does not crash, preserves existing slots, records error
    assert res.slots.from_station == "SBC"
    assert res.slots.to_station == "MYS"
    assert res.error is not None
    assert "could not parse" in res.error.lower()


def test_extractor_llm_provider_failure_handled_gracefully():
    mock_llm = MockLLMClient(error_to_raise=LLMTimeoutError("Gemini call timed out"))
    extractor = IntentAndSlotExtractor(mock_llm)

    existing_slots = AgentSlots(from_station="SBC", to_station="MYS")
    res = extractor.extract("Find trains", current_slots=existing_slots)

    # Graceful return with error recorded, existing slots preserved
    assert res.slots.from_station == "SBC"
    assert res.slots.to_station == "MYS"
    assert res.error is not None
    assert "timed out" in res.error.lower()
