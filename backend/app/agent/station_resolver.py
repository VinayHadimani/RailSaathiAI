"""Conservative railway station resolver with ambiguity detection.

Maps user-provided station queries (codes, city names, and aliases) to
standard Indian Railways station codes. Never silently resolves ambiguous
multi-station cities.
"""

from __future__ import annotations

import re
from typing import Final

from .schemas import (
    StationCandidate,
    StationResolutionResult,
    StationResolutionStatus,
)

# IR station codes are 2 to 5 alphabetic characters.
STATION_CODE_REGEX: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]{2,5}$")

# Maintained catalog of common stations in the RailSaathi network and major gateways.
KNOWN_STATIONS: Final[dict[str, StationCandidate]] = {
    "MYS": StationCandidate(code="MYS", name="Mysuru Junction", city="Mysuru", state="Karnataka"),
    "MYA": StationCandidate(code="MYA", name="Mandya", city="Mandya", state="Karnataka"),
    "SBC": StationCandidate(code="SBC", name="KSR Bengaluru City (Majestic)", city="Bengaluru", state="Karnataka"),
    "YPR": StationCandidate(code="YPR", name="Yesvantpur Junction", city="Bengaluru", state="Karnataka"),
    "SMVB": StationCandidate(code="SMVB", name="Sir M. Visvesvaraya Terminal Bengaluru", city="Bengaluru", state="Karnataka"),
    "BNC": StationCandidate(code="BNC", name="Bengaluru Cantt", city="Bengaluru", state="Karnataka"),
    "TK": StationCandidate(code="TK", name="Tumakuru", city="Tumakuru", state="Karnataka"),
    "ASK": StationCandidate(code="ASK", name="Arsikere Junction", city="Arsikere", state="Karnataka"),
    "HAS": StationCandidate(code="HAS", name="Hassan Junction", city="Hassan", state="Karnataka"),
    "SMET": StationCandidate(code="SMET", name="Shivamogga Town", city="Shivamogga", state="Karnataka"),
    "SAG": StationCandidate(code="SAG", name="Sagara Jambagaru", city="Sagara", state="Karnataka"),
    "DVG": StationCandidate(code="DVG", name="Davangere", city="Davangere", state="Karnataka"),
    "HRR": StationCandidate(code="HRR", name="Harihar", city="Harihar", state="Karnataka"),
    "UBL": StationCandidate(code="UBL", name="SSS Hubballi Junction", city="Hubballi", state="Karnataka"),
    "GDG": StationCandidate(code="GDG", name="Gadag Junction", city="Gadag", state="Karnataka"),
    "BGK": StationCandidate(code="BGK", name="Bagalkot", city="Bagalkot", state="Karnataka"),
    "DWR": StationCandidate(code="DWR", name="Dharwad", city="Dharwad", state="Karnataka"),
    "BGM": StationCandidate(code="BGM", name="Belagavi", city="Belagavi", state="Karnataka"),
    "MAO": StationCandidate(code="MAO", name="Madgaon Junction", city="Madgaon", state="Goa"),
    "THVM": StationCandidate(code="THVM", name="Thivim", city="Thivim", state="Goa"),
    "KRMI": StationCandidate(code="KRMI", name="Karmali", city="Karmali", state="Goa"),
    "PVR": StationCandidate(code="PVR", name="Pernem", city="Pernem", state="Goa"),
    "MAQ": StationCandidate(code="MAQ", name="Mangaluru Central", city="Mangaluru", state="Karnataka"),
    "MAJN": StationCandidate(code="MAJN", name="Mangaluru Junction", city="Mangaluru", state="Karnataka"),
    "PUNE": StationCandidate(code="PUNE", name="Pune Junction", city="Pune", state="Maharashtra"),
    "CSMT": StationCandidate(code="CSMT", name="Chhatrapati Shivaji Maharaj Terminus", city="Mumbai", state="Maharashtra"),
    "MMCT": StationCandidate(code="MMCT", name="Mumbai Central", city="Mumbai", state="Maharashtra"),
    "LTT": StationCandidate(code="LTT", name="Lokmanya Tilak Terminus", city="Mumbai", state="Maharashtra"),
    "BDTS": StationCandidate(code="BDTS", name="Bandra Terminus", city="Mumbai", state="Maharashtra"),
    "NDLS": StationCandidate(code="NDLS", name="New Delhi", city="Delhi", state="Delhi"),
    "DLI": StationCandidate(code="DLI", name="Old Delhi Junction", city="Delhi", state="Delhi"),
    "NZM": StationCandidate(code="NZM", name="Hazrat Nizamuddin", city="Delhi", state="Delhi"),
    "MAS": StationCandidate(code="MAS", name="MGR Chennai Central", city="Chennai", state="Tamil Nadu"),
    "MS": StationCandidate(code="MS", name="Chennai Egmore", city="Chennai", state="Tamil Nadu"),
    "HYB": StationCandidate(code="HYB", name="Hyderabad Deccan", city="Hyderabad", state="Telangana"),
    "SC": StationCandidate(code="SC", name="Secunderabad Junction", city="Secunderabad", state="Telangana"),
    "HWH": StationCandidate(code="HWH", name="Howrah Junction", city="Kolkata", state="West Bengal"),
    "SDAH": StationCandidate(code="SDAH", name="Sealdah", city="Kolkata", state="West Bengal"),
    "ADI": StationCandidate(code="ADI", name="Ahmedabad Junction", city="Ahmedabad", state="Gujarat"),
    "JP": StationCandidate(code="JP", name="Jaipur Junction", city="Jaipur", state="Rajasthan"),
    "BPL": StationCandidate(code="BPL", name="Bhopal Junction", city="Bhopal", state="Madhya Pradesh"),
    "CNB": StationCandidate(code="CNB", name="Kanpur Central", city="Kanpur", state="Uttar Pradesh"),
    "LKO": StationCandidate(code="LKO", name="Lucknow Charbagh", city="Lucknow", state="Uttar Pradesh"),
    "PNBE": StationCandidate(code="PNBE", name="Patna Junction", city="Patna", state="Bihar"),
    "NGP": StationCandidate(code="NGP", name="Nagpur Junction", city="Nagpur", state="Maharashtra"),
    "ERS": StationCandidate(code="ERS", name="Ernakulam Junction", city="Kochi", state="Kerala"),
    "TVC": StationCandidate(code="TVC", name="Thiruvananthapuram Central", city="Thiruvananthapuram", state="Kerala"),
    "CBE": StationCandidate(code="CBE", name="Coimbatore Junction", city="Coimbatore", state="Tamil Nadu"),
    "VSKP": StationCandidate(code="VSKP", name="Visakhapatnam Junction", city="Visakhapatnam", state="Andhra Pradesh"),
}

