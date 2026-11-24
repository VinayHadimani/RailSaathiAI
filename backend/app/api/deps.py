"""FastAPI dependencies.

The RailCore client is a process-level singleton so usage metrics (API
requests, credits) accumulate correctly across requests. Tests override
this dependency with a client built on httpx.MockTransport.
"""

from __future__ import annotations

from functools import lru_cache

from ..core.config import get_settings
from ..providers.railcore import RailCoreClient


@lru_cache
def get_railcore_client() -> RailCoreClient:
    settings = get_settings()
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
        base_url=settings.railcore_base_url,
        timeout_seconds=settings.railcore_timeout_seconds,
        connect_timeout_seconds=settings.railcore_connect_timeout_seconds,
        max_retries=settings.railcore_max_retries,
        backoff_base_seconds=settings.railcore_backoff_base_seconds,
        hmac_secret=hmac_secret,
    )
