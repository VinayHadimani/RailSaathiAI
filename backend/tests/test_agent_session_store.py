"""Unit tests for InMemorySessionStore (Phase 6B.4).

Verifies session creation, retrieval, slot preservation, explicit corrections,
TTL expiration, history limits, and thread safety.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time
import uuid

import pytest

from app.agent.schemas import AgentSlots, StationCandidate
from app.agent.session_store import InMemorySessionStore


def test_session_store_create_new_session():
    store = InMemorySessionStore(ttl_seconds=300.0)
    session = store.create_session()

    assert session.session_id is not None
    # Valid UUID format
    assert uuid.UUID(session.session_id)
    assert session.slots.from_station is None
    assert session.slots.to_station is None
    assert session.slots.travel_class == "SL"
    assert session.history == []
    assert store.session_count() == 1


def test_session_store_create_with_client_uuid():
    store = InMemorySessionStore()
    client_id = "c1f7a012-706d-495d-9860-9dfc12658a2e"
    session = store.create_session(client_id)

    assert session.session_id == client_id
    retrieved = store.get_session(client_id)
    assert retrieved is not None
    assert retrieved.session_id == client_id


def test_session_store_get_or_create():
    store = InMemorySessionStore()
    client_id = "a1b2c3d4-e5f6-4a1b-8c2d-1234567890ab"

    # First call creates
    session1, is_new1 = store.get_or_create_session(client_id)
    assert is_new1 is True
    assert session1.session_id == client_id

    # Second call retrieves existing
    session2, is_new2 = store.get_or_create_session(client_id)
    assert is_new2 is False
    assert session2.session_id == client_id


def test_session_store_update_individual_slots_preserves_others():
    store = InMemorySessionStore()
    session = store.create_session()
    sid = session.session_id

    # Turn 1: Set origin and destination
    store.update_slots(sid, {"from_station": "SBC", "to_station": "MYS"})
    s1 = store.get_session(sid)
    assert s1.slots.from_station == "SBC"
    assert s1.slots.to_station == "MYS"
    assert s1.slots.journey_date is None

    # Turn 2: Set date only -> origin and destination MUST be preserved
    store.update_slots(sid, {"journey_date": "2026-12-01"})
    s2 = store.get_session(sid)
    assert s2.slots.from_station == "SBC"
    assert s2.slots.to_station == "MYS"
    assert s2.slots.journey_date == "2026-12-01"
    assert s2.slots.travel_class == "SL"


def test_session_store_explicit_correction():
    store = InMemorySessionStore()
    session = store.create_session()
    sid = session.session_id

    store.update_slots(sid, {"from_station": "SBC", "to_station": "MYS", "journey_date": "2026-12-01"})

    # User corrects destination to Dharwad
    store.update_slots(sid, {"to_station": "DWR"})
    s_corrected = store.get_session(sid)
    assert s_corrected.slots.from_station == "SBC"  # Preserved
    assert s_corrected.slots.to_station == "DWR"    # Corrected
    assert s_corrected.slots.journey_date == "2026-12-01"  # Preserved


def test_session_store_ambiguous_and_unverified_candidates():
    store = InMemorySessionStore()
    session = store.create_session()
    sid = session.session_id

    candidates = [
        StationCandidate(code="SBC", name="Majestic", city="Bengaluru"),
        StationCandidate(code="YPR", name="Yesvantpur", city="Bengaluru"),
    ]
    store.update_ambiguous_candidates(sid, {"from_station": candidates})
    store.update_unverified_stations(sid, {"to_station": "XYZ"})

    s = store.get_session(sid)
    assert "from_station" in s.ambiguous_candidates
    assert len(s.ambiguous_candidates["from_station"]) == 2
    assert s.unverified_stations["to_station"] == "XYZ"


def test_session_store_history_bounds():
    store = InMemorySessionStore(max_history_messages=4)
    session = store.create_session()
    sid = session.session_id

    store.add_message(sid, "user", "Message 1")
    store.add_message(sid, "assistant", "Reply 1")
    store.add_message(sid, "user", "Message 2")
    store.add_message(sid, "assistant", "Reply 2")
    store.add_message(sid, "user", "Message 3")  # Exceeds max 4, oldest should drop

    s = store.get_session(sid)
    assert len(s.history) == 4
    assert s.history[0].content == "Reply 1"
    assert s.history[3].content == "Message 3"


def test_session_store_ttl_expiration():
    store = InMemorySessionStore(ttl_seconds=0.1)  # 100ms TTL
    session = store.create_session()
    sid = session.session_id

    assert store.get_session(sid) is not None
    time.sleep(0.15)  # Allow session to expire

    assert store.get_session(sid) is None
    assert store.session_count() == 0


def test_session_store_manual_purge_expired():
    store = InMemorySessionStore(ttl_seconds=10.0)
    session = store.create_session()
    sid = session.session_id

    # Artificially age the session
    with store._lock:
        store._sessions[sid].last_activity = datetime.now(timezone.utc) - timedelta(seconds=20)

    purged = store.clear_expired_sessions()
    assert purged == 1
    assert store.session_count() == 0


def test_session_store_thread_safe_concurrent_updates():
    store = InMemorySessionStore(ttl_seconds=300.0)
    session = store.create_session()
    sid = session.session_id

    def worker(worker_id: int):
        for i in range(25):
            store.add_message(sid, "user", f"Worker {worker_id} message {i}")
            store.update_slots(sid, {"ranking_preference": "CHEAPEST"})

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, w) for w in range(8)]
        for f in futures:
            f.result()

    final_session = store.get_session(sid)
    assert final_session is not None
    assert len(final_session.history) == store.max_history_messages
    assert final_session.slots.ranking_preference == "CHEAPEST"
