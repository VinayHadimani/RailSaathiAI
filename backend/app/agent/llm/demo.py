"""Deterministic Demo LLM client for offline development and hosted demos.

LLM_PROVIDER=demo replaces the Gemini call with a rule-based extraction that
mimics the JSON contract of the real extraction prompt. This exists so the
conversational agent can be exercised end-to-end with zero network access and
zero LLM quota (e.g. geo-restricted environments, CI, local demos). It is
deterministic: the same message always produces the same extraction.

The real production path remains LLM_PROVIDER=gemini.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .base import LLMClient

# Cities / stations the rule extractor recognizes as raw words. The
# deterministic StationResolver downstream does the real verification, so
# this list only needs to cover words that *could* be stations.
_KNOWN_WORDS: set[str] = {
    "mysuru", "mysore", "mandya", "bengaluru", "bangalore", "bengaluru city",
    "bangalore city", "majestic", "yesvantpur", "yeshwantpur", "yeshwanthpur",
    "tumakuru", "tumkur", "arsikere", "hassan", "shivamogga", "shimoga",
    "davangere", "harihar", "hubballi", "hubli", "gadag", "bagalkot",
    "dharwad", "belagavi", "belgaum", "madgaon", "goa", "thivim", "karmali",
    "pernem", "mangaluru", "mangalore", "mumbai", "bombay", "delhi",
    "chennai", "madras", "hyderabad", "secunderabad", "pune", "mys",
    "sbc", "ypr", "smvb", "bnc", "ask", "has", "ubl", "dwr", "bgm",
    "mao", "ndls", "mmct", "csmt", "mas", "sc",
}

_GREETING_RE = re.compile(r"^\s*(hi|hello|hey|namaste|hola|good\s*(morning|afternoon|evening))\b[!. ]*$", re.I)
_CORRECTION_WORDS = re.compile(r"\b(actually|instead|change|make it|switch to|no[,.]|rather|prefer)\b", re.I)
_FROM_TO_RE = re.compile(r"\bfrom\s+([a-z][a-z\s]{1,40}?)\s+to\s+([a-z][a-z\s]{1,40}?)(?=\s+(?:on|in|by|for|tomorrow|today|day|next|this|$)|[,.;?!]|$)", re.I)
_X_TO_Y_RE = re.compile(r"\b([a-z][a-z\s]{1,40}?)\s+to\s+([a-z][a-z\s]{1,40}?)(?=\s+(?:on|in|by|for|tomorrow|today|day|next|this|$)|[,.;?!]|$)", re.I)
_CLASS_RE = re.compile(r"\b(sleeper|sl|3a|3ac|3tier|ac\s*3|2a|2ac|ac\s*2|1a|first\s*ac|cc|chair\s*car|2s|second\s*sitting|3e|ec|ea|ev|fc)\b", re.I)
_QUOTA_RE = re.compile(r"\b(gn|tq|pt|ld|ss|hp|df|ft)\b(?=\s*(?:quota)?\s*$|\s+quota\b)", re.I)
_RANK_CHEAPEST_RE = re.compile(r"\b(cheapest|lowest\s*fare|budget|least\s*expensive|cheaper)\b", re.I)
_RANK_FASTEST_RE = re.compile(r"\b(fastest|quickest|least\s*time|shortest\s*time)\b", re.I)
_RANK_FEWEST_RE = re.compile(r"\b(fewest|fewer|least|less)\s*(changes?|transfers?|stops?)\b", re.I)

_DATE_TOMORROW_RE = re.compile(r"\b(tomorrow)\b", re.I)
_DATE_DAF_RE = re.compile(r"\b(day\s*after\s*tomorrow)\b", re.I)
_DATE_TODAY_RE = re.compile(r"\b(today|tonight)\b", re.I)
_DATE_WEEKDAY_RE = re.compile(r"\b(?:this\s+|next\s+|coming\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_DATE_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DATE_DDMM_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s+(\d{4}))?\b", re.I)

_CLASS_MAP = {
    "sleeper": "SL", "sl": "SL",
    "3a": "3A", "3ac": "3A", "3tier": "3A", "ac 3": "3A", "ac3": "3A",
    "2a": "2A", "2ac": "2A", "ac 2": "2A", "ac2": "2A",
    "1a": "1A", "first ac": "1A",
    "cc": "CC", "chair car": "CC", "chaircar": "CC",
    "2s": "2S", "second sitting": "2S",
    "3e": "3E", "ec": "EC", "ea": "EA", "ev": "EV", "fc": "FC",
}

# Tokens that look like short station codes but are travel classes / quotas.
_NON_STATION_TOKENS = {
    "SL", "3A", "2A", "1A", "CC", "2S", "3E", "EC", "EA", "EV", "FC",
    "GN", "TQ", "PT", "LD", "SS", "HP", "DF", "FT", "AC", "IT", "TO",
    "BY", "ON", "IN", "AT", "IS", "BE", "DO", "GO", "NO", "ME", "MY", "WE",
    "AN", "OR", "SO", "IF", "AS", "UP", "OF", "FOR",
}


def _clean_token(token: str) -> str:
    return re.sub(r"\s+", " ", token.strip().lower()).strip(" .,!?")


def _looks_like_station(token: str) -> bool:
    words = _clean_token(token)
    if not words:
        return False
    if words in _KNOWN_WORDS:
        return True
    # Raw 2-5 letter alphabetic station code (e.g. 'MYS', 'UBL')
    if re.fullmatch(r"[a-z]{2,5}", words):
        return True
    return False


def _extract_user_message(prompt: str) -> str:
    """Pull the raw user message out of the extractor's prompt template."""
    match = re.search(r'New User Message:\s*\n?"(.*)"\s*\n?\s*Extract parameters as JSON:', prompt, re.S)
    if match:
        return match.group(1)
    # Fallback: last quoted line
    lines = prompt.strip().splitlines()
    for line in reversed(lines):
        m = re.search(r'^"(.*)"$', line.strip())
        if m:
            return m.group(1)
    return ""


