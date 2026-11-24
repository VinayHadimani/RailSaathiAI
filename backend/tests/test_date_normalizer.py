"""Unit tests for DateNormalizer (Phase 6B.2)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from app.agent.date_normalizer import (
    IST,
    DateNormalizer,
    get_current_date_ist,
)
from app.agent.schemas import DateResolutionStatus


def test_timezone_is_asia_kolkata():
    """Verify that current date anchor uses Asia/Kolkata timezone."""
    now_ist = datetime.now(IST)
    today_ist = get_current_date_ist()
    assert today_ist == now_ist.date()


def test_today_tomorrow_day_after():
    """Deterministic relative keywords resolve correctly relative to reference date."""
    ref = date(2026, 11, 24)

    res_today = DateNormalizer.normalize("today", reference_date=ref)
    assert res_today.status == DateResolutionStatus.RESOLVED
    assert res_today.resolved_date == date(2026, 11, 24)
    assert res_today.iso_date == "2026-11-24"

    res_tomorrow = DateNormalizer.normalize("tomorrow", reference_date=ref)
    assert res_tomorrow.status == DateResolutionStatus.RESOLVED
    assert res_tomorrow.resolved_date == date(2026, 11, 25)
    assert res_tomorrow.iso_date == "2026-11-25"

    res_day_after = DateNormalizer.normalize("day after tomorrow", reference_date=ref)
    assert res_day_after.status == DateResolutionStatus.RESOLVED
    assert res_day_after.resolved_date == date(2026, 11, 26)
    assert res_day_after.iso_date == "2026-11-26"


def test_past_date_rejection():
    """Dates in the past are rejected with PAST_DATE."""
    ref = date(2026, 11, 24)

    # Yesterday keyword
    res_yesterday = DateNormalizer.normalize("yesterday", reference_date=ref)
    assert res_yesterday.status == DateResolutionStatus.PAST_DATE
    assert res_yesterday.resolved_date == date(2026, 11, 23)
    assert "in the past" in res_yesterday.error

    # Explicit past date
    res_past = DateNormalizer.normalize("2026-11-20", reference_date=ref)
    assert res_past.status == DateResolutionStatus.PAST_DATE
    assert res_past.resolved_date == date(2026, 11, 20)

    # Distant past
    res_old = DateNormalizer.normalize("2020-01-01", reference_date=ref)
    assert res_old.status == DateResolutionStatus.PAST_DATE


def test_absolute_date_formats():
    """Various date formats parse correctly."""
    ref = date(2026, 11, 24)

    formats = [
        ("2026-11-28", date(2026, 11, 28)),
        ("2026/11/28", date(2026, 11, 28)),
        ("28-11-2026", date(2026, 11, 28)),
        ("28/11/2026", date(2026, 11, 28)),
        ("28 Nov 2026", date(2026, 11, 28)),
        ("28th November 2026", date(2026, 11, 28)),
        ("November 28 2026", date(2026, 11, 28)),
    ]
    for expr, expected in formats:
        res = DateNormalizer.normalize(expr, reference_date=ref)
        assert res.status == DateResolutionStatus.RESOLVED, f"Failed for {expr}"
        assert res.resolved_date == expected
        assert res.iso_date == expected.isoformat()


def test_relative_weekday_resolution():
    """Relative weekdays ('this friday', 'next monday') resolve deterministically."""
    # 2026-11-24 is a Tuesday (weekday 1)
    ref = date(2026, 11, 24)

    # This Friday is 2026-11-27 (+3 days)
    res_fri = DateNormalizer.normalize("this friday", reference_date=ref)
    assert res_fri.status == DateResolutionStatus.RESOLVED
    assert res_fri.resolved_date == date(2026, 11, 27)

    # Next Monday is 2026-11-30 (+6 days)
    res_mon = DateNormalizer.normalize("next monday", reference_date=ref)
    assert res_mon.status == DateResolutionStatus.RESOLVED
    assert res_mon.resolved_date == date(2026, 11, 30)


def test_month_and_year_boundaries():
    """Roll over across month and year boundaries correctly."""
    # End of month boundary: Nov 30 -> Dec 1
    ref_nov30 = date(2026, 11, 30)
    res_dec = DateNormalizer.normalize("tomorrow", reference_date=ref_nov30)
    assert res_dec.status == DateResolutionStatus.RESOLVED
    assert res_dec.resolved_date == date(2026, 12, 1)

    # End of year boundary: Dec 31 -> Jan 1 next year
    ref_dec31 = date(2026, 12, 31)
    res_jan1 = DateNormalizer.normalize("tomorrow", reference_date=ref_dec31)
    assert res_jan1.status == DateResolutionStatus.RESOLVED
    assert res_jan1.resolved_date == date(2027, 1, 1)
    assert res_jan1.iso_date == "2027-01-01"


def test_booking_window_disabled_by_default():
    """By default, booking window is non-enforced (allows distant future dates)."""
    ref = date(2026, 11, 24)
    # +150 days in the future (beyond 120 days)
    distant_date = ref + timedelta(days=150)
    res = DateNormalizer.normalize(
        distant_date.isoformat(),
        reference_date=ref,
        max_booking_days=120,
        enforce_booking_window=False,  # default
    )
    assert res.status == DateResolutionStatus.RESOLVED
    assert res.resolved_date == distant_date


def test_booking_window_enforced_when_requested():
    """When explicitly enforced, dates beyond max_booking_days are flagged OUTSIDE_WINDOW."""
    ref = date(2026, 11, 24)
    distant_date = ref + timedelta(days=150)
    res = DateNormalizer.normalize(
        distant_date.isoformat(),
        reference_date=ref,
        max_booking_days=120,
        enforce_booking_window=True,
    )
    assert res.status == DateResolutionStatus.OUTSIDE_WINDOW
    assert "exceeds the advance reservation window" in res.error


def test_invalid_date_formats():
    """Malformed dates return INVALID_FORMAT."""
    ref = date(2026, 11, 24)
    invalids = ["not-a-date", "2026-02-31", "32/13/2026", "2026-99-99", "random text", ""]
    for expr in invalids:
        res = DateNormalizer.normalize(expr, reference_date=ref)
        assert res.status == DateResolutionStatus.INVALID_FORMAT, f"Expected invalid format for {expr}"
        assert res.resolved_date is None
        assert res.error is not None
