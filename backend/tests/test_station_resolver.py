"""Unit tests for StationResolver (Phase 6B.2)."""

import pytest

from app.agent.schemas import StationResolutionStatus
from app.agent.station_resolver import KNOWN_STATIONS, StationResolver


def test_station_code_recognition():
    """Exact station codes in catalog are resolved."""
    for code in ["MYS", "SBC", "DWR", "BGM", "UBL", "ASK", "TK"]:
        res = StationResolver.resolve(code)
        assert res.status == StationResolutionStatus.RESOLVED
        assert res.code == code
        assert res.city is not None

    # Lowercase code is also accepted
    res_lower = StationResolver.resolve("mys")
    assert res_lower.status == StationResolutionStatus.RESOLVED
    assert res_lower.code == "MYS"


def test_station_aliases():
    """Unambiguous town names and aliases resolve to standard codes."""
    cases = [
        ("Mysuru", "MYS"),
        ("Mysore", "MYS"),
        ("Mysuru Jn", "MYS"),
        ("Dharwad", "DWR"),
        ("Belagavi", "BGM"),
        ("Belgaum", "BGM"),
        ("Hubli", "UBL"),
        ("Hubballi Jn", "UBL"),
        ("Arsikere", "ASK"),
        ("Tumkur", "TK"),
        ("Mandya", "MYA"),
        ("Davangere", "DVG"),
        ("Pune", "PUNE"),
    ]
    for raw, expected_code in cases:
        res = StationResolver.resolve(raw)
        assert res.status == StationResolutionStatus.RESOLVED, f"Failed for {raw}"
        assert res.code == expected_code


def test_specific_station_in_cluster():
    """Specifically named stations within multi-station clusters resolve unambiguously."""
    cases = [
        ("Majestic", "SBC"),
        ("KSR Bengaluru", "SBC"),
        ("Bangalore City", "SBC"),
        ("Yesvantpur", "YPR"),
        ("Yeshwantpur", "YPR"),
        ("SMVB", "SMVB"),
        ("Sir M Visvesvaraya", "SMVB"),
        ("Bengaluru Cantt", "BNC"),
        ("Madgaon", "MAO"),
        ("Thivim", "THVM"),
        ("Mangaluru Central", "MAQ"),
        ("Mangalore Central", "MAQ"),
        ("Mangaluru Jn", "MAJN"),
        ("CSMT", "CSMT"),
        ("New Delhi", "NDLS"),
    ]
    for raw, expected_code in cases:
        res = StationResolver.resolve(raw)
        assert res.status == StationResolutionStatus.RESOLVED, f"Failed for {raw}"
        assert res.code == expected_code


def test_ambiguous_city_names():
    """Ambiguous multi-station cities return AMBIGUOUS with candidate lists (never guess)."""
    ambiguous_inputs = ["Bengaluru", "Bangalore", "Goa", "Mangaluru", "Mangalore", "Mumbai", "Delhi"]
    for city in ambiguous_inputs:
        res = StationResolver.resolve(city)
        assert res.status == StationResolutionStatus.AMBIGUOUS, f"Expected ambiguous for {city}"
        assert len(res.candidates) >= 2, f"Expected >=2 candidates for {city}"
        assert res.code is None
        assert res.error is not None
        # Verify candidate codes belong to catalog
        for cand in res.candidates:
            assert cand.code in KNOWN_STATIONS


def test_unverified_code_shaped_inputs():
    """Code-shaped 2-5 letter inputs not in KNOWN_STATIONS return UNVERIFIED_CODE.

    Ensures that unknown 2-5 letter strings are never silently treated as verified
    stations, but preserves the normalized uppercase code for potential verification.
    """
    code_shaped = ["SUR", "GTL", "BZA", "KOTA", "sur", "xyz", "abcd"]
    for raw in code_shaped:
        res = StationResolver.resolve(raw)
        assert res.status == StationResolutionStatus.UNVERIFIED_CODE
        assert res.status != StationResolutionStatus.RESOLVED, f"Must not be RESOLVED: {raw}"
        assert res.code == raw.upper(), f"Code must be preserved in uppercase: {raw}"
        assert res.name is None
        assert res.city is None
        assert res.error is not None


def test_unknown_station_names():
    """Unrecognized multi-word or long non-code names return UNKNOWN."""
    unknowns = ["Atlantis", "Hogwarts", "Gotham City", "Wakanda", "SomewhereRandom"]
    for raw in unknowns:
        res = StationResolver.resolve(raw)
        assert res.status == StationResolutionStatus.UNKNOWN
        assert res.code is None
        assert res.error is not None


def test_invalid_station_inputs():
    """Inputs with digits, special characters, or extreme lengths return INVALID_CODE or UNKNOWN."""
    invalids = ["123", "Station123", "SBC1", "abc@123", "a" * 60]
    for raw in invalids:
        res = StationResolver.resolve(raw)
        assert res.status in (StationResolutionStatus.INVALID_CODE, StationResolutionStatus.UNKNOWN)
        assert res.code is None

    # Empty string or whitespace or None
    for empty in ["", "   ", None]:
        empty_res = StationResolver.resolve(empty)
        assert empty_res.status == StationResolutionStatus.UNKNOWN


def test_pan_india_station_resolution():
    """Pan-India cities & spelling variants resolve via aliases and trains.db."""
    cases = [
        ("kalburgi", "KLBG"),
        ("kalaburgi", "KLBG"),
        ("kalaburagi", "KLBG"),
        ("gulbarga", "KLBG"),
        ("solapur", "SUR"),
        ("vijayawada", "BZA"),
        ("surat", "ST"),
        ("tirupati", "TPTY"),
        ("madurai", "MDU"),
        ("ahemdabad", "ADI"),
        ("vishakapatnam", "VSKP"),
        ("bhubaneshwar", "BBS"),
    ]
    for raw, expected_code in cases:
        res = StationResolver.resolve(raw)
        assert res.status == StationResolutionStatus.RESOLVED, f"Failed for {raw}: {res}"
        assert res.code == expected_code

    for typo_city in ("hydrabad", "banglore", "chenai", "kolkatta"):
        res = StationResolver.resolve(typo_city)
        assert res.status == StationResolutionStatus.AMBIGUOUS, f"Expected AMBIGUOUS for {typo_city}: {res}"
