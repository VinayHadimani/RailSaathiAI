"""Thread-safe API Key Pool with round-robin load distribution and cooldown-based rate-limit failover.

Designed for high-availability multi-key rotation across LLM providers (Gemini)
and railway transport providers (RailCore).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Sequence

logger = logging.getLogger("railroute.core.key_pool")


def mask_key(key: str | None) -> str:
    """Safely mask an API key for logs without leaking secret contents.

    Examples:
        rk_live_example1234567890 -> rk_liv...7890
        AQ.Ab8_example1234567890 -> AQ.Ab8...7890
    """
    if not key:
        return "[EMPTY]"
    clean = str(key).strip()
    if len(clean) <= 8:
        return "[REDACTED]"
    return f"{clean[:6]}...{clean[-4:]}"


class APIKeyPool:
    """Thread-safe key pool providing round-robin distribution and rate-limit failover.

    Features:
    - Round-robin key selection across calls to balance quota and RPM evenly.
    - Automatic exclusion of keys during their cooldown period after a 429 / quota error.
    - Seamless failover: on rate limit, caller can immediately retrieve the next candidate.
    - Automatic recovery: once a key's cooldown window expires, it returns to the active pool.
    - Safe credential hygiene: keys are masked in all log outputs.
    """

    def __init__(
        self,
        keys: str | Sequence[str] | None,
        *,
        default_cooldown_seconds: float = 60.0,
        name: str = "KeyPool",
    ) -> None:
        self._name = name
        self._default_cooldown = max(1.0, float(default_cooldown_seconds))
        self._lock = threading.Lock()

        # Parse and normalize key list
        raw_keys: list[str] = []
        if isinstance(keys, str):
            for part in keys.split(","):
                part_clean = part.strip()
                if part_clean:
                    raw_keys.append(part_clean)
        elif keys is not None:
            for item in keys:
                if item:
                    item_str = str(item).strip()
                    if "," in item_str:
                        for sub in item_str.split(","):
                            sub_clean = sub.strip()
                            if sub_clean:
                                raw_keys.append(sub_clean)
                    elif item_str:
                        raw_keys.append(item_str)

        # Deduplicate preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for k in raw_keys:
            if k not in seen:
                seen.add(k)
                deduped.append(k)

        self._keys = deduped
        self._index = 0
        # Maps key -> monotonic timestamp when cooldown expires
        self._cooldowns: dict[str, float] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    @property
    def total_count(self) -> int:
        return len(self._keys)

    @property
    def is_configured(self) -> bool:
        return len(self._keys) > 0

    def available_count(self, now: float | None = None) -> int:
        """Count of keys not currently in cooldown."""
        t = time.monotonic() if now is None else now
        with self._lock:
            return sum(1 for k in self._keys if self._cooldowns.get(k, 0.0) <= t)

    def get_candidates(self) -> list[str]:
        """Get ordered list of candidate keys to try for a request.

        Strategy:
        1. Select keys not in cooldown, starting from the current round-robin index.
        2. If all keys are currently in cooldown, return all keys ordered by earliest
           cooldown expiration so the closest-to-recovery key is attempted.
        """
        now = time.monotonic()
        with self._lock:
            if not self._keys:
                return []

            # Purge expired cooldowns
            expired = [k for k, expire_at in self._cooldowns.items() if expire_at <= now]
            for k in expired:
                del self._cooldowns[k]

            total = len(self._keys)
            start_idx = self._index % total

            # Rotate keys to start from current index
            rotated_keys = self._keys[start_idx:] + self._keys[:start_idx]

            # Available (healthy) keys
            healthy = [k for k in rotated_keys if k not in self._cooldowns]
            if healthy:
                return healthy

            # All keys are in cooldown: sort by earliest cooldown expiration
            logger.warning(
                "[%s] All %d keys are in cooldown. Attempting key closest to recovery.",
                self._name,
                total,
            )
            return sorted(self._keys, key=lambda k: self._cooldowns.get(k, 0.0))

    def advance(self) -> None:
        """Advance round-robin pointer to balance next request across keys."""
        with self._lock:
            if self._keys:
                self._index = (self._index + 1) % len(self._keys)

    def mark_rate_limited(
        self,
        key: str,
        cooldown_seconds: float | None = None,
    ) -> None:
        """Mark a key as rate-limited with a cooldown period."""
        cd = float(cooldown_seconds) if cooldown_seconds and cooldown_seconds > 0 else self._default_cooldown
        now = time.monotonic()
        with self._lock:
            if key in self._keys:
                expire_at = now + cd
                self._cooldowns[key] = expire_at
                # Advance index so next request doesn't start on this rate-limited key
                if self._keys[self._index % len(self._keys)] == key:
                    self._index = (self._index + 1) % len(self._keys)

        logger.warning(
            "[%s] Key %s rate-limited / quota exhausted. Cooldown set for %.1fs (%d/%d keys healthy).",
            self._name,
            mask_key(key),
            cd,
            self.available_count(now),
            self.total_count,
        )

    def reset_cooldowns(self) -> None:
        """Reset all cooldowns (primarily for testing)."""
        with self._lock:
            self._cooldowns.clear()
            self._index = 0
