"""Timezone-aware date normalizer for railway journey discovery (Phase 6B).

Uses Asia/Kolkata (Indian Standard Time) as the reference timezone.
Deterministically resolves relative date expressions ('today', 'tomorrow',
'day after tomorrow', 'next Friday') and validates against past dates.
Booking window enforcement is configurable and disabled by default.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from typing import Final

try:
    from zoneinfo import ZoneInfo
    IST: Final = ZoneInfo("Asia/Kolkata")
except Exception:
    IST: Final = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")

from .schemas import (
    DateResolutionResult,
    DateResolutionStatus,
)

WEEKDAY_MAP: Final[dict[str, int]] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

MONTH_MAP: Final[dict[str, int]] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9, "sept": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def get_current_date_ist() -> date:
    """Return current calendar date in Asia/Kolkata (IST)."""
    return datetime.now(IST).date()


class DateNormalizer:
    """Normalizes natural and structured date inputs to ISO dates in IST."""

    @classmethod
    def normalize(
        cls,
        raw_date: str | date | datetime | None,
        *,
        reference_date: date | None = None,
        max_booking_days: int = 120,
        enforce_booking_window: bool = False,
    ) -> DateResolutionResult:
        """Normalize a date expression.

        Args:
            raw_date: Input string, date or datetime object.
            reference_date: Optional reference date (defaults to current date in Asia/Kolkata).
            max_booking_days: Maximum advance reservation days if enforced.
            enforce_booking_window: Whether to reject dates beyond max_booking_days (default False).

        Returns:
            DateResolutionResult with status RESOLVED, PAST_DATE, OUTSIDE_WINDOW, or INVALID_FORMAT.
        """
        today = reference_date if reference_date is not None else get_current_date_ist()

        if raw_date is None:
            return DateResolutionResult(
                status=DateResolutionStatus.INVALID_FORMAT,
                error="Date query is empty.",
            )

        # 1. If already a date or datetime
        if isinstance(raw_date, datetime):
            target = raw_date.date()
            return cls._validate_target(
                target, today, max_booking_days, enforce_booking_window
            )
        if isinstance(raw_date, date):
            return cls._validate_target(
                raw_date, today, max_booking_days, enforce_booking_window
            )

        expr = str(raw_date).strip().lower()
        if not expr:
            return DateResolutionResult(
                status=DateResolutionStatus.INVALID_FORMAT,
                error="Date query is empty.",
            )

        # 2. Relative keywords
        if expr in ("today", "tonight"):
            return cls._validate_target(today, today, max_booking_days, enforce_booking_window)

        if expr == "tomorrow":
            return cls._validate_target(
                today + timedelta(days=1), today, max_booking_days, enforce_booking_window
            )

        if expr in ("day after tomorrow", "overmorrow"):
            return cls._validate_target(
                today + timedelta(days=2), today, max_booking_days, enforce_booking_window
            )

        if expr == "yesterday":
            return cls._validate_target(
                today - timedelta(days=1), today, max_booking_days, enforce_booking_window
            )

        # 3. Relative weekday handling ("this friday", "next monday", "coming saturday", "friday")
        weekday_match = re.match(
            r"^(?:(this|next|coming)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)$",
            expr,
        )
        if weekday_match:
            modifier = weekday_match.group(1) or "this"
            target_weekday = WEEKDAY_MAP[weekday_match.group(2)]
            current_weekday = today.weekday()

            days_ahead = (target_weekday - current_weekday) % 7
            if modifier == "this":
                # "this Friday": if today is Friday, it's today (0); otherwise next upcoming Friday.
                target = today + timedelta(days=days_ahead)
            elif modifier in ("next", "coming"):
                # "next Friday": if today is Friday, it's 7 days ahead; if earlier, target next week (+7 if days_ahead == 0)
                if days_ahead == 0:
                    days_ahead = 7
                target = today + timedelta(days=days_ahead)
            else:
                target = today + timedelta(days=days_ahead)

            return cls._validate_target(
                target, today, max_booking_days, enforce_booking_window
            )

        # 4. Standard ISO format: YYYY-MM-DD
        iso_match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", expr)
        if iso_match:
            try:
                y = int(iso_match.group(1))
                m = int(iso_match.group(2))
                d = int(iso_match.group(3))
                target = date(y, m, d)
                return cls._validate_target(
                    target, today, max_booking_days, enforce_booking_window
                )
            except ValueError as exc:
                return DateResolutionResult(
                    status=DateResolutionStatus.INVALID_FORMAT,
                    error=f"Invalid calendar date '{expr}': {exc}.",
                )

        # 5. DD-MM-YYYY or DD/MM/YYYY
        dmy_match = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", expr)
        if dmy_match:
            try:
                d = int(dmy_match.group(1))
                m = int(dmy_match.group(2))
                y = int(dmy_match.group(3))
                target = date(y, m, d)
                return cls._validate_target(
                    target, today, max_booking_days, enforce_booking_window
                )
            except ValueError as exc:
                return DateResolutionResult(
                    status=DateResolutionStatus.INVALID_FORMAT,
                    error=f"Invalid calendar date '{expr}': {exc}.",
                )

        # 6. Natural format: "27 Nov 2026", "27th November 2026", "Nov 27, 2026", "27 Nov"
        # Clean ordinal suffixes: 1st, 2nd, 3rd, 4th -> 1, 2, 3, 4
        clean_expr = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", expr)
        clean_expr = clean_expr.replace(",", " ")

        # Match "27 november 2026" or "27 nov"
        nat_match1 = re.match(r"^(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?$", clean_expr)
        if nat_match1:
            d = int(nat_match1.group(1))
            m_str = nat_match1.group(2)
            y_str = nat_match1.group(3)
            if m_str in MONTH_MAP:
                m = MONTH_MAP[m_str]
                y = int(y_str) if y_str else today.year
                try:
                    target = date(y, m, d)
                    # If year was omitted and target is in the past, assume next year
                    if not y_str and target < today:
                        target = date(y + 1, m, d)
                    return cls._validate_target(
                        target, today, max_booking_days, enforce_booking_window
                    )
                except ValueError as exc:
                    return DateResolutionResult(
                        status=DateResolutionStatus.INVALID_FORMAT,
                        error=f"Invalid date '{expr}': {exc}.",
                    )

        # Match "nov 27 2026" or "november 27"
        nat_match2 = re.match(r"^([a-z]+)\s+(\d{1,2})(?:\s+(\d{4}))?$", clean_expr)
        if nat_match2:
            m_str = nat_match2.group(1)
            d = int(nat_match2.group(2))
            y_str = nat_match2.group(3)
            if m_str in MONTH_MAP:
                m = MONTH_MAP[m_str]
                y = int(y_str) if y_str else today.year
                try:
                    target = date(y, m, d)
                    if not y_str and target < today:
                        target = date(y + 1, m, d)
                    return cls._validate_target(
                        target, today, max_booking_days, enforce_booking_window
                    )
                except ValueError as exc:
                    return DateResolutionResult(
                        status=DateResolutionStatus.INVALID_FORMAT,
                        error=f"Invalid date '{expr}': {exc}.",
                    )

        # Unrecognized format
        return DateResolutionResult(
            status=DateResolutionStatus.INVALID_FORMAT,
            error=f"Could not parse journey date '{raw_date}'. Use YYYY-MM-DD or relative terms like 'tomorrow'.",
        )

    @classmethod
    def _validate_target(
        cls,
        target: date,
        today: date,
        max_booking_days: int,
        enforce_booking_window: bool,
    ) -> DateResolutionResult:
        """Validate target date against today and configurable reservation window."""
        if target < today:
            return DateResolutionResult(
                status=DateResolutionStatus.PAST_DATE,
                resolved_date=target,
                iso_date=target.isoformat(),
                error=f"Journey date {target.isoformat()} is in the past (today is {today.isoformat()} in India).",
            )

        if enforce_booking_window:
            max_allowed = today + timedelta(days=max_booking_days)
            if target > max_allowed:
                return DateResolutionResult(
                    status=DateResolutionStatus.OUTSIDE_WINDOW,
                    resolved_date=target,
                    iso_date=target.isoformat(),
                    error=(
                        f"Journey date {target.isoformat()} exceeds the advance reservation "
                        f"window of {max_booking_days} days (latest booking date is {max_allowed.isoformat()})."
                    ),
                )

        return DateResolutionResult(
            status=DateResolutionStatus.RESOLVED,
            resolved_date=target,
            iso_date=target.isoformat(),
        )
