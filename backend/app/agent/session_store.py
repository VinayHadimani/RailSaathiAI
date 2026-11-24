"""Thread-safe in-memory session store for conversational agent state (Phase 6B.4).

Maintains journey slots, conversation history, and ambiguity state across
turns using UUID session identifiers and configurable TTL expiration.

Limitations of In-Memory Session Storage:
------------------------------------------
1. Process Lifetime: All session state resides strictly in process memory.
   Restarting or redeploying the FastAPI application clears all active sessions.
2. Multi-Worker Concurrency: In multi-worker process environments (e.g.
   `uvicorn main:app --workers 4`), each process maintains its own isolated
   session dictionary unless sticky routing / process affinity is configured.
   For multi-container or clustered production deployments, this store should
   be backed by an external distributed store such as Redis.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from typing import Any
import uuid

from .schemas import AgentSlots, SessionMessage, SessionState, StationCandidate

logger = logging.getLogger(__name__)


class InMemorySessionStore:
    """Thread-safe, in-memory session store with TTL expiration and history bounds."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        max_history_messages: int = 20,
    ) -> None:
        self._ttl_seconds = float(ttl_seconds)
        self._max_history_messages = int(max_history_messages)
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    @property
    def max_history_messages(self) -> int:
        return self._max_history_messages

    def _is_expired(self, session: SessionState, now: datetime) -> bool:
        elapsed = (now - session.last_activity).total_seconds()
        return elapsed > self._ttl_seconds

    def _purge_expired_locked(self, now: datetime) -> int:
        expired_keys = [
            sid for sid, session in self._sessions.items()
            if self._is_expired(session, now)
        ]
        for sid in expired_keys:
            del self._sessions[sid]
        return len(expired_keys)

    def create_session(self, session_id: str | None = None) -> SessionState:
        """Create and store a new conversation session.

        Args:
            session_id: Optional client-provided UUID string. If None or invalid,
                        a standard UUID4 is generated.

        Returns:
            A deep copy of the newly created SessionState.
        """
        now = datetime.now(timezone.utc)
        if not session_id:
            session_id = str(uuid.uuid4())
        else:
            try:
                # Normalize and validate UUID
                session_id = str(uuid.UUID(session_id.strip()))
            except (ValueError, AttributeError):
                session_id = str(uuid.uuid4())

        new_session = SessionState(
            session_id=session_id,
            slots=AgentSlots(),
            history=[],
            ambiguous_candidates={},
            unverified_stations={},
            created_at=now,
            last_activity=now,
        )

        with self._lock:
            self._purge_expired_locked(now)
            self._sessions[session_id] = new_session
            return new_session.model_copy(deep=True)

    def get_session(self, session_id: str, *, touch: bool = True) -> SessionState | None:
        """Retrieve an existing session if present and not expired.

        Args:
            session_id: UUID of the session.
            touch: If True, updates the session's last_activity timestamp.

        Returns:
            A deep copy of SessionState if active, or None if expired/non-existent.
        """
        if not session_id:
            return None

        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None

            if self._is_expired(session, now):
                del self._sessions[session_id]
                return None

            if touch:
                session.last_activity = now

            return session.model_copy(deep=True)

    def get_or_create_session(self, session_id: str | None = None) -> tuple[SessionState, bool]:
        """Get an existing valid session or create a new one.

        Returns:
            Tuple of (SessionState, is_new).
        """
        if session_id:
            existing = self.get_session(session_id, touch=True)
            if existing is not None:
                return existing, False

        new_session = self.create_session(session_id)
        return new_session, True

    def update_slots(
        self,
        session_id: str,
        slot_updates: dict[str, Any] | AgentSlots,
    ) -> SessionState:
        """Update specific journey slots without overwriting unrelated valid slots.

        Supports explicit corrections (e.g. changing only destination while
        keeping origin and date intact).
        """
        now = datetime.now(timezone.utc)
        updates: dict[str, Any]
        if isinstance(slot_updates, AgentSlots):
            updates = slot_updates.model_dump(exclude_unset=True)
        else:
            updates = dict(slot_updates)

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or self._is_expired(session, now):
                # Create a fresh session if missing or expired
                session = SessionState(
                    session_id=session_id or str(uuid.uuid4()),
                    slots=AgentSlots(),
                    history=[],
                    ambiguous_candidates={},
                    unverified_stations={},
                    created_at=now,
                    last_activity=now,
                )
                self._sessions[session.session_id] = session

            for field_name, value in updates.items():
                if hasattr(session.slots, field_name) and value is not None:
                    setattr(session.slots, field_name, value)

            session.last_activity = now
            return session.model_copy(deep=True)

    def update_ambiguous_candidates(
        self,
        session_id: str,
        candidates: dict[str, list[StationCandidate]],
    ) -> SessionState:
        """Store ambiguous candidate clusters awaiting resolution."""
        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or self._is_expired(session, now):
                session = SessionState(
                    session_id=session_id or str(uuid.uuid4()),
                    slots=AgentSlots(),
                    history=[],
                    ambiguous_candidates={},
                    unverified_stations={},
                    created_at=now,
                    last_activity=now,
                )
                self._sessions[session.session_id] = session

            session.ambiguous_candidates = {
                k: [c.model_copy(deep=True) for c in v]
                for k, v in candidates.items()
            }
            session.last_activity = now
            return session.model_copy(deep=True)

    def update_unverified_stations(
        self,
        session_id: str,
        unverified: dict[str, str],
    ) -> SessionState:
        """Store unverified station codes awaiting user verification."""
        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or self._is_expired(session, now):
                session = SessionState(
                    session_id=session_id or str(uuid.uuid4()),
                    slots=AgentSlots(),
                    history=[],
                    ambiguous_candidates={},
                    unverified_stations={},
                    created_at=now,
                    last_activity=now,
                )
                self._sessions[session.session_id] = session

            session.unverified_stations = dict(unverified)
            session.last_activity = now
            return session.model_copy(deep=True)

    def cache_search_result(
        self,
        session_id: str,
        request_data: dict[str, Any],
        response_data: dict[str, Any],
    ) -> SessionState:
        """Cache the most recent unified search request and response in session state."""
        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or self._is_expired(session, now):
                session = SessionState(
                    session_id=session_id or str(uuid.uuid4()),
                    slots=AgentSlots(),
                    history=[],
                    ambiguous_candidates={},
                    unverified_stations={},
                    created_at=now,
                    last_activity=now,
                )
                self._sessions[session.session_id] = session

            session.last_search_request = dict(request_data)
            session.last_search_response = dict(response_data)
            session.last_activity = now
            return session.model_copy(deep=True)

    def add_message(self, session_id: str, role: str, content: str) -> SessionState:
        """Append a message turn to conversation history, truncating to max bounds."""
        now = datetime.now(timezone.utc)
        clean_content = str(content).strip()
        msg = SessionMessage(role=role, content=clean_content, timestamp=now)

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or self._is_expired(session, now):
                session = SessionState(
                    session_id=session_id or str(uuid.uuid4()),
                    slots=AgentSlots(),
                    history=[],
                    ambiguous_candidates={},
                    unverified_stations={},
                    created_at=now,
                    last_activity=now,
                )
                self._sessions[session.session_id] = session

            session.history.append(msg)
            # Enforce max history bounds
            if len(session.history) > self._max_history_messages:
                session.history = session.history[-self._max_history_messages:]

            session.last_activity = now
            return session.model_copy(deep=True)

    def delete_session(self, session_id: str) -> bool:
        """Explicitly remove a session."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def clear_expired_sessions(self) -> int:
        """Purge all sessions that have exceeded the TTL."""
        now = datetime.now(timezone.utc)
        with self._lock:
            return self._purge_expired_locked(now)

    def session_count(self) -> int:
        """Total active sessions currently in memory."""
        with self._lock:
            return len(self._sessions)

    def clear_all(self) -> None:
        """Clear all sessions (useful for test resets)."""
        with self._lock:
            self._sessions.clear()
