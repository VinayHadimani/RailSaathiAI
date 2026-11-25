"""RailCore API client.

Official documentation (verified 2026-11-23):
- Quickstart:      https://railcore.tech/docs/quickstart
- Train schedule:  https://railcore.tech/docs/api/train-schedule
- Availability:    https://railcore.tech/docs/api/seat-availability

Facts implemented here (never guessed, taken from the docs):
- Base URL:            https://ir.railcore.tech/v1
- Auth header:         X-RailCore-Key: rk_live_...
  (Authorization: Bearer and X-Api-Key also accepted; only one is sent.
  HMAC-SHA256 headers are added automatically when a signing secret is
  configured, which is only required for Growth/Scale/Enterprise plans.)
- Train schedule:      GET /trains/{train_number}/schedule?include_intermediate=false|true
- Seat availability:   GET /availability/seats?train_number=&from=&to=&date=&class=&quota=
- Envelope:            {"success": bool, "data": {...}, "error": {...}, "meta": {...}}
- Rate limits (free):  10/minute, 100/day -> HTTP 429 with Retry-After
- Credits:             1 per successful request; failed requests are free
  (exposed via X-RailCore-Credits-Cost / X-RailCore-Credits-Remaining)

Safety rules:
- The API key is read from configuration, sent only in the auth header,
  never logged, never returned in responses, never placed in URLs.
- Retries apply ONLY to retryable failures (timeouts, transport errors,
  5xx, 429). Permanent errors (401/402/other 4xx) are never retried.
- An API failure is never translated into an availability result.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_lib
import json
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Generic, TypeVar
from urllib.parse import urlencode, urlparse

import httpx

from ..models.availability import SegmentAvailability
from ..models.route_search import RouteTrainsResult
from ..models.schedule import TrainSchedule
from ..core.key_pool import APIKeyPool, mask_key
from .base import (
    ProviderAuthError,
    ProviderClientError,
    ProviderCreditsError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderUnexpectedResponseError,
    RailwayProviderError,
)
from .normalizer import RailCoreNormalizer

logger = logging.getLogger("railroute.providers.railcore")

T = TypeVar("T")

# A 429 whose Retry-After is larger than this is not retried inside a
# request: blocking an API response for minutes would be worse than
# failing fast with a clear error.
MAX_INLINE_RETRY_AFTER_SECONDS = 5.0


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class BatchAvailabilityResult:
    """Result of a batch availability call.

    items: normalized per-item results (items the provider answered with
    a classes array).
    failed_items: raw per-item failures the provider returned in-place
    (e.g. BOOKING_NOT_OPEN, QUOTA_NOT_SUPPORTED) - these are NOT
    availability information and must never be treated as such.
    """

    items: list[SegmentAvailability]
    failed_items: list[dict[str, Any]]
    raw: dict[str, Any]
    request_id: str | None = None
    credits_cost: int | None = None
    http_status: int | None = None
    checked_at: str = field(default_factory=_utc_now_iso)


@dataclass
class ProviderResponse(Generic[T]):
    """A successful provider call: normalized model plus raw payload.

    The raw payload is preserved for feasibility analysis and debugging;
    it never contains credentials.
    """

    data: T
    raw: dict[str, Any]
    request_id: str | None = None
    credits_cost: int | None = None
    http_status: int | None = None
    checked_at: str = field(default_factory=_utc_now_iso)


@dataclass
class _RawCall:
    payload: dict[str, Any]
    http_status: int
    request_id: str | None
    credits_cost: int | None


class RailCoreClient:
    """Synchronous RailCore provider client (httpx-based).

    An ``http_client`` can be injected for unit testing (MockTransport);
    in production the client creates and owns its own httpx.Client.
    """

    provider_name = "railcore"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_keys: Sequence[str] | None = None,
        base_url: str = "https://ir.railcore.tech/v1",
        timeout_seconds: float = 15.0,
        connect_timeout_seconds: float = 5.0,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.5,
        hmac_secret: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        raw_keys: list[str] = []
        if api_keys:
            raw_keys.extend(api_keys)
        if api_key:
            raw_keys.append(api_key)
        self._key_pool = APIKeyPool(raw_keys, name="RailCore")
        self._api_key = self._key_pool.keys[0] if self._key_pool.keys else None
        self._base_url = (base_url or "https://ir.railcore.tech/v1").rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = float(backoff_base_seconds)
        self._hmac_secret = (hmac_secret or "").strip() or None
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.Client(timeout=self._timeout)
        self._normalizer = RailCoreNormalizer()

        # Usage metrics (spec section 18: track API requests and credits).
        self.stats: dict[str, int] = {
            "requests_sent": 0,
            "successful_requests": 0,
            "failed_requests": 0,
        }
        self.credits_cost_total = 0
        self.credits_remaining: int | None = None
        self.rate_limit_remaining: int | None = None
        self.rate_limit_day_remaining: int | None = None

    @property
    def key_pool(self) -> APIKeyPool:
        return self._key_pool

    # ------------------------------------------------------------------ API

    def get_train_schedule(
        self, train_number: str, *, include_intermediate: bool = False
    ) -> ProviderResponse[TrainSchedule]:
        """GET /trains/{train_number}/schedule - complete train timetable.

        ``include_intermediate=True`` adds non-stop pass-through route
        points (the "full route" view). Boarding/alighting checks use the
        default stops-only view.
        """
        train_number = str(train_number).strip()
        call = self._request(
            "GET",
            f"/trains/{train_number}/schedule",
            {"include_intermediate": "true" if include_intermediate else "false"},
        )
        schedule = self._normalizer.train_schedule(call.payload)
        return ProviderResponse(
            data=schedule,
            raw=call.payload,
            request_id=call.request_id,
            credits_cost=call.credits_cost,
            http_status=call.http_status,
        )

    def get_train_route(self, train_number: str) -> ProviderResponse[TrainSchedule]:
        """Full route including pass-through points (schedule alias)."""
        return self.get_train_schedule(train_number, include_intermediate=True)

    def get_seat_availability(
        self,
        *,
        train_number: str,
        from_station: str,
        to_station: str,
        journey_date: str,
        travel_class: str,
        quota: str = "GN",
    ) -> ProviderResponse[SegmentAvailability]:
        """GET /availability/seats - seat availability for one segment.

        Checks the availability of a single station-to-station segment on
        one train, for one date/class/quota combination. The same call is
        used for every segment of a journey; availability of one segment
        is never assumed for another (spec section 7).
        """
        params = {
            "train_number": str(train_number).strip(),
            "from": str(from_station).strip().upper(),
            "to": str(to_station).strip().upper(),
            "date": str(journey_date),
            "class": str(travel_class).strip().upper(),
            "quota": str(quota).strip().upper(),
        }
        call = self._request("GET", "/availability/seats", params)
        availability = self._normalizer.segment_availability(
            call.payload,
            train_number=params["train_number"],
            from_station=params["from"],
            to_station=params["to"],
            journey_date=params["date"],
            travel_class=params["class"],
            quota=params["quota"],
        )
        return ProviderResponse(
            data=availability,
            raw=call.payload,
            request_id=call.request_id,
            credits_cost=call.credits_cost,
            http_status=call.http_status,
        )

    def get_trains_between_stations(
        self,
        *,
        from_station: str,
        to_station: str,
        journey_date: str,
        quota: str | None = "GN",
        match: str = "station",
    ) -> ProviderResponse[RouteTrainsResult]:
        """GET /routes/trains - trains serving a route on a date.

        Candidate discovery for the journey search engine. ``match="station"``
        restricts results to trains actually running between the two requested
        stations (no city expansion). The per-train ``availability_summary``
        is informational context only and can be days old; it is never used
        as availability evidence by the search engine.

        ``quota`` is forwarded only for GN/TQ (the values the endpoint
        documents); other quotas are omitted so the request is not rejected.
        """
        params: dict[str, str] = {
            "from": str(from_station).strip().upper(),
            "to": str(to_station).strip().upper(),
            "date": str(journey_date),
            "match": match,
        }
        if quota and str(quota).strip().upper() in ("GN", "TQ"):
            params["quota"] = str(quota).strip().upper()
        call = self._request("GET", "/routes/trains", params)
        result = self._normalizer.route_trains(
            call.payload,
            from_station=params["from"],
            to_station=params["to"],
            journey_date=params["date"],
            quota=quota,
        )
        return ProviderResponse(
            data=result,
            raw=call.payload,
            request_id=call.request_id,
            credits_cost=call.credits_cost,
            http_status=call.http_status,
        )

    def get_batch_availability(self, items: list[dict[str, Any]]) -> BatchAvailabilityResult:
        """POST /availability/seats/batch - up to 25 segment/class checks.

        Each item is a dict: {"train_number", "from", "to", "date",
        "classes": [.. up to 8 ..], "quota"?}. Credits: 1 request per
        distinct class per item, minimum 1 (per provider docs).

        Items the provider fails in place (e.g. BOOKING_NOT_OPEN) are
        returned in failed_items and carry no availability claim.
        """
        if not 1 <= len(items) <= 25:
            raise ValueError("batch availability accepts 1 to 25 items")
        call = self._request("POST", "/availability/seats/batch", None, json_body={"items": items})
        data = call.payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ProviderUnexpectedResponseError(
                "RailCore batch availability response is missing the 'results' array",
                code="MALFORMED_BATCH_AVAILABILITY",
            )
        normalized: list[SegmentAvailability] = []
        failed: list[dict[str, Any]] = []
        for index, result in enumerate(data["results"]):
            if isinstance(result, dict) and isinstance(result.get("classes"), list):
                # If the caller asked for exactly one class, surface its
                # status directly; multi-class items expose all_classes.
                declared = items[index].get("classes") if index < len(items) and isinstance(items[index], dict) else None
                requested_class = declared[0] if isinstance(declared, list) and len(declared) == 1 else ""
                normalized.append(
                    self._normalizer.segment_availability(
                        {"success": True, "data": result, "meta": call.payload.get("meta", {})},
                        train_number=str(result.get("train_number") or ""),
                        from_station=str(result.get("from_station_code") or ""),
                        to_station=str(result.get("to_station_code") or ""),
                        journey_date=str(result.get("journey_date") or ""),
                        travel_class=str(requested_class),
                        quota=str(result.get("quota") or ""),
                    )
                )
            else:
                failed.append(result if isinstance(result, dict) else {"unexpected": result})
        return BatchAvailabilityResult(
            items=normalized,
            failed_items=failed,
            raw=call.payload,
            request_id=call.request_id,
            credits_cost=call.credits_cost,
            http_status=call.http_status,
        )

    # ------------------------------------------------------------- internals

    def stats_snapshot(self) -> dict[str, int]:
        return dict(self.stats)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None = None,
    ) -> _RawCall:
        if not self._key_pool.is_configured:
            raise ProviderNotConfiguredError(
                "RailCore API key is not configured. Copy backend/.env.example "
                "to backend/.env and set RAILCORE_API_KEY, then restart the server."
            )
        candidates = self._key_pool.get_candidates()
        if not candidates:
            raise ProviderNotConfiguredError("RailCore API key is not configured.")

        # If every key in the pool is already in cooldown with more than
        # MAX_INLINE_RETRY_AFTER_SECONDS remaining, fail fast with RATE_LIMITED
        # instead of hammering the upstream API with doomed calls.
        min_cd = self._key_pool.min_cooldown_remaining()
        if min_cd > MAX_INLINE_RETRY_AFTER_SECONDS:
            raise ProviderRateLimitError(
                f"All RailCore API keys are rate-limited (cooldown {min_cd:.1f}s remaining).",
                code="RATE_LIMITED",
                http_status=429,
                details={"path": path, "retry_after_seconds": round(min_cd, 1)},
            )
        if 0.0 < min_cd <= MAX_INLINE_RETRY_AFTER_SECONDS:
            logger.info(
                "All RailCore keys in short cooldown; waiting %.2fs for earliest key recovery...",
                min_cd + 0.05,
            )
            time.sleep(min_cd + 0.05)
            candidates = self._key_pool.get_candidates()

        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        body_bytes = b""
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")

        attempts_allowed = 1 + self._max_retries
        last_error: RailwayProviderError | None = None

        for key_idx, current_key in enumerate(candidates):
            headers = {
                "Accept": "application/json",
                "User-Agent": "railroute-ai-feasibility/0.1 (+https://railcore.tech docs)",
                "X-RailCore-Key": current_key,  # never logged, never in URLs
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if self._hmac_secret:
                headers.update(self._hmac_headers(method, path, query, body_bytes))

            key_error: RailwayProviderError | None = None
            for attempt in range(1, attempts_allowed + 1):
                if self._owns_client and attempt == 1:
                    # Gentle per-key pacing for live HTTP pools (10 req/min/key free tier):
                    # spreads bursts across healthy keys without slowing mock unit tests.
                    healthy = self._key_pool.available_count()
                    per_key_gap = 2.5 if healthy >= 5 else (4.0 if healthy == 4 else (5.5 if healthy >= 2 else 3.0))
                    self._key_pool.wait_for_key_gap(current_key, per_key_gap)
                started = time.monotonic()
                response: httpx.Response | None = None
                try:
                    response = self._http.request(
                        method, url, headers=headers, content=body_bytes if body_bytes else None
                    )
                except httpx.TimeoutException as exc:
                    key_error = ProviderTimeoutError(
                        f"RailCore request timed out: {exc}",
                        details={"path": path, "attempt": attempt},
                    )
                except httpx.TransportError as exc:
                    key_error = ProviderTransportError(
                        f"RailCore network error: {exc}",
                        details={"path": path, "attempt": attempt},
                    )
                else:
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    self.stats["requests_sent"] += 1
                    self._absorb_usage_headers(response)
                    request_id = response.headers.get("x-railcore-request-id")
                    logger.info(
                        "railcore_call path=%s http=%s elapsed_ms=%s attempt=%s/%s request_id=%s key=%s",
                        path, response.status_code, elapsed_ms, attempt, attempts_allowed, request_id, mask_key(current_key),
                    )

                    if 200 <= response.status_code < 300:
                        payload = self._parse_json(response, path)
                        envelope_error = self._envelope_error(payload, response, path)
                        if envelope_error is not None:
                            self.stats["failed_requests"] += 1
                            key_error = envelope_error
                        else:
                            self.stats["successful_requests"] += 1
                            self._key_pool.advance()
                            return _RawCall(
                                payload=payload,
                                http_status=response.status_code,
                                request_id=request_id,
                                credits_cost=self._header_int(response, "x-railcore-credits-cost"),
                            )
                    else:
                        self.stats["failed_requests"] += 1
                        key_error = self._http_error(response, path)

                # Retry on the current key if retryable and retry_after <= MAX_INLINE_RETRY_AFTER_SECONDS
                # UNLESS it is 429/402 and we have other healthy keys to failover to immediately!
                retry_after = self._retry_after_seconds(response) if response is not None else None
                is_rate_limit_or_credits = isinstance(key_error, (ProviderRateLimitError, ProviderCreditsError))
                has_other_keys = (key_idx + 1 < len(candidates))

                if is_rate_limit_or_credits and has_other_keys:
                    # Don't sleep/retry on this exhausted key: immediately fail over to the next healthy key!
                    break

                should_retry = (
                    attempt < attempts_allowed
                    and key_error is not None
                    and key_error.retryable
                    and (retry_after is None or retry_after <= MAX_INLINE_RETRY_AFTER_SECONDS)
                )
                if should_retry:
                    delay = self._retry_delay(attempt, retry_after)
                    logger.warning(
                        "railcore_retry path=%s code=%s attempt=%s/%s retry_in=%.2fs",
                        path, key_error.code if key_error else "?", attempt, attempts_allowed, delay,
                    )
                    time.sleep(delay)
                    continue

                break

            last_error = key_error
            # If current key failed due to rate limit, credits exhaustion, or invalid auth in a multi-key pool:
            if isinstance(key_error, ProviderRateLimitError):
                retry_after_cd = key_error.details.get("retry_after_seconds") if key_error.details else None
                self._key_pool.mark_rate_limited(current_key, retry_after_cd or 60.0)
                if key_idx + 1 < len(candidates):
                    logger.warning(
                        "RailCore key %s rate-limited (code=%s). Failing over to next key (%d remaining)...",
                        mask_key(current_key),
                        key_error.code,
                        len(candidates) - (key_idx + 1),
                    )
                    continue
            elif isinstance(key_error, ProviderCreditsError):
                self._key_pool.mark_rate_limited(current_key, 3600.0)
                if key_idx + 1 < len(candidates):
                    logger.warning(
                        "RailCore key %s exhausted credits (code=%s). Benching and failing over (%d remaining)...",
                        mask_key(current_key),
                        key_error.code,
                        len(candidates) - (key_idx + 1),
                    )
                    continue
            elif isinstance(key_error, ProviderAuthError) and key_idx + 1 < len(candidates):
                self._key_pool.mark_rate_limited(current_key, 3600.0)
                logger.warning(
                    "RailCore key %s failed authentication (code=%s). Failing over to next key (%d remaining)...",
                    mask_key(current_key),
                    key_error.code,
                    len(candidates) - (key_idx + 1),
                )
                continue

            # If it's a non-rate-limit error (e.g. AuthError 401 with no fallback, ClientError 404), raise immediately
            if last_error is not None:
                raise last_error

        raise last_error or ProviderUnexpectedResponseError("RailCore request failed")

    def _parse_json(self, response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnexpectedResponseError(
                f"RailCore returned a non-JSON body (HTTP {response.status_code})",
                code="INVALID_JSON",
                http_status=response.status_code,
                details={"path": path, "body_head": response.text[:200]},
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderUnexpectedResponseError(
                "RailCore response JSON is not an object",
                code="INVALID_JSON",
                http_status=response.status_code,
                details={"path": path},
            )
        return payload

    def _envelope_error(
        self, payload: dict[str, Any], response: httpx.Response, path: str
    ) -> RailwayProviderError | None:
        """Defensive check: a 2xx whose envelope says success=false."""
        if payload.get("success") is True:
            return None
        err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        return ProviderUnexpectedResponseError(
            str(err.get("message") or "RailCore returned success=false with HTTP 2xx"),
            code=str(err.get("code") or "UNEXPECTED_ENVELOPE"),
            http_status=response.status_code,
            request_id=response.headers.get("x-railcore-request-id"),
            details={"path": path},
        )

    def _http_error(self, response: httpx.Response, path: str) -> RailwayProviderError:
        status = response.status_code
        request_id = response.headers.get("x-railcore-request-id")
        code: str | None = None
        message = f"RailCore returned HTTP {status}"
        retry_after = self._retry_after_seconds(response)
        try:
            payload = response.json()
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    code = err.get("code") if isinstance(err.get("code"), str) else None
                    message = err.get("message") if isinstance(err.get("message"), str) else message
        except ValueError:
            message = f"RailCore returned HTTP {status} with a non-JSON body"

        details: dict[str, Any] = {"path": path}
        if retry_after is not None:
            details["retry_after_seconds"] = retry_after

        if status in (401, 403):
            return ProviderAuthError(message, code=code or "AUTH_ERROR", http_status=status, request_id=request_id, details=details)
        if status == 402:
            return ProviderCreditsError(message, code=code or "CREDITS_EXHAUSTED", http_status=status, request_id=request_id, details=details)
        if status == 404:
            return ProviderClientError(message, code=code or "NOT_FOUND", http_status=status, request_id=request_id, details=details)
        if status == 429:
            return ProviderRateLimitError(message, code=code or "RATE_LIMITED", http_status=status, request_id=request_id, details=details)
        if 400 <= status < 500:
            return ProviderClientError(message, code=code or f"HTTP_{status}", http_status=status, request_id=request_id, details=details)
        return ProviderServerError(message, code=code or f"HTTP_{status}", http_status=status, request_id=request_id, details=details)

    def _absorb_usage_headers(self, response: httpx.Response) -> None:
        credits_cost = self._header_int(response, "x-railcore-credits-cost")
        if credits_cost:
            self.credits_cost_total += credits_cost
        credits_remaining = self._header_int(response, "x-railcore-credits-remaining")
        if credits_remaining is not None:
            self.credits_remaining = credits_remaining
        rate_remaining = self._header_int(response, "x-railcore-ratelimit-remaining")
        if rate_remaining is not None:
            self.rate_limit_remaining = rate_remaining
        day_remaining = self._header_int(response, "x-railcore-ratelimit-day-remaining")
        if day_remaining is not None:
            self.rate_limit_day_remaining = day_remaining

    @staticmethod
    def _header_int(response: httpx.Response, name: str) -> int | None:
        try:
            return int(response.headers[name])
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        exponential = self._backoff_base * (2 ** (attempt - 1))
        return exponential + random.uniform(0, 0.25)

    def _hmac_headers(
        self, method: str, path: str, query: str, body_bytes: bytes = b""
    ) -> dict[str, str]:
        """HMAC-SHA256 signing for Growth/Scale/Enterprise plan keys.

        Signature payload (per docs): METHOD, path, query, SHA256(body),
        timestamp, nonce - joined with newlines. GET requests have an
        empty body, hashed as the SHA256 of zero bytes; POST bodies are
        hashed from the exact bytes sent.
        """
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        base_path = urlparse(self._base_url).path.rstrip("/")
        sign_path = f"{base_path}{path}"
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        payload = "\n".join([method.upper(), sign_path, query, body_hash, timestamp, nonce])
        signature = base64.b64encode(
            hmac_lib.new(self._hmac_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        return {
            "X-RailCore-Timestamp": timestamp,
            "X-RailCore-Nonce": nonce,
            "X-RailCore-Signature": signature,
        }
