"""Build the Intent Board — what this cycle must try to cover.

Step 1: seed from Finnhub earnings + economic calendars (earnings prints,
macro prints, FOMC/Fed decisions). Optional standing Fed-news intent so
Fed journalism is never absent from the board on quiet calendar days.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from config import EARNINGS_PREVIEW_DAYS_FORWARD
from database import get_setting
from logging_config import setup_logging
from pipeline.earnings import _has_actual, fetch_earnings_calendar
from pipeline.earnings_calendar import dedupe_calendar_events
from pipeline.earnings_freshness import coerce_quarter_year, is_current_reporting_period
from pipeline.finnhub_api import get_finnhub_key
from pipeline.macro_calendar import (
    _impact_ok,
    _is_us_event,
    _macro_label,
    fetch_economic_calendar,
)
from pipeline.v2.types import Intent, IntentKind
from pipeline.watchlist_scope import in_watchlist, normalized_watchlist

logger = setup_logging()

# Caps keep the board focused; evidence/verify come in later steps.
_MAX_EARNINGS_INTENTS = 12
_MAX_MACRO_INTENTS = 8


def build_intent_board() -> list[Intent]:
    """Return must-cover intents for this cycle."""
    intents: list[Intent] = []
    if not get_finnhub_key():
        logger.info("v2 intent board: Finnhub not configured — empty board")
        return intents

    today = datetime.utcnow().date()
    intents.extend(_seed_earnings_intents(today))
    intents.extend(_seed_macro_intents(today))
    intents.extend(_seed_standing_fed_intent(today, intents))

    # Stable order: earnings → macro/fed calendar → standing fed
    logger.info(
        "v2 intent board: %d intents (earnings=%d macro=%d fed=%d)",
        len(intents),
        sum(1 for i in intents if i.kind == "earnings_print"),
        sum(1 for i in intents if i.kind == "macro_print"),
        sum(1 for i in intents if i.kind in ("fed_decision", "fed_speak")),
    )
    return intents


def _seed_earnings_intents(today) -> list[Intent]:
    watchlist = normalized_watchlist(get_setting("watchlist", []))
    has_watchlist = bool(watchlist)
    yesterday = today - timedelta(days=1)
    preview_end = today + timedelta(days=EARNINGS_PREVIEW_DAYS_FORWARD)

    events = dedupe_calendar_events(
        fetch_earnings_calendar(yesterday.isoformat(), preview_end.isoformat())
    )
    out: list[Intent] = []
    seen: set[str] = set()

    for event in events:
        if len(out) >= _MAX_EARNINGS_INTENTS:
            break

        symbol = (event.get("symbol") or "").upper().strip()
        if not symbol:
            continue

        on_watchlist = has_watchlist and in_watchlist(symbol, watchlist)
        if has_watchlist and not on_watchlist:
            continue

        date_str = (event.get("date") or "")[:10]
        quarter, year = coerce_quarter_year(event.get("quarter"), event.get("year"))
        has_print = _has_actual(event)

        # Without a watchlist, only prioritize names that already printed.
        if not has_watchlist and not has_print:
            continue

        if quarter and year:
            if not is_current_reporting_period(quarter, year, as_of=today, require_period=True):
                continue
        elif has_print:
            # Printed but missing Q/Y — still seed so verify can drop later;
            # avoids silently missing a watchlist name.
            if not on_watchlist and has_watchlist:
                continue
        else:
            # Preview without period — skip (can't target the right print)
            continue

        period = f"Q{quarter}-{year}" if quarter and year else None
        intent_id = f"earnings:{symbol}:{date_str}:{period or 'unknown'}"
        if intent_id in seen:
            continue
        seen.add(intent_id)

        status = "reported" if has_print else "preview"
        queries = _earnings_queries(symbol, quarter, year)
        out.append(
            Intent(
                kind="earnings_print",
                id=intent_id,
                tickers=[symbol],
                period=period,
                label=symbol,
                window_hours=8.0 if has_print else 24.0,
                queries=queries,
                metadata={
                    "date": date_str,
                    "hour": event.get("hour"),
                    "status": status,
                    "on_watchlist": on_watchlist,
                    "quarter": quarter,
                    "year": year,
                    "eps_actual": event.get("epsActual"),
                    "eps_estimate": event.get("epsEstimate"),
                    "revenue_actual": event.get("revenueActual"),
                    "revenue_estimate": event.get("revenueEstimate"),
                },
            )
        )

    return out


def _earnings_queries(symbol: str, quarter: int | None, year: int | None) -> list[str]:
    period_bits: list[str] = []
    if quarter and year:
        period_bits.append(f"Q{quarter} {year}")
        period_bits.append(f"Q{quarter}")
        word = {1: "first", 2: "second", 3: "third", 4: "fourth"}.get(quarter)
        if word:
            period_bits.append(f'"{word} quarter" {year}')
    period_clause = " OR ".join(period_bits) if period_bits else "earnings"
    return [
        f'"{symbol}" ({period_clause}) (earnings OR results) (EPS OR revenue)',
        f'"{symbol}" earnings (beat OR miss OR results) -preview -scheduled',
    ]


def _seed_macro_intents(today) -> list[Intent]:
    yesterday = today - timedelta(days=1)
    events = fetch_economic_calendar(yesterday.isoformat(), today.isoformat())
    out: list[Intent] = []
    seen: set[str] = set()

    for event in events:
        if len(out) >= _MAX_MACRO_INTENTS:
            break
        if not _is_us_event(event) or not _impact_ok(event):
            continue

        event_name = event.get("event") or ""
        label = _macro_label(event_name)
        if not label:
            continue

        date_str = (event.get("date") or today.isoformat())[:10]
        kind: IntentKind = "fed_decision" if label == "Fed" else "macro_print"
        has_actual = event.get("actual") is not None
        status = "reported" if has_actual else "preview"

        intent_id = f"macro:{label}:{date_str}:{status}"
        if intent_id in seen:
            continue
        seen.add(intent_id)

        out.append(
            Intent(
                kind=kind,
                id=intent_id,
                tickers=["SPY", "TLT", "QQQ"],
                period=None,
                label=label,
                window_hours=12.0,
                queries=_macro_queries(label, kind),
                metadata={
                    "date": date_str,
                    "time": event.get("time"),
                    "status": status,
                    "event_name": event_name,
                    "actual": event.get("actual"),
                    "estimate": event.get("estimate"),
                    "unit": event.get("unit"),
                    "impact": event.get("impact"),
                },
            )
        )

    return out


def _macro_queries(label: str, kind: IntentKind) -> list[str]:
    if kind == "fed_decision" or label == "Fed":
        return [
            'FOMC OR "Federal Reserve" (decision OR statement OR rate OR rates) when:1d',
            'Powell OR FOMC (hold OR cut OR hike OR "basis points") when:1d',
        ]
    mapping = {
        "CPI": 'CPI OR "consumer price index" (inflation OR beats OR misses OR rises) when:1d',
        "PPI": 'PPI OR "producer price index" (inflation OR rises OR falls) when:1d',
        "Nonfarm payrolls": '("nonfarm payrolls" OR NFP OR payrolls) (jobs OR employment) when:1d',
        "Unemployment": '("unemployment rate") (jobs OR labor) when:1d',
        "GDP": 'GDP OR "gross domestic product" (growth OR economy) when:1d',
        "Retail sales": '("retail sales") (consumer OR spending) when:1d',
        "Jobless claims": '("jobless claims" OR "initial claims") when:1d',
    }
    primary = mapping.get(label, f'"{label}" economy OR markets when:1d')
    return [primary, f'"{label}" (beats OR misses OR vs OR estimate) when:1d']


def _seed_standing_fed_intent(today, existing: list[Intent]) -> list[Intent]:
    """Ensure Fed coverage appears on the board even without a calendar FOMC row."""
    if any(i.kind in ("fed_decision", "fed_speak") for i in existing):
        return []
    date_str = today.isoformat()
    return [
        Intent(
            kind="fed_speak",
            id=f"fed:standing:{date_str}",
            tickers=["SPY", "TLT", "QQQ"],
            label="Fed",
            window_hours=12.0,
            queries=[
                'Powell OR "Federal Reserve" OR FOMC (speech OR speaks OR testimony OR minutes) when:1d',
                '"Federal Reserve" (rates OR inflation OR "policy") when:1d',
            ],
            metadata={"date": date_str, "status": "standing", "standing": True},
        )
    ]
