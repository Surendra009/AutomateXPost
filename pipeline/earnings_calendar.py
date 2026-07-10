"""Earnings calendar helpers — snapshots for chat, settings, and pipeline status."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from config import EARNINGS_PREVIEW_DAYS_FORWARD
from database import get_setting
from pipeline.earnings import (
    EARNINGS_SOURCE,
    _fmt_eps,
    _fmt_money,
    _has_actual,
    _hour_short,
    _surprise_word,
    fetch_earnings_calendar,
)
from pipeline.finnhub_api import get_finnhub_key
from pipeline.watchlist_scope import in_watchlist, normalized_watchlist


def dedupe_calendar_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for ev in events:
        key = f"{ev.get('symbol')}|{ev.get('date')}|{ev.get('quarter')}|{ev.get('year')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(ev)
    return unique


def _event_status(event: dict[str, Any]) -> str:
    if _has_actual(event):
        eps_word = _surprise_word(event.get("epsActual"), event.get("epsEstimate"))
        rev_word = _surprise_word(event.get("revenueActual"), event.get("revenueEstimate"), tol=0.005)
        return eps_word or rev_word or "reported"
    return "preview"


def format_calendar_event(event: dict[str, Any], *, on_watchlist: bool = False) -> dict[str, Any]:
    symbol = (event.get("symbol") or "").upper()
    date_str = event.get("date") or ""
    hour = _hour_short(event.get("hour")) or (event.get("hour") or "").upper()
    status = _event_status(event)
    eps_est = _fmt_eps(event.get("epsEstimate"))
    eps_actual = _fmt_eps(event.get("epsActual"))
    rev_est = _fmt_money(event.get("revenueEstimate"))
    rev_actual = _fmt_money(event.get("revenueActual"))

    label_parts = [symbol]
    if hour:
        label_parts.append(hour)
    if status == "preview" and eps_est:
        label_parts.append(f"EPS est {eps_est}")
    elif status in ("beat", "miss", "in-line", "reported"):
        if eps_actual and eps_est:
            label_parts.append(f"EPS {eps_actual} vs {eps_est}")
        label_parts.append(status)

    return {
        "symbol": symbol,
        "date": date_str,
        "hour": hour,
        "quarter": event.get("quarter"),
        "year": event.get("year"),
        "status": status,
        "on_watchlist": on_watchlist,
        "eps_estimate": eps_est,
        "eps_actual": eps_actual,
        "revenue_estimate": rev_est,
        "revenue_actual": rev_actual,
        "label": " · ".join(label_parts),
        "source": EARNINGS_SOURCE,
    }


def get_earnings_snapshot(
    *,
    watchlist: list[str] | None = None,
    query_symbols: list[str] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Finnhub earnings rows for chat and diagnostics."""
    if not get_finnhub_key():
        return []

    watchlist = normalized_watchlist(watchlist if watchlist is not None else get_setting("watchlist", []))
    query_symbols = [s.upper() for s in (query_symbols or []) if s]

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=1)).isoformat()
    to_date = (today + timedelta(days=EARNINGS_PREVIEW_DAYS_FORWARD)).isoformat()
    events = dedupe_calendar_events(fetch_earnings_calendar(from_date, to_date))

    rows: list[dict[str, Any]] = []
    for event in events:
        symbol = (event.get("symbol") or "").upper()
        if not symbol:
            continue

        on_watchlist = bool(watchlist) and in_watchlist(symbol, watchlist)
        if query_symbols and symbol not in query_symbols:
            continue
        if watchlist and not on_watchlist and not query_symbols:
            continue
        if not watchlist and not query_symbols:
            if not _has_actual(event):
                continue
            eps_word = _surprise_word(event.get("epsActual"), event.get("epsEstimate"))
            rev_word = _surprise_word(
                event.get("revenueActual"), event.get("revenueEstimate"), tol=0.005
            )
            if eps_word not in ("beat", "miss") and rev_word not in ("beat", "miss"):
                continue

        try:
            event_date = datetime.strptime(event.get("date") or "", "%Y-%m-%d").date()
        except ValueError:
            continue

        if _has_actual(event):
            pass
        elif event_date < today or event_date > today + timedelta(days=EARNINGS_PREVIEW_DAYS_FORWARD):
            continue

        rows.append(format_calendar_event(event, on_watchlist=on_watchlist))

    rows.sort(key=lambda r: (r["date"], r["symbol"]))
    return rows[:limit]


def get_earnings_pipeline_summary() -> dict[str, Any]:
    """Lightweight earnings status for the settings / pipeline panel."""
    watchlist = normalized_watchlist(get_setting("watchlist", []))
    if not get_finnhub_key():
        return {
            "configured": False,
            "watchlist_count": len(watchlist),
            "reporting_today": 0,
            "upcoming": [],
            "hint": "Set FINNHUB_KEY for structured earnings drafts and calendar",
        }

    today = datetime.utcnow().date().isoformat()
    snapshot = get_earnings_snapshot(watchlist=watchlist, limit=20)
    today_rows = [r for r in snapshot if r["date"] == today]
    upcoming = [r for r in snapshot if r["status"] == "preview"][:6]

    hint = None
    if not watchlist:
        hint = "Add tickers to your watchlist for earnings previews (beat/miss still drafts without one)"
    elif not today_rows and not upcoming:
        hint = "No watchlist tickers reporting earnings in the next few days"

    return {
        "configured": True,
        "watchlist_count": len(watchlist),
        "reporting_today": len(today_rows),
        "upcoming": upcoming,
        "today": today_rows[:6],
        "hint": hint,
    }
