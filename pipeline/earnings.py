"""Finnhub earnings calendar — previews (BMO/AMC) and actual vs estimate results."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlmodel import select

from config import (
    EARNINGS_PREVIEW_DAYS_FORWARD,
    MAX_EARNINGS_DRAFTS_PER_CYCLE,
    MAX_MARKET_EARNINGS_DRAFTS_PER_CYCLE,
)
from pipeline.dedup import was_recently_drafted
from pipeline.draft_budget import DraftBudget
from pipeline.earnings_dedup import earnings_ticker_blocked, expire_earnings_previews_for_ticker
from pipeline.earnings_freshness import estimate_earnings_release_utc, is_earnings_fresh
from pipeline.earnings_enrich import enrich_earnings_context
from pipeline.earnings_parse import (
    EarningsFacts,
    build_earnings_lines,
)
from pipeline.finnhub_api import finnhub_get, get_finnhub_key
from database import get_session, get_setting
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.story_key import title_fingerprint
from pipeline.watchlist_scope import in_watchlist, normalized_watchlist

logger = setup_logging()

EARNINGS_SOURCE = "Finnhub Earnings"


def _hour_label(hour: str | None) -> str:
    mapping = {
        "bmo": "before market open",
        "amc": "after market close",
        "dmh": "during market hours",
    }
    return mapping.get((hour or "").lower(), "today")


def _hour_short(hour: str | None) -> str:
    mapping = {"bmo": "BMO", "amc": "AMC", "dmh": "DMH"}
    return mapping.get((hour or "").lower(), "")


def _fmt_money(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    return f"${value:,.0f}"


def _fmt_eps(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return None


def _surprise_word(actual: float | None, estimate: float | None, tol: float = 0.01) -> str | None:
    if actual is None or estimate is None:
        return None
    try:
        diff = float(actual) - float(estimate)
    except (TypeError, ValueError):
        return None
    if abs(diff) <= tol:
        return "in-line"
    return "beat" if diff > 0 else "miss"


def _event_hash(symbol: str, date: str, quarter: int, year: int, kind: str) -> str:
    raw = f"{symbol.upper()}|{date}|Q{quarter}|{year}|{kind}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _event_url(symbol: str, date: str) -> str:
    return f"https://finnhub.io/earnings/{symbol.upper()}/{date}"


def _has_actual(event: dict[str, Any]) -> bool:
    eps_actual = event.get("epsActual")
    rev_actual = event.get("revenueActual")
    return eps_actual is not None or rev_actual is not None


def fetch_earnings_calendar(from_date: str, to_date: str) -> list[dict[str, Any]]:
    if not get_finnhub_key():
        return []
    data, err = finnhub_get("calendar/earnings", {"from": from_date, "to": to_date})
    if err:
        logger.warning("Finnhub earnings calendar: %s", err)
        return []
    return data.get("earningsCalendar") or []


def _build_preview(event: dict[str, Any]) -> tuple[str, str, str] | None:
    symbol = (event.get("symbol") or "").upper()
    if not symbol:
        return None

    hour = event.get("hour")
    timing = _hour_label(hour)
    hour_tag = _hour_short(hour)
    eps_est = _fmt_eps(event.get("epsEstimate"))
    rev_est = _fmt_money(event.get("revenueEstimate"))
    quarter = event.get("quarter")
    year = event.get("year")

    parts = [f"EPS est {eps_est}" if eps_est else None, f"revenue est {rev_est}" if rev_est else None]
    est_line = " · ".join(p for p in parts if p)
    if not est_line:
        return None

    q_label = f"Q{quarter} " if quarter else ""
    title = f"{symbol} reports {timing} — {est_line}"
    summary = (
        f"{symbol} {q_label}{year or ''} earnings preview ({hour_tag or timing}). "
        f"Estimate: {est_line}."
    ).strip()

    line1 = f"{symbol} reports {hour_tag} today" if hour_tag else f"{symbol} reports {timing} today"
    line2 = est_line
    line3 = "Watch segment commentary, margins, and full-year guide on the call"
    draft = f"{line1}\n{line2}\n{line3}\n\n${symbol}"
    return title, summary, draft


def _build_results(event: dict[str, Any]) -> tuple[str, str, str, str] | None:
    symbol = (event.get("symbol") or "").upper()
    if not symbol:
        return None

    eps_actual = event.get("epsActual")
    eps_est = event.get("epsEstimate")
    rev_actual = event.get("revenueActual")
    rev_est = event.get("revenueEstimate")

    eps_word = _surprise_word(eps_actual, eps_est)
    rev_word = _surprise_word(rev_actual, rev_est, tol=0.005)

    if not eps_word and not rev_word:
        return None

    quarter = event.get("quarter")
    year = event.get("year")
    hour = _hour_short(event.get("hour")) or _hour_label(event.get("hour"))
    q_label = f"Q{quarter} " if quarter else ""

    # Headline
    eps_actual_s = _fmt_eps(eps_actual)
    eps_est_s = _fmt_eps(eps_est)
    rev_actual_s = _fmt_money(rev_actual)
    rev_est_s = _fmt_money(rev_est)

    headline_bits = []
    if eps_word and eps_actual_s and eps_est_s:
        headline_bits.append(f"EPS {eps_actual_s} vs {eps_est_s} est ({eps_word})")
    if rev_word and rev_actual_s and rev_est_s:
        headline_bits.append(f"revenue {rev_actual_s} vs {rev_est_s} est ({rev_word})")

    if not headline_bits:
        return None

    overall = eps_word or rev_word or "reports"
    title = f"{symbol} {overall}s {q_label}earnings — {', '.join(headline_bits)} ({hour})"
    summary = title

    verb = {"beat": "beat", "miss": "missed", "in-line": "matched"}.get(
        eps_word or rev_word or "in-line", "matched"
    )
    facts = EarningsFacts(
        quarter=f"Q{quarter}" if quarter else None,
        eps_actual=eps_actual_s,
        eps_estimate=eps_est_s,
        revenue_actual=rev_actual_s,
        revenue_estimate=rev_est_s,
    )
    enrichment = enrich_earnings_context(
        symbol,
        quarter=quarter,
        year=year,
        finnhub_facts=facts,
        finnhub_summary=summary,
    )
    facts = enrichment.facts or facts
    lines = build_earnings_lines(
        symbol,
        verb,
        facts,
        source_text=enrichment.news_context or summary,
        article_text=enrichment.article_text,
        allow_llm=False,
    )
    if lines:
        line1, line2, line3 = lines
        draft = f"{line1}\n{line2}\n{line3}\n\n${symbol}"
    else:
        line1 = f"{symbol} {verb} {q_label}EPS {eps_actual_s} vs {eps_est_s} est"
        line2 = (
            f"Revenue {rev_actual_s} vs {rev_est_s} est"
            if rev_actual_s and rev_est_s
            else "Segment detail on the call"
        )
        line3 = "Guidance and margins set the next move"
        draft = f"{line1}\n{line2}\n{line3}\n\n${symbol}"

    impact = "high" if overall in ("beat", "miss") else "med"
    return title, summary, draft, impact


def _headline_exists(content_hash: str) -> bool:
    with get_session() as session:
        row = session.exec(select(Headline).where(Headline.hash == content_hash)).first()
        return row is not None


def _pending_draft_for_hash(content_hash: str) -> bool:
    with get_session() as session:
        headline = session.exec(select(Headline).where(Headline.hash == content_hash)).first()
        if not headline:
            return False
        draft = session.exec(
            select(Draft).where(Draft.headline_id == headline.id, Draft.status == "pending")
        ).first()
        return draft is not None


EARNINGS_SOURCE = "Finnhub Earnings"


@dataclass
class _EarningsWrite:
    symbol: str
    title: str
    summary: str
    draft_text: str
    impact: str
    fmt: str
    confidence: float
    chash: str
    date_str: str
    release_at: datetime
    expire_previews: bool = False


def process_earnings(budget: DraftBudget | None = None) -> tuple[int, int]:
    """Fetch earnings calendar and create preview/result drafts. Returns (ingested, drafts)."""
    if not get_finnhub_key():
        return 0, 0

    watchlist = normalized_watchlist(get_setting("watchlist", []))
    has_watchlist = bool(watchlist)

    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    preview_end = today + timedelta(days=EARNINGS_PREVIEW_DAYS_FORWARD)
    events = fetch_earnings_calendar(yesterday.isoformat(), preview_end.isoformat())

    seen: set[str] = set()
    unique_events: list[dict[str, Any]] = []
    for ev in events:
        key = f"{ev.get('symbol')}|{ev.get('date')}|{ev.get('quarter')}|{ev.get('year')}"
        if key in seen:
            continue
        seen.add(key)
        unique_events.append(ev)

    pending: list[_EarningsWrite] = []
    drafts_created = 0
    market_drafts = 0
    now = datetime.utcnow()

    for event in unique_events:
        symbol = (event.get("symbol") or "").upper()
        if not symbol:
            continue

        on_watchlist = has_watchlist and in_watchlist(symbol, watchlist)
        if has_watchlist and not on_watchlist:
            continue

        date_str = event.get("date") or today.isoformat()
        quarter = int(event.get("quarter") or 0)
        year = int(event.get("year") or 0)

        try:
            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        if budget is not None and budget.remaining <= 0:
            break
        if drafts_created >= MAX_EARNINGS_DRAFTS_PER_CYCLE:
            break

        if _has_actual(event):
            kind = "result"
            chash = _event_hash(symbol, date_str, quarter, year, kind)
            if _headline_exists(chash):
                continue
            if earnings_ticker_blocked(symbol, results_only=True):
                logger.debug("Skipping duplicate earnings result for %s", symbol)
                continue

            release_at = estimate_earnings_release_utc(
                date_str, event.get("hour"), has_actuals=True
            )
            if release_at is None or not is_earnings_fresh(release_at):
                logger.debug(
                    "Skipping stale earnings result %s %s (release %s)",
                    symbol,
                    date_str,
                    release_at,
                )
                continue

            built = _build_results(event)
            if not built:
                continue
            title, summary, draft_text, impact = built

            if not has_watchlist:
                if impact != "high":
                    continue
                if market_drafts >= MAX_MARKET_EARNINGS_DRAFTS_PER_CYCLE:
                    continue

            if was_recently_drafted(title, EARNINGS_SOURCE):
                continue

            pending.append(
                _EarningsWrite(
                    symbol=symbol,
                    title=title,
                    summary=summary,
                    draft_text=draft_text,
                    impact=impact,
                    fmt="BREAKING",
                    confidence=0.95,
                    chash=chash,
                    date_str=date_str,
                    release_at=release_at,
                    expire_previews=True,
                )
            )
            drafts_created += 1
            if not has_watchlist:
                market_drafts += 1
            continue

        if not on_watchlist:
            continue
        if event_date < today or event_date > preview_end:
            continue

        kind = "preview"
        chash = _event_hash(symbol, date_str, quarter, year, kind)
        if _headline_exists(chash) or _pending_draft_for_hash(chash):
            continue
        if earnings_ticker_blocked(symbol):
            continue

        built = _build_preview(event)
        if not built:
            continue
        title, summary, draft_text = built
        if was_recently_drafted(title, EARNINGS_SOURCE):
            continue

        release_at = estimate_earnings_release_utc(date_str, event.get("hour")) or now
        pending.append(
            _EarningsWrite(
                symbol=symbol,
                title=title,
                summary=summary,
                draft_text=draft_text,
                impact="med",
                fmt="CONTEXT",
                confidence=0.88,
                chash=chash,
                date_str=date_str,
                release_at=release_at,
            )
        )
        drafts_created += 1

    ingested = 0
    if not pending:
        return 0, 0

    with get_session() as session:
        for item in pending:
            if budget is not None and budget.remaining <= 0:
                break
            if item.expire_previews:
                expire_earnings_previews_for_ticker(item.symbol)

            headline = Headline(
                source=EARNINGS_SOURCE,
                url=_event_url(item.symbol, item.date_str),
                title=item.title,
                summary=item.summary,
                published_at=item.release_at,
                hash=item.chash,
                title_fp=title_fingerprint(item.title),
                status="drafted",
            )
            session.add(headline)
            session.flush()

            draft = Draft(
                headline_id=headline.id,
                text=item.draft_text,
                format=item.fmt,
                impact=item.impact,
                category="earnings",
                tickers=item.symbol,
                confidence=item.confidence,
                status="pending",
                created_at=now,
            )
            session.add(draft)
            ingested += 1
            if budget:
                budget.try_take(1)

        session.commit()

    if ingested:
        logger.info("Earnings: created %d drafts (%d events ingested)", ingested, ingested)
    return ingested, ingested
