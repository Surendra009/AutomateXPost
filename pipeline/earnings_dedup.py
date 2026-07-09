"""Dedup earnings drafts by ticker — one result per symbol per report window."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlmodel import select

from database import get_session
from logging_config import setup_logging
from models import Draft, Headline

logger = setup_logging()

_EARNINGS_TICKER_DEDUP_HOURS = 24
_ACTIVE_STATUSES = ("pending", "scheduled")
_RECENT_STATUSES = ("pending", "scheduled", "posted", "approved", "rejected")
_TICKER_RE = re.compile(r"\$?([A-Z]{1,5})\b")
_EARNINGS_WORD = re.compile(r"\b(eps|earnings|revenue|quarterly|guidance|beat|miss)\b", re.I)


def tickers_from_field(tickers: str | None) -> set[str]:
    if not tickers:
        return set()
    return {t.strip().upper() for t in tickers.split(",") if t.strip()}


def primary_ticker(tickers: str | None) -> str | None:
    parsed = tickers_from_field(tickers)
    return next(iter(parsed)) if parsed else None


def extract_ticker_from_text(text: str) -> str | None:
    for match in _TICKER_RE.finditer(text or ""):
        symbol = match.group(1)
        if symbol.isalpha() and len(symbol) <= 5:
            return symbol
    return None


def earnings_ticker_blocked(ticker: str, *, hours: int | None = None, results_only: bool = False) -> bool:
    """True when this ticker already has a recent or active earnings draft.

    results_only: ignore CONTEXT/preview drafts so calendar results can replace previews.
    """
    symbol = (ticker or "").upper().strip()
    if not symbol:
        return False

    if _has_active_earnings_draft(symbol, results_only=results_only):
        return True

    hours = hours if hours is not None else _EARNINGS_TICKER_DEDUP_HOURS
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as session:
        rows = session.exec(
            select(Draft).where(
                Draft.category == "earnings",
                Draft.created_at >= cutoff,
                Draft.status.in_(_RECENT_STATUSES),
            )
        ).all()
        for draft in rows:
            if symbol not in tickers_from_field(draft.tickers):
                continue
            if results_only and draft.format == "CONTEXT":
                continue
            return True
    return False


def _has_active_earnings_draft(ticker: str, *, results_only: bool = False) -> bool:
    with get_session() as session:
        rows = session.exec(
            select(Draft).where(
                Draft.category == "earnings",
                Draft.status.in_(_ACTIVE_STATUSES),
            )
        ).all()
        for draft in rows:
            if ticker not in tickers_from_field(draft.tickers):
                continue
            if results_only and draft.format == "CONTEXT":
                continue
            return True
    return False


def expire_earnings_previews_for_ticker(ticker: str) -> int:
    """Drop pending preview drafts when actual results arrive for the same symbol."""
    symbol = (ticker or "").upper().strip()
    if not symbol:
        return 0

    expired = 0
    with get_session() as session:
        rows = session.exec(
            select(Draft).where(
                Draft.category == "earnings",
                Draft.status == "pending",
            )
        ).all()
        for draft in rows:
            if symbol not in tickers_from_field(draft.tickers):
                continue
            headline = session.get(Headline, draft.headline_id)
            if headline and "preview" in (headline.title or "").lower():
                draft.status = "stale"
                session.add(draft)
                expired += 1
            elif draft.format == "CONTEXT" and "reports" in (draft.text or "").lower():
                draft.status = "stale"
                session.add(draft)
                expired += 1
        session.commit()

    if expired:
        logger.info("Expired %d earnings preview(s) for %s", expired, symbol)
    return expired


def earnings_group_key(draft: Draft) -> str | None:
    """Queue grouping key — one visible draft per ticker for earnings."""
    if draft.category != "earnings":
        return None
    symbol = primary_ticker(draft.tickers)
    if not symbol:
        return None
    day = draft.created_at.date().isoformat() if draft.created_at else datetime.utcnow().date().isoformat()
    return f"earnings:{symbol}:{day}"


def headline_looks_like_earnings(title: str, summary: str = "") -> bool:
    return bool(_EARNINGS_WORD.search(f"{title} {summary}"))
