"""Price-based discovery — big movers and all-time / 52-week highs via Finnhub quotes."""

from __future__ import annotations

from datetime import datetime

from config import (
    ATH_PROXIMITY_PCT,
    MARKET_MOVER_PCT_THRESHOLD,
    MARKET_MOVERS_ENABLED,
    MAX_MARKET_MOVER_DRAFTS_PER_CYCLE,
    MAX_MOVER_QUOTES_PER_CYCLE,
)
from logging_config import setup_logging
from pipeline.draft_budget import DraftBudget
from pipeline.finnhub_api import fetch_52_week_high, fetch_quote, get_finnhub_key
from pipeline.market_universe import scan_universe
from pipeline.structured_common import content_hash, save_structured_draft

logger = setup_logging()

MOVERS_SOURCE = "Finnhub Movers"
MAX_ATH_METRIC_LOOKUPS = 15


def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 100:
        return f"${value:.1f}"
    return f"${value:.2f}"


def _fmt_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def _build_mover_draft(symbol: str, pct: float, price: float, reason: str) -> str:
    direction = "up" if pct >= 0 else "down"
    line1 = f"{symbol} {direction} {_fmt_pct(pct)} today — {_fmt_price(price)}"
    line2 = reason
    line3 = "Volume and headlines drive whether the move holds into the close"
    return f"{line1}\n{line2}\n{line3}\n\n${symbol}"


def _build_ath_draft(symbol: str, price: float, high_52: float) -> str:
    line1 = f"{symbol} trading at {_fmt_price(price)} — at/near 52-week highs"
    line2 = f"Prior 52-week high was {_fmt_price(high_52)}"
    line3 = "Breakouts attract momentum flows — watch for a failed breakout fade"
    return f"{line1}\n{line2}\n{line3}\n\n${symbol}"


def _near_ath(price: float, high_52: float) -> bool:
    if high_52 <= 0 or price <= 0:
        return False
    gap_pct = (high_52 - price) / high_52 * 100
    return gap_pct <= ATH_PROXIMITY_PCT or price >= high_52


def process_market_movers(budget: DraftBudget | None = None) -> tuple[int, int]:
    """Scan quotes for big % moves and ATH proximity. Returns (ingested, drafts)."""
    if not MARKET_MOVERS_ENABLED or not get_finnhub_key():
        return 0, 0

    symbols = scan_universe(max_symbols=MAX_MOVER_QUOTES_PER_CYCLE)
    if not symbols:
        return 0, 0

    quotes: list[tuple[str, dict[str, float]]] = []
    for symbol in symbols:
        quote = fetch_quote(symbol)
        if quote:
            quotes.append((symbol, quote))

    # Prioritize largest moves for 52-week high lookups (rate-limit friendly)
    quotes.sort(key=lambda item: abs(item[1]["pct"]), reverse=True)

    ingested = 0
    drafts_created = 0
    today = datetime.utcnow().date().isoformat()
    metric_lookups = 0

    for symbol, quote in quotes:
        if budget is not None and budget.remaining <= 0:
            break
        if drafts_created >= MAX_MARKET_MOVER_DRAFTS_PER_CYCLE:
            break

        pct = quote["pct"]
        price = quote["price"]
        abs_pct = abs(pct)
        is_mover = abs_pct >= MARKET_MOVER_PCT_THRESHOLD

        high_52 = None
        if is_mover or (abs_pct >= 2.0 and metric_lookups < MAX_ATH_METRIC_LOOKUPS):
            high_52 = fetch_52_week_high(symbol)
            metric_lookups += 1

        is_ath = high_52 is not None and _near_ath(price, high_52)

        if not is_mover and not is_ath:
            continue

        if is_ath and not is_mover:
            title = f"{symbol} at 52-week high — {_fmt_price(price)}"
            summary = f"{symbol} trading near record highs ({_fmt_price(price)} vs 52w {_fmt_price(high_52)})"
            draft_text = _build_ath_draft(symbol, price, high_52)
            kind = "ath"
            impact = "high"
            confidence = 0.9
        else:
            verb = "rallies" if pct >= 0 else "slides"
            title = f"{symbol} {verb} {_fmt_pct(pct)} on the session — {_fmt_price(price)}"
            ath_note = ""
            if is_ath and high_52:
                ath_note = " · at 52-week highs"
            summary = f"{symbol} moved {_fmt_pct(pct)} today{ath_note}"
            reason = "Big session move — check earnings, guidance, or sector news"
            if is_ath and high_52:
                reason = f"Price at 52-week highs ({_fmt_price(high_52)}) — momentum or breakout trade"
            draft_text = _build_mover_draft(symbol, pct, price, reason)
            kind = "mover"
            impact = "high" if abs_pct >= MARKET_MOVER_PCT_THRESHOLD + 2 else "med"
            confidence = 0.91 if abs_pct >= 6 else 0.87

        chash = content_hash(MOVERS_SOURCE, symbol, today, kind, f"{pct:.2f}")
        url = f"https://finnhub.io/quote/{symbol}"

        if save_structured_draft(
            source=MOVERS_SOURCE,
            url=url,
            title=title,
            summary=summary,
            draft_text=draft_text,
            tickers=symbol,
            category="other",
            impact=impact,
            fmt="BREAKING" if impact == "high" else "CONTEXT",
            confidence=confidence,
            chash=chash,
            budget=budget,
        ):
            ingested += 1
            drafts_created += 1

    if drafts_created:
        logger.info(
            "Market movers: created %d drafts (scanned %d quotes, %d metric lookups)",
            drafts_created,
            len(quotes),
            metric_lookups,
        )
    return ingested, drafts_created
