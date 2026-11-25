"""Application configuration for the RailRoute AI feasibility backend.

All secrets are read from environment variables or a local ``.env`` file
(never hardcoded, never committed, never returned by any API endpoint).

The ``.env`` file is resolved relative to the ``backend/`` directory so the
server works regardless of the working directory it is launched from.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/ -> backend/  =>  backend/.env
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Environment-driven settings (see .env.example for documentation)."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "RailRoute AI - RailCore Feasibility Backend"
    app_version: str = "0.4.0"
    environment: str = "local"
    log_level: str = "INFO"

    # --- RailCore provider -------------------------------------------------
    railcore_api_key: SecretStr | None = None
    railcore_api_keys: list[SecretStr] | None = None
    railcore_base_url: str = "https://ir.railcore.tech/v1"
    railcore_timeout_seconds: float = 15.0
    railcore_connect_timeout_seconds: float = 5.0
    # Retries apply ONLY to retryable failures (timeouts, 5xx, 429 rate
    # limits). Permanent errors (401/402/4xx) are never retried.
    railcore_max_retries: int = 2
    railcore_backoff_base_seconds: float = 0.5
    # Optional HMAC-SHA256 signing secret (Growth/Scale/Enterprise plans
    # only). Leave unset for Free/Starter keys.
    railcore_hmac_secret: SecretStr | None = None

    # --- Same-train combination search budgets (phase 2) -------------------
    # Computational safeguards, NOT a maximum number of seat changes: the
    # engine enumerates combinations of ANY size and reports truncation.
    search_max_trains: int = 5
    search_max_api_requests: int = 40
    search_max_credits: int = 80
    search_time_budget_seconds: float = 30.0
    search_max_combinations_per_train: int = 25
    search_max_results: int = 50
    # Timetable data is stable: cache schedules for a long time.
    search_schedule_cache_ttl_seconds: float = 600.0
    # Route discovery is cheap to re-run: short cache.
    search_discovery_cache_ttl_seconds: float = 300.0
    # Optional client-side pacing between provider HTTP calls (seconds),
    # used to stay under the provider's per-minute rate limit. 0 = off.
    search_min_request_interval_seconds: float = 0.0

    # --- Train-change connection search (phase 3) -------------------------
    # Minimum minutes between arriving on one train and departing on the
    # next at an interchange station (0 = catch-on-the-minute allowed).
    train_change_min_transfer_buffer_minutes: int = 30
    # Maximum minutes a passenger may wait at an interchange station.
    train_change_max_transfer_wait_minutes: int = 720
    # Journeys must reach the destination by the end of
    # journey_date + train_change_search_horizon_days.
    train_change_search_horizon_days: int = 2
    # Computational safeguards (NOT train-change limits): the engine
    # explores connections of ANY depth within these budgets and reports
    # truncation honestly.
    train_change_max_discovery_pairs: int = 20
    train_change_max_trains: int = 15
    train_change_max_api_requests: int = 40
    train_change_max_credits: int = 120
    train_change_time_budget_seconds: float = 30.0
    train_change_max_expanded_states: int = 300
    train_change_max_journeys: int = 25
    train_change_max_results: int = 20

    # --- Mixed journey search (phase 4) -----------------------------------
    # Same transfer semantics as the train-change engine: the mixed engine
    # reuses its connection machinery and adds same-train seat-change
    # continuations (no transfer window applies while staying onboard).
    mixed_min_transfer_buffer_minutes: int = 30
    mixed_max_transfer_wait_minutes: int = 720
    mixed_search_horizon_days: int = 2
    # Computational safeguards (NOT seat-change or train-change limits):
    # journeys with ANY number of changes are explored within these budgets
    # and truncation is reported honestly.
    mixed_max_discovery_pairs: int = 20
    mixed_max_trains: int = 15
    mixed_max_api_requests: int = 40
    mixed_max_credits: int = 120
    mixed_time_budget_seconds: float = 30.0
    mixed_max_expanded_states: int = 300
    mixed_max_journeys: int = 25
    mixed_max_results: int = 20

    # --- Unified journey search (phase 5) ---------------------------------
    # ONE shared envelope for a whole unified search across the phase 2,
    # phase 3 and phase 4 engines: maximum API requests, estimated credits
    # and wall-clock time are enforced by the shared SegmentQueryPlanner
    # BEFORE any provider call, and the counters are never reset between
    # engines. An engine whose turn arrives after the envelope is exhausted
    # is skipped with an honestly reported reason.
    unified_max_api_requests: int = 60
    unified_max_credits: int = 150
    unified_time_budget_seconds: float = 45.0
    unified_max_results: int = 30
    unified_schedule_cache_ttl_seconds: float = 600.0
    unified_discovery_cache_ttl_seconds: float = 300.0
    unified_min_request_interval_seconds: float = 0.0

    # --- CORS (development origins) ----------------------------------------
    cors_allowed_origins: list[str] = [
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # --- Date normalization & booking window (Phase 6B) --------------------
    booking_window_max_days: int = 120
    booking_window_enforce: bool = False  # Disabled by default until train-specific exceptions are supported

    # --- LLM Client (Phase 6B.3) -------------------------------------------
    llm_provider: str = "gemini"  # "gemini" or "mock"
    llm_model: str = "gemini-3.5-flash-lite"
    gemini_api_key: SecretStr | None = None
    gemini_api_keys: list[SecretStr] | None = None
    llm_timeout_seconds: float = 30.0

    # --- Conversational Agent Session Store (Phase 6B.4) -------------------
    agent_session_ttl_seconds: float = 3600.0
    agent_max_history_messages: int = 20

    @property
    def gemini_api_keys_list(self) -> list[str]:
        keys: list[str] = []
        if self.gemini_api_keys:
            for k in self.gemini_api_keys:
                val = k.get_secret_value().strip()
                if val and val not in keys:
                    keys.append(val)
        if self.gemini_api_key:
            val = self.gemini_api_key.get_secret_value()
            for part in val.split(","):
                part_clean = part.strip()
                if part_clean and part_clean not in keys:
                    keys.append(part_clean)
        return keys

    @property
    def gemini_api_key_is_configured(self) -> bool:
        return len(self.gemini_api_keys_list) > 0

    @property
    def railcore_api_keys_list(self) -> list[str]:
        keys: list[str] = []
        if self.railcore_api_keys:
            for k in self.railcore_api_keys:
                val = k.get_secret_value().strip()
                if val and val not in keys:
                    keys.append(val)
        if self.railcore_api_key:
            val = self.railcore_api_key.get_secret_value()
            for part in val.split(","):
                part_clean = part.strip()
                if part_clean and part_clean not in keys:
                    keys.append(part_clean)
        return keys

    @property
    def railcore_api_key_is_configured(self) -> bool:
        return len(self.railcore_api_keys_list) > 0

    @property
    def railcore_hmac_enabled(self) -> bool:
        value = self.railcore_hmac_secret.get_secret_value() if self.railcore_hmac_secret else ""
        return bool(value.strip())


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (override in tests via monkeypatch + cache clear)."""
    return Settings()
