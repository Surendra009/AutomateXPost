"""Earnings release timing — estimate when results dropped and gate stale drafts."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config import MAX_EARNINGS_AGE_HOURS, PIPELINE_TIMEZONE

_ET = ZoneInfo(PIPELINE_TIMEZONE)

_QUARTER_LABEL = re.compile(r"\bQ([1-4])\b", re.I)
_YEAR_LABEL = re.compile(r"\b(20\d{2})\b")
# True retrospective / roundup copy — NOT ordinary "year over year" growth language
_STALE_EARNINGS_COPY = re.compile(
    r"\b("
    r"last quarter|previous quarter|prior quarter|earlier quarter|"
    r"a year ago|year[- ]ago|"
    r"recap|look back|revisited|roundup|"
    r"already reported|had reported|previously reported|"
    r"earnings season wrap|earnings roundup|"
    r"from last (?:quarter|year)|reported last (?:quarter|year)"
    r")\b",
    re.I,
)


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


def expected_reporting_quarters(as_of: date | None = None) -> set[tuple[int, int]]:
    """Fiscal (quarter, year) pairs that are in-season around a calendar date."""
    as_of = as_of or datetime.utcnow().date()
    month, year = as_of.month, as_of.year

    if month in (1, 2):
        return {(4, year - 1)}
    if month == 3:
        return {(4, year - 1), (1, year)}
    if month in (4, 5):
        return {(1, year)}
    if month == 6:
        return {(1, year), (2, year)}
    if month in (7, 8):
        return {(2, year)}
    if month == 9:
        return {(2, year), (3, year)}
    if month in (10, 11):
        return {(3, year)}
    return {(3, year), (4, year)}


def coerce_quarter_year(
    quarter: Any = None,
    year: Any = None,
) -> tuple[int | None, int | None]:
    """Normalize Finnhub quarter/year fields to ints."""
    q_out: int | None = None
    y_out: int | None = None
    try:
        if quarter not in (None, "", 0, "0"):
            q_out = int(quarter)
            if q_out < 1 or q_out > 4:
                q_out = None
    except (TypeError, ValueError):
        q_out = None
    try:
        if year not in (None, "", 0, "0"):
            y_out = int(year)
            if y_out < 2000 or y_out > 2100:
                y_out = None
    except (TypeError, ValueError):
        y_out = None
    return q_out, y_out


def is_current_reporting_period(
    quarter: int | None,
    year: int | None,
    *,
    as_of: date | None = None,
    require_period: bool = False,
) -> bool:
    """True when quarter/year matches the active earnings season.

    When ``require_period`` is True (Finnhub calendar rows), missing Q/Y is rejected
    instead of treated as current — unless the caller also checks event date freshness.
    """
    if not quarter or quarter < 1 or quarter > 4:
        return not require_period
    if not year:
        return not require_period
    return (int(quarter), int(year)) in expected_reporting_quarters(as_of)


def _year_near_quarter(text: str, quarter_match: re.Match[str]) -> int | None:
    start = max(0, quarter_match.start() - 12)
    end = min(len(text), quarter_match.end() + 12)
    window = text[start:end]
    year_match = _YEAR_LABEL.search(window)
    if year_match:
        return int(year_match.group(1))
    return None


def parse_quarter_year_from_text(text: str) -> tuple[int | None, int | None]:
    """Extract Q# and optional fiscal year from headline or summary."""
    blob = text or ""
    q_match = _QUARTER_LABEL.search(blob)
    if not q_match:
        return None, None
    quarter = int(q_match.group(1))
    year = _year_near_quarter(blob, q_match)
    if year is None:
        years = [int(m.group(1)) for m in _YEAR_LABEL.finditer(blob)]
        if years:
            year = years[0]
    return quarter, year


def _all_quarter_year_mentions(text: str) -> list[tuple[int, int | None]]:
    out: list[tuple[int, int | None]] = []
    for match in _QUARTER_LABEL.finditer(text or ""):
        out.append((int(match.group(1)), _year_near_quarter(text, match)))
    return out


def earnings_period_is_stale(
    text: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
    as_of: date | None = None,
) -> bool:
    """True when copy references a prior-quarter earnings report."""
    as_of = as_of or datetime.utcnow().date()
    blob = text or ""

    if _STALE_EARNINGS_COPY.search(blob):
        return True

    parsed_q, parsed_y = parse_quarter_year_from_text(blob)
    quarter = quarter if quarter is not None else parsed_q
    year = year if year is not None else parsed_y

    # Text that only names stale quarters (e.g. "Q1 2026" in July) is prior-period
    mentions = _all_quarter_year_mentions(blob)
    if mentions and quarter is None:
        current = expected_reporting_quarters(as_of)
        resolved: list[tuple[int, int]] = []
        for q, y in mentions:
            if y is None:
                for cq, cy in current:
                    if cq == q:
                        y = cy
                        break
                if y is None:
                    y = as_of.year - 1 if q == 4 and as_of.month <= 3 else as_of.year
            resolved.append((q, int(y)))
        if resolved and not any((q, y) in current for q, y in resolved):
            return True

    if quarter is None:
        return False

    if year is None:
        for q, y in expected_reporting_quarters(as_of):
            if q == quarter:
                year = y
                break
        if year is None:
            if quarter == 4 and as_of.month <= 3:
                year = as_of.year - 1
            else:
                year = as_of.year

    return not is_current_reporting_period(quarter, year, as_of=as_of)