# Ambiguous city clusters: multiple plausible stations exist.
# Never resolve these automatically; return all candidates.
AMBIGUOUS_CLUSTERS: Final[dict[str, list[str]]] = {
    "bengaluru": ["SBC", "YPR", "SMVB", "BNC"],
    "bangalore": ["SBC", "YPR", "SMVB", "BNC"],
    "goa": ["MAO", "THVM", "KRMI", "PVR"],
    "mangaluru": ["MAQ", "MAJN"],
    "mangalore": ["MAQ", "MAJN"],
    "mumbai": ["CSMT", "MMCT", "LTT", "BDTS"],
    "bombay": ["CSMT", "MMCT", "LTT", "BDTS"],
    "delhi": ["NDLS", "DLI", "NZM"],
    "chennai": ["MAS", "MS"],
    "madras": ["MAS", "MS"],
    "hyderabad": ["HYB", "SC"],
    "kolkata": ["HWH", "SDAH"],
    "calcutta": ["HWH", "SDAH"],
}

# Unambiguous specific aliases and single-station towns.
STATION_ALIASES: Final[dict[str, str]] = {
    # Specific stations within multi-station clusters:
    "ksr bengaluru": "SBC",
    "ksr bangalore": "SBC",
    "majestic": "SBC",
    "bengaluru city": "SBC",
    "bangalore city": "SBC",
    "yesvantpur": "YPR",
    "yesvantpur jn": "YPR",
    "yeshwantpur": "YPR",
    "yeshwanthpur": "YPR",
    "smvb": "SMVB",
    "sir m visvesvaraya": "SMVB",
    "sir m visvesvaraya terminal": "SMVB",
    "bengaluru cantt": "BNC",
    "bangalore cantt": "BNC",
    "madgaon": "MAO",
    "madgaon jn": "MAO",
    "thivim": "THVM",
    "karmali": "KRMI",
    "pernem": "PVR",
    "mangaluru central": "MAQ",
    "mangalore central": "MAQ",
    "mangaluru jn": "MAJN",
    "mangalore jn": "MAJN",
    "new delhi": "NDLS",
    "old delhi": "DLI",
    "hazrat nizamuddin": "NZM",
    "nizamuddin": "NZM",
    "chennai central": "MAS",
    "chennai egmore": "MS",
    "secunderabad": "SC",
    "secunderabad jn": "SC",
    "hyderabad deccan": "HYB",
    "nampally": "HYB",
    "csmt": "CSMT",
    "mumbai csmt": "CSMT",
    "chhatrapati shivaji maharaj terminus": "CSMT",
    "chhatrapati shivaji terminus": "CSMT",
    "cst": "CSMT",
    "vt": "CSMT",
    "mgr chennai central": "MAS",
    "kolkata howrah": "HWH",
    "ksr bengaluru city": "SBC",
    "victoria terminus": "CSMT",
    "mumbai central": "MMCT",
    "bandra terminus": "BDTS",
    "lokmanya tilak": "LTT",

    # Unambiguous towns / single-station corridors:
    "mysuru": "MYS",
    "mysore": "MYS",
    "mysuru jn": "MYS",
    "mysore jn": "MYS",
    "mandya": "MYA",
    "tumakuru": "TK",
    "tumkur": "TK",
    "arsikere": "ASK",
    "arsikere jn": "ASK",
    "hassan": "HAS",
    "hassan jn": "HAS",
    "shivamogga": "SMET",
    "shimoga": "SMET",
    "shivamogga town": "SMET",
    "sagara": "SAG",
    "sagara jambagaru": "SAG",
    "davangere": "DVG",
    "davangere jn": "DVG",
    "harihar": "HRR",
    "hubballi": "UBL",
    "hubli": "UBL",
    "hubballi jn": "UBL",
    "hubli jn": "UBL",
    "gadag": "GDG",
    "gadag jn": "GDG",
    "bagalkot": "BGK",
    "dharwad": "DWR",
    "belagavi": "BGM",
    "belgaum": "BGM",
    "pune": "PUNE",
    "pune jn": "PUNE",
    "howrah": "HWH",
    "sealdah": "SDAH",
    "ahmedabad": "ADI",
    "jaipur": "JP",
    "bhopal": "BPL",
    "kanpur": "CNB",
    "lucknow": "LKO",
    "patna": "PNBE",
    "nagpur": "NGP",
    "kochi": "ERS",
    "ernakulam": "ERS",
    "trivandrum": "TVC",
    "thiruvananthapuram": "TVC",
    "coimbatore": "CBE",
    "visakhapatnam": "VSKP",
    "vizag": "VSKP",
}


