"""Finnhub earnings calendar — previews (BMO/AMC) and actual vs estimate results."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlmodel import select

from config import FINNHUB_KEY, MAX_EARNINGS_DRAFTS_PER_CYCLE
from database import get_session, get_setting
from logging_config import setup_logging
from models import Draft, Headline

logger = setup_logging()

EARNINGS_SOURCE = "Finnhub Earnings"

# When watchlist is empty, track mega-cap / widely followed names
DEFAULT_EARNINGS_SYMBOLS = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "AMD", "INTC",
    "NFLX", "CRM", "ORCL", "AVGO", "QCOM", "UBER", "ABNB", "SHOP", "SQ", "PYPL",
    "COIN", "PLTR", "SNOW", "CRWD", "PANW", "ADBE", "IBM", "DIS", "BA", "JPM",
    "GS", "V", "MA", "WMT", "COST", "HD", "LOW", "TGT", "NKE", "SBUX",
    "RIVN", "LCID", "F", "GM", "MU", "LRCX", "AMAT", "ASML", "TSM", "ARM",
}


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


def _in_scope(symbol: str, watchlist: list[str]) -> bool:
    sym = symbol.upper()
    if watchlist:
        return sym in {w.upper() for w in watchlist}
    return sym in DEFAULT_EARNINGS_SYMBOLS


def _has_actual(event: dict[str, Any]) -> bool:
    eps_actual = event.get("epsActual")
    rev_actual = event.get("revenueActual")
    return eps_actual is not None or rev_actual is not None


def fetch_earnings_calendar(from_date: str, to_date: str) -> list[dict[str, Any]]:
    if not FINNHUB_KEY:
        return []
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                "https://finnhub.io/api/v1/calendar/earnings",
                params={"from": from_date, "to": to_date, "token": FINNHUB_KEY},
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("earningsCalendar") or []
    except Exception as e:
        logger.warning("Failed to fetch Finnhub earnings calendar: %s", e)
        return []


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

    line1 = f"{symbol} reports {timing} today"
    if hour_tag:
        line1 = f"{symbol} reports {hour_tag} today"
    draft = f"{line1}\n{est_line}\n\n${symbol}"
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

    # Draft — lead with strongest surprise
    if eps_word:
        verb = {"beat": "beat", "miss": "missed", "in-line": "matched"}[eps_word]
        line1 = f"{symbol} {verb} {q_label}EPS {eps_actual_s} vs {eps_est_s} est"
    else:
        line1 = f"{symbol} reported {q_label}earnings"

    line2_parts = []
    if rev_actual_s and rev_est_s and rev_word:
        line2_parts.append(f"Revenue {rev_actual_s} vs {rev_est_s} est")
    elif eps_word == "in-line" and rev_actual_s:
        line2_parts.append(f"Revenue {rev_actual_s}")

    timing_suffix = hour if hour in ("BMO", "AMC", "DMH") else hour
    line2 = " — ".join(line2_parts) if line2_parts else ""
    if line2 and timing_suffix:
        line2 = f"{line2} — {timing_suffix}"

    draft = f"{line1}\n{line2}\n\n${symbol}".replace("\n\n\n", "\n\n").strip()
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


def process_earnings() -> tuple[int, int]:
    """Fetch earnings calendar and create preview/result drafts. Returns (ingested, drafts)."""
    if not FINNHUB_KEY:
        return 0, 0

    watchlist = get_setting("watchlist", [])
    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    events: list[dict[str, Any]] = []
    events.extend(fetch_earnings_calendar(yesterday.isoformat(), today.isoformat()))

    # Deduplicate by symbol+date+quarter
    seen: set[str] = set()
    unique_events = []
    for ev in events:
        key = f"{ev.get('symbol')}|{ev.get('date')}|{ev.get('quarter')}|{ev.get('year')}"
        if key in seen:
            continue
        seen.add(key)
        unique_events.append(ev)

    ingested = 0
    drafts_created = 0
    now = datetime.utcnow()

    with get_session() as session:
        for event in unique_events:
            symbol = (event.get("symbol") or "").upper()
            if not _in_scope(symbol, watchlist):
                continue

            date_str = event.get("date") or today.isoformat()
            quarter = int(event.get("quarter") or 0)
            year = int(event.get("year") or 0)

            try:
                event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            if drafts_created >= MAX_EARNINGS_DRAFTS_PER_CYCLE:
                break

            # Results take priority over preview when actuals are in
            if _has_actual(event):
                built = _build_results(event)
                if not built:
                    continue
                title, summary, draft_text, impact = built
                kind = "result"
                chash = _event_hash(symbol, date_str, quarter, year, kind)
                if _headline_exists(chash):
                    continue

                headline = Headline(
                    source=EARNINGS_SOURCE,
                    url=_event_url(symbol, date_str),
                    title=title,
                    summary=summary,
                    published_at=now,
                    hash=chash,
                    status="drafted",
                )
                session.add(headline)
                session.flush()

                draft = Draft(
                    headline_id=headline.id,
                    text=draft_text,
                    format="BREAKING",
                    impact=impact,
                    category="earnings",
                    tickers=symbol,
                    confidence=0.95,
                    status="pending",
                    created_at=now,
                )
                session.add(draft)
                ingested += 1
                drafts_created += 1
                continue

            # Preview for today's reports without actuals yet
            if event_date != today:
                continue

            built = _build_preview(event)
            if not built:
                continue
            title, summary, draft_text = built
            kind = "preview"
            chash = _event_hash(symbol, date_str, quarter, year, kind)
            if _headline_exists(chash) or _pending_draft_for_hash(chash):
                continue

            headline = Headline(
                source=EARNINGS_SOURCE,
                url=_event_url(symbol, date_str),
                title=title,
                summary=summary,
                published_at=now,
                hash=chash,
                status="drafted",
            )
            session.add(headline)
            session.flush()

            draft = Draft(
                headline_id=headline.id,
                text=draft_text,
                format="CONTEXT",
                impact="med",
                category="earnings",
                tickers=symbol,
                confidence=0.88,
                status="pending",
                created_at=now,
            )
            session.add(draft)
            ingested += 1
            drafts_created += 1

        session.commit()

    if drafts_created:
        logger.info("Earnings: created %d drafts (%d events ingested)", drafts_created, ingested)
    return ingested, drafts_created