def _find_stations(message: str) -> tuple[str | None, str | None]:
    """Find (origin_raw, destination_raw) in the message."""
    # 1. Explicit "from X to Y" (or bare "X to Y") pattern.
    for pattern in (_FROM_TO_RE, _X_TO_Y_RE):
        m = pattern.search(message)
        if m:
            origin = _clean_token(m.group(1))
            dest = _clean_token(m.group(2))
            if _looks_like_station(origin) or _looks_like_station(dest):
                return origin or None, dest or None

    # 2. Preposition-scoped words: "no, from Mysore" / "switch to Dharwad".
    origin = _scan_preposition(r"\bfrom\s+", message)
    dest = _scan_preposition(r"\b(?:to|for)\s+", message)
    if origin or dest:
        return origin, dest

    # 3. Correction context with a bare station word: "make it Dharwad".
    if _CORRECTION_WORDS.search(message):
        found = _scan_station_words(message)
        if len(found) == 1:
            return None, found[0]
        if len(found) >= 2:
            return found[0], found[-1]

    # 4. Bare station reply (e.g. answering an active ambiguity): emit no raw
    #    station so the deterministic clarification shortcut resolves it.
    return None, None


def _scan_preposition(prefix_re: str, message: str) -> str | None:
    """First station-shaped word that follows a given preposition."""
    pattern = re.compile(
        prefix_re + r"([a-z][a-z\s]{0,40}?)(?=\s+(?:on|in|by|tomorrow|today|day|this|next)\b|$|[,.;?!])",
        re.I,
    )
    for m in pattern.finditer(message):
        token = _clean_token(m.group(1))
        if token and token.upper() in _NON_STATION_TOKENS:
            continue  # travel-class / quota codes are never stations here
        if _looks_like_station(token):
            return token
    return None


def _scan_station_words(message: str) -> list[str]:
    """Return known station words/codes appearing in the message, in order."""
    tokens = [t.strip(".,!?)('\"") for t in message.lower().split()]
    found: list[str] = []
    candidates: list[str] = list(tokens)
    candidates += [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    for cand in candidates:
        if cand and cand in _KNOWN_WORDS and cand not in found:
            found.append(cand)
    return found


def _find_date(message: str) -> str | None:
    for pattern in (
        _DATE_DAF_RE,
        _DATE_TOMORROW_RE,
        _DATE_TODAY_RE,
        _DATE_ISO_RE,
        _DATE_DDMM_RE,
        _DATE_WEEKDAY_RE,
    ):
        m = pattern.search(message)
        if m:
            return m.group(1)
    return None


def _find_class(message: str) -> str | None:
    m = _CLASS_RE.search(message)
    if not m:
        return None
    raw = re.sub(r"\s+", " ", m.group(1).strip().lower())
    return _CLASS_MAP.get(raw, _CLASS_MAP.get(raw.replace(" ", ""), None))


def _find_ranking(message: str) -> str | None:
    if _RANK_CHEAPEST_RE.search(message):
        return "CHEAPEST"
    if _RANK_FASTEST_RE.search(message):
        return "FASTEST"
    if _RANK_FEWEST_RE.search(message):
        return "FEWEST_TRANSFERS"
    return None


class DemoLLMClient(LLMClient):
    """Deterministic, offline rule-based extraction client (LLM_PROVIDER=demo)."""

    def __init__(self, model_name: str = "demo-rules-v1") -> None:
        self._model = model_name
        self._history: list[dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "demo"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def history(self) -> list[dict[str, Any]]:
        return self._history

    def _extract(self, message: str) -> dict[str, Any]:
        msg = message.strip()
        origin, dest = _find_stations(msg)
        date_raw = _find_date(msg)
        travel_class = _find_class(msg)
        ranking = _find_ranking(msg)

        is_greeting = bool(_GREETING_RE.match(msg)) and not origin and not dest and not date_raw
        correction_word = bool(_CORRECTION_WORDS.search(msg))

        # Correction heuristics: explicitly changing an already-stated parameter.
        correction_target = None
        if correction_word or (not origin and not dest and not date_raw):
            if travel_class:
                correction_target = "class"
            elif ranking:
                correction_target = "ranking_preference"
            elif correction_word and dest and not origin:
                correction_target = "destination"
            elif correction_word and origin and not dest:
                correction_target = "origin"
            elif correction_word and date_raw:
                correction_target = "date"

        if is_greeting:
            intent = "GREETING"
        elif correction_target:
            intent = "CORRECTION"
        elif (origin or dest or date_raw or travel_class or ranking) and not _CORRECTION_WORDS.search(msg):
            intent = "JOURNEY_SEARCH"
        else:
            intent = "OTHER"

        return {
            "intent": intent,
            "origin_raw": origin,
            "destination_raw": dest,
            "date_raw": date_raw,
            "travel_class": travel_class,
            "quota": None,
            "ranking_preference": ranking,
            "is_correction": intent == "CORRECTION",
            "correction_target": correction_target,
        }

    def _resolve_response(self, prompt: str) -> str:
        user_message = _extract_user_message(prompt)
        extraction = self._extract(user_message)
        self._history.append({"prompt_tail": prompt[-400:], "user_message": user_message, "extraction": extraction})
        return json.dumps(extraction)

    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        return self._resolve_response(prompt)

    async def generate_text_async(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        await asyncio.sleep(0)
        return self._resolve_response(prompt)
