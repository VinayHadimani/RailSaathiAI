"""FastAPI dependencies.

The RailCore client is a process-level singleton so usage metrics (API
requests, credits) accumulate correctly across requests. Tests override
this dependency with a client built on httpx.MockTransport.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends

from ..agent.extractor import IntentAndSlotExtractor
from ..agent.llm import get_llm_client
from ..agent.orchestrator import AgentOrchestrator
from ..agent.session_store import InMemorySessionStore
from ..core.config import get_settings
from ..providers.railcore import RailCoreClient
from ..services.unified_journey_service import UnifiedJourneyOrchestrationService


@lru_cache
def get_railcore_client() -> RailCoreClient:
    settings = get_settings()
    env = settings.environment.strip().lower()
    llm_prov = settings.llm_provider.strip().lower()
    if env in ("demo", "mock") or (
        llm_prov == "mock" and not settings.railcore_api_key_is_configured
    ):
        from ..providers.mock_provider import create_mock_railcore_client
        return create_mock_railcore_client()

    api_keys = settings.railcore_api_keys_list
    api_key = (
        settings.railcore_api_key.get_secret_value()
        if settings.railcore_api_key is not None
        else None
    )
    hmac_secret = (
        settings.railcore_hmac_secret.get_secret_value()
        if settings.railcore_hmac_secret is not None
        else None
    )
    return RailCoreClient(
        api_key=api_key,
        api_keys=api_keys,
        base_url=settings.railcore_base_url,
        timeout_seconds=settings.railcore_timeout_seconds,
        connect_timeout_seconds=settings.railcore_connect_timeout_seconds,
        max_retries=settings.railcore_max_retries,
        backoff_base_seconds=settings.railcore_backoff_base_seconds,
        hmac_secret=hmac_secret,
    )


@lru_cache
def get_session_store() -> InMemorySessionStore:
    settings = get_settings()
    return InMemorySessionStore(
        ttl_seconds=settings.agent_session_ttl_seconds,
        max_history_messages=settings.agent_max_history_messages,
    )


def get_agent_orchestrator(
    client: RailCoreClient = Depends(get_railcore_client),
    session_store: InMemorySessionStore = Depends(get_session_store),
) -> AgentOrchestrator:
    settings = get_settings()
    llm_client = get_llm_client(settings)
    extractor = IntentAndSlotExtractor(
        llm_client=llm_client,
        booking_window_enforce=settings.booking_window_enforce,
    )
    unified_service = UnifiedJourneyOrchestrationService(client)
    return AgentOrchestrator(
        extractor=extractor,
        session_store=session_store,
        unified_service=unified_service,
    )
