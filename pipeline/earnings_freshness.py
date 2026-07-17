"""Earnings release timing — estimate when results dropped and gate stale drafts."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config import MAX_EARNINGS_AGE_HOURS, PIPELINE_TIMEZONE

_ET = ZoneInfo(PIPELINE_TIMEZONE)

_QUARTER_LABEL = re.compile(r"\bQ([1-4])\b", re.I)
_QUARTER_WORD = re.compile(
    r"\b(first|second|third|fourth|1st|2nd|3rd|4th)(?:\s*|-)?quarters?\b",
    re.I,
)
_QUARTER_WORD_MAP = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
}
_YEAR_LABEL = re.compile(r"\b(20\d{2})\b")
_FY_YEAR = re.compile(r"\bFY\s*(20\d{2})\b", re.I)
# True retrospective / roundup copy — NOT ordinary "year over year" growth language
_STALE_EARNINGS_COPY = re.compile(
    r"\b("
    r"last quarter|previous quarter|prior quarter|earlier quarter|"
    # Avoid bare "last year" — earnings copy often says "up vs last year" for current results
    r"last year'?s? (?:q[1-4]|quarter)|(?:q[1-4]|quarter) last year|"
    r"previous year'?s? (?:q[1-4]|quarter)|(?:q[1-4]|quarter) (?:previous|prior) year|"
    r"a year ago|year[- ]ago earnings|"
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
    """True when quarter/year matches the active earnings season."""
    if not quarter or quarter < 1 or quarter > 4:
        return not require_period
    if not year:
        return not require_period
    return (int(quarter), int(year)) in expected_reporting_quarters(as_of)


def _year_near_span(text: str, start: int, end: int, *, pad: int = 24) -> int | None:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    window = text[lo:hi]
    fy = _FY_YEAR.search(window)
    if fy:
        return int(fy.group(1))
    year_match = _YEAR_LABEL.search(window)
    if year_match:
        return int(year_match.group(1))
    return None


def _iter_quarter_spans(text: str) -> list[tuple[int, int, int]]:
    """Return (quarter, start, end) spans for Q# and 'fourth quarter' forms."""
    spans: list[tuple[int, int, int]] = []
    for match in _QUARTER_LABEL.finditer(text or ""):
        spans.append((int(match.group(1)), match.start(), match.end()))
    for match in _QUARTER_WORD.finditer(text or ""):
        word = match.group(1).lower()
        q = _QUARTER_WORD_MAP.get(word)
        if q:
            spans.append((q, match.start(), match.end()))
    spans.sort(key=lambda item: item[1])
    return spans


def parse_quarter_year_from_text(text: str) -> tuple[int | None, int | None]:
    """Extract Q# / 'fourth quarter' and optional year from headline or summary."""
    blob = text or ""
    spans = _iter_quarter_spans(blob)
    if not spans:
        return None, None
    quarter, start, end = spans[0]
    year = _year_near_span(blob, start, end)
    if year is None:
        fy = _FY_YEAR.search(blob)
        if fy:
            year = int(fy.group(1))
        else:
            years = [int(m.group(1)) for m in _YEAR_LABEL.finditer(blob)]
            if years:
                # Prefer a year adjacent to the quarter mention; else first year in text
                year = years[0]
    return quarter, year


def _all_quarter_year_mentions(text: str) -> list[tuple[int, int | None]]:
    out: list[tuple[int, int | None]] = []
    for quarter, start, end in _iter_quarter_spans(text or ""):
        out.append((quarter, _year_near_span(text, start, end)))
    return out


def resolve_period_year(
    quarter: int,
    year: int | None,
    *,
    as_of: date | None = None,
) -> int:
    """Fill missing year for a quarter using the active season when possible."""
    as_of = as_of or datetime.utcnow().date()
    if year is not None:
        return int(year)
    for q, y in expected_reporting_quarters(as_of):
        if q == quarter:
            return y
    if quarter == 4 and as_of.month <= 3:
        return as_of.year - 1
    return as_of.year


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

    current = expected_reporting_quarters(as_of)
    mentions = _all_quarter_year_mentions(blob)
    if mentions:
        resolved = [(q, resolve_period_year(q, y, as_of=as_of)) for q, y in mentions]
        # Any off-season period with no in-season period → stale (blocks Q4 2025 in July)
        if not any((q, y) in current for q, y in resolved):
            return True

    if quarter is None:
        return False

    year = resolve_period_year(quarter, year, as_of=as_of)
    return not is_current_reporting_period(quarter, year, as_of=as_of)


def earnings_draft_period_allowed(
    text: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
    as_of: date | None = None,
) -> bool:
    """Stricter gate for creating earnings drafts from news/templates.

    Calendar events already carry Finnhub Q/Y. News-derived drafts must not
    invent or reprint an off-season period (e.g. NFLX Q4 last year in July).
    """
    as_of = as_of or datetime.utcnow().date()
    if earnings_period_is_stale(text, quarter=quarter, year=year, as_of=as_of):
        return False

    parsed_q, parsed_y = parse_quarter_year_from_text(text)
    q = quarter if quarter is not None else parsed_q
    y = year if year is not None else parsed_y
    if q is None:
        # Unlabeled — allow only if body doesn't name any off-season quarter
        return not earnings_period_is_stale(text, as_of=as_of)

    y = resolve_period_year(q, y, as_of=as_of)
    return is_current_reporting_period(q, y, as_of=as_of, require_period=True)
