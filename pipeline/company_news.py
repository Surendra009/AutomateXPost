"""Finnhub company news — zero-LLM drafts using explicit ticker fields."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from config import MAX_COMPANY_NEWS_DRAFTS_PER_CYCLE
from database import get_setting
from logging_config import setup_logging
from pipeline.draft_budget import DraftBudget
from pipeline.finnhub_api import finnhub_get, get_finnhub_key, parse_finnhub_timestamp
from pipeline.freshness import is_fresh
from pipeline.noise import is_title_noise
from pipeline.structured_common import content_hash, save_structured_draft
from pipeline.earnings_parse import build_earnings_lines, extract_earnings_facts, format_earnings_draft
from pipeline.enrich import fetch_article_text
from pipeline.watchlist_scope import normalized_watchlist

logger = setup_logging()

COMPANY_NEWS_SOURCE = "Finnhub Company"

BEAT_MISS = re.compile(
    r"\b(beat|beats|beating|topped|tops|exceeded|exceeds|surpassed|"
    r"missed|misses|missing|fell short|below expectations|in.?line with)\b",
    re.I,
)
EARNINGS_CONTEXT = re.compile(
    r"\b(eps|earnings|revenue|sales|quarterly results|q[1-4])\b",
    re.I,
)
GUIDANCE = re.compile(r"\b(guidance (raised|lowered|cut|hiked)|outlook (raised|lowered|cut))\b", re.I)
DEAL = re.compile(
    r"\b("
    r"acquires?|acquired|acquiring|acquisition(?:\s+of)?|"
    r"merger(?:\s+with)?|merges?\s+with|merged\s+with|"
    r"agrees?\s+to\s+buy|to\s+acquire|deal\s+to\s+acquire|"
    r"takeover|buyout|bought\s+out"
    r")\b",
    re.I,
)
_PAREN_TICKER = re.compile(r"\(([A-Z]{1,5})\)")


def _fetch_company_news(symbol: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    data, err = finnhub_get(
        "company-news",
        {"symbol": symbol, "from": from_date, "to": to_date},
    )
    if err:
        logger.warning("Finnhub company news %s: %s", symbol, err)
        return []
    return data if isinstance(data, list) else []


def _parenthetical_ticker(headline: str) -> str | None:
    match = _PAREN_TICKER.search(headline)
    return match.group(1) if match else None


def _ticker_matches_headline(ticker: str, headline: str) -> bool:
    """Reject when a parenthetical subject ticker differs from the assigned ticker."""
    subject = _parenthetical_ticker(headline)
    if subject and subject != ticker.upper():
        return False
    return True


def _beat_miss_word(match: re.Match) -> str:
    word = match.group(0).lower()
    if word in ("missed", "misses", "missing", "fell short", "below expectations"):
        return "missed"
    if "in-line" in word or "inline" in word:
        return "matched"
    return "beat"


def _build_draft(
    symbol: str,
    headline: str,
    summary: str,
    url: str = "",
) -> tuple[str, str, str, str, str, float] | None:
    text = f"{headline} {summary}"
    bm = BEAT_MISS.search(headline)

    if bm and EARNINGS_CONTEXT.search(text):
        verb = _beat_miss_word(bm)
        article = ""
        facts = extract_earnings_facts(text)
        if url:
            article = fetch_article_text(url) or ""
            if article:
                facts = extract_earnings_facts(f"{text} {article[:3000]}")
        lines = build_earnings_lines(
            symbol,
            verb,
            facts,
            source_text=text,
            article_text=article,
        )
        if not lines:
            return None
        line1, line2, line3, highlights = lines
        impact = "high" if verb in ("beat", "missed") else "med"
        fmt = "BREAKING"
        confidence = 0.9
        category = "earnings"
        draft = format_earnings_draft(
            line1, line2, line3, highlights=highlights, ticker=symbol
        )
        return draft, category, impact, fmt, confidence, line1
    elif GUIDANCE.search(text):
        line1 = f"{symbol} updated guidance"
        line2 = "Forward outlook reset for the stock"
        impact = "high"
        fmt = "BREAKING"
        confidence = 0.88
        category = "earnings"
    elif DEAL.search(headline):
        line1 = f"{symbol} M&A headline"
        line2 = headline[:72] + ("…" if len(headline) > 72 else "")
        impact = "high"
        fmt = "BREAKING"
        confidence = 0.86
        category = "regulatory"
    else:
        return None

    draft = f"{line1}\n{line2}\n\n${symbol}"
    return draft, category, impact, fmt, confidence, line1


def process_company_news(budget: DraftBudget | None = None) -> tuple[int, int]:
    """Draft structured company news using Finnhub ticker field. Returns (ingested, drafts)."""
    if not get_finnhub_key():
        return 0, 0

    watchlist = get_setting("watchlist", [])
    symbols = normalized_watchlist(watchlist)[:15]
    if not symbols:
        return 0, 0

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=1)).isoformat()
    to_date = today.isoformat()

    ingested = 0
    drafts_created = 0
    seen_ids: set[int] = set()

    for symbol in symbols[:15]:
        if budget is not None and budget.remaining <= 0:
            break
        if drafts_created >= MAX_COMPANY_NEWS_DRAFTS_PER_CYCLE:
            break
        symbol = symbol.upper().strip()
        if not symbol:
            continue

        for item in _fetch_company_news(symbol, from_date, to_date)[:12]:
            if drafts_created >= MAX_COMPANY_NEWS_DRAFTS_PER_CYCLE:
                break

            news_id = item.get("id")
            if news_id is not None:
                if news_id in seen_ids:
                    continue
                seen_ids.add(news_id)

            headline = (item.get("headline") or "").strip()
            url = (item.get("url") or "").strip()
            summary = (item.get("summary") or "")[:500]
            if not headline or not url:
                continue

            published_at = parse_finnhub_timestamp(item.get("datetime"))
            if not is_fresh(published_at):
                continue

            if is_title_noise(headline):
                continue

            # Prefer explicit related ticker from API; fall back to query symbol
            related = (item.get("related") or "").strip().upper()
            ticker = symbol
            if related:
                first = related.split(",")[0].strip()
                if first and re.fullmatch(r"[A-Z]{1,5}", first):
                    ticker = first

            if not _ticker_matches_headline(ticker, headline):
                continue

            built = _build_draft(ticker, headline, summary, url)
            if not built:
                continue

            draft_text, category, impact, fmt, confidence, _line1 = built
            chash = content_hash(COMPANY_NEWS_SOURCE, str(news_id or url), ticker)

            if save_structured_draft(
                source=COMPANY_NEWS_SOURCE,
                url=url,
                title=headline,
                summary=summary,
                draft_text=draft_text,
                tickers=ticker,
                category=category,
                impact=impact,
                fmt=fmt,
                confidence=confidence,
                chash=chash,
                published_at=published_at,
                budget=budget,
            ):
                ingested += 1
                drafts_created += 1

    if drafts_created:
        logger.info("Company news: created %d structured drafts", drafts_created)
    return ingested, drafts_created
