"""Earnings release timing — estimate when results dropped and gate stale drafts."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import MAX_EARNINGS_AGE_HOURS, PIPELINE_TIMEZONE

_ET = ZoneInfo(PIPELINE_TIMEZONE)


def estimate_earnings_release_utc(
    date_str: str,
    hour: str | None,
    *,
    has_actuals: bool = False,
) -> datetime | None:
    """Estimate earnings release time in naive UTC (matches rest of the app)."""
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None

    hour_l = (hour or "").lower()
    if hour_l == "bmo":
        local_dt = datetime.combine(event_date, datetime.min.time()).replace(
            hour=8, minute=0, tzinfo=_ET
        )
    elif hour_l == "amc":
        local_dt = datetime.combine(event_date, datetime.min.time()).replace(
            hour=16, minute=15, tzinfo=_ET
        )
    elif hour_l == "dmh":
        local_dt = datetime.combine(event_date, datetime.min.time()).replace(
            hour=12, minute=0, tzinfo=_ET
        )
    elif has_actuals:
        # Results without a timing tag — assume after the close.
        local_dt = datetime.combine(event_date, datetime.min.time()).replace(
            hour=16, minute=15, tzinfo=_ET
        )
    else:
        local_dt = datetime.combine(event_date, datetime.min.time()).replace(
            hour=16, minute=15, tzinfo=_ET
        )

    return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def earnings_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(hours=MAX_EARNINGS_AGE_HOURS)


def is_earnings_fresh(published_at: datetime) -> bool:
    return published_at >= earnings_cutoff()


def earnings_age_hours(published_at: datetime) -> float:
    return max(0.0, (datetime.utcnow() - published_at).total_seconds() / 3600)