def _clean_input(raw: str) -> str:
    """Normalize input string for matching."""
    s = raw.strip().lower()
    # Strip common conversational prefixes/suffixes
    s = re.sub(r"^(to|from|at|near)\s+", "", s)
    s = re.sub(r"\s+railway\s+station$", "", s)
    s = re.sub(r"\s+station$", "", s)
    # Remove extraneous punctuation
    s = re.sub(r"[\.,\-_\(\)]", " ", s)
    return " ".join(s.split())


class StationResolver:
    """Resolves station queries deterministically with ambiguity protection."""

    @classmethod
    def resolve(cls, query: str | None) -> StationResolutionResult:
        """Resolve a raw station query.

        Args:
            query: Raw user query (e.g. 'MYS', 'Bangalore', 'Majestic', 'DWR').

        Returns:
            StationResolutionResult with status RESOLVED, AMBIGUOUS, UNKNOWN, or INVALID_CODE.
        """
        if not query or not query.strip():
            return StationResolutionResult(
                status=StationResolutionStatus.UNKNOWN,
                error="Empty station query.",
            )

        raw = query.strip()
        cleaned = _clean_input(raw)

        # 1. Invalid characters check (must not contain numbers or excessive length)
        if any(c.isdigit() for c in raw) or len(raw) > 50:
            return StationResolutionResult(
                status=StationResolutionStatus.INVALID_CODE,
                error=f"Invalid station name or code: '{raw}'.",
            )

        # 2. Check if raw input is an exact known station code (case-insensitive)
        upper_code = raw.upper()
        if upper_code in KNOWN_STATIONS:
            stn = KNOWN_STATIONS[upper_code]
            return StationResolutionResult(
                status=StationResolutionStatus.RESOLVED,
                code=stn.code,
                name=stn.name,
                city=stn.city,
            )

        # 3. Check exact match in ambiguous clusters (NEVER guess silently)
        if cleaned in AMBIGUOUS_CLUSTERS:
            codes = AMBIGUOUS_CLUSTERS[cleaned]
            candidates = [KNOWN_STATIONS[c] for c in codes if c in KNOWN_STATIONS]
            return StationResolutionResult(
                status=StationResolutionStatus.AMBIGUOUS,
                candidates=candidates,
                error=(
                    f"'{query.strip()}' has multiple passenger stations. "
                    f"Please choose from: {', '.join(c.code for c in candidates)}."
                ),
            )

        # 4. Check unambiguous station aliases
        if cleaned in STATION_ALIASES:
            code = STATION_ALIASES[cleaned]
            stn = KNOWN_STATIONS.get(code)
            if stn:
                return StationResolutionResult(
                    status=StationResolutionStatus.RESOLVED,
                    code=stn.code,
                    name=stn.name,
                    city=stn.city,
                )

        # 5. Exact match on full station names or city names in catalog
        matches: list[StationCandidate] = []
        for stn in KNOWN_STATIONS.values():
            if cleaned == stn.name.lower() or cleaned == stn.city.lower():
                matches.append(stn)

        # Deduplicate matches by station code
        unique_matches = {m.code: m for m in matches}
        match_list = list(unique_matches.values())

        if len(match_list) == 1:
            stn = match_list[0]
            return StationResolutionResult(
                status=StationResolutionStatus.RESOLVED,
                code=stn.code,
                name=stn.name,
                city=stn.city,
            )
        elif len(match_list) > 1:
            return StationResolutionResult(
                status=StationResolutionStatus.AMBIGUOUS,
                candidates=match_list,
                error=f"Multiple stations match '{raw}'. Please specify the exact station code.",
            )

        # 6. Unverified code-shaped fallback (2-5 alphabetic characters not in catalog)
        # Preserves the exact uppercase code; marked UNVERIFIED_CODE so it cannot
        # be mistaken for a verified catalog station.
        if STATION_CODE_REGEX.match(raw) and len(raw) <= 5:
            return StationResolutionResult(
                status=StationResolutionStatus.UNVERIFIED_CODE,
                code=upper_code,
                name=None,
                city=None,
                error=(
                    f"'{upper_code}' has a valid station code format (2-5 letters) "
                    "but is not in the verified local catalog. It must be verified "
                    "against schedule and availability providers."
                ),
            )

        # 7. Unknown station
        return StationResolutionResult(
            status=StationResolutionStatus.UNKNOWN,
            error=f"Could not recognize railway station: '{raw}'.",
        )
