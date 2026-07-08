"""Watchlist and market web searches for earnings, mergers, and company news."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from config import (
    MAX_WEB_RESULTS_PER_QUERY,
    MAX_WEB_TICKERS_PER_CYCLE,
    WEB_SEARCH_ENABLED,
)
from database import get_setting
from logging_config import setup_logging
from pipeline.noise import is_title_noise
from pipeline.url_resolve import resolve_article_url
from pipeline.watchlist_scope import normalized_watchlist
from pipeline.web_search import search_google_news

logger = setup_logging()

SOURCE_EARNINGS = "Web Search · earnings"
SOURCE_MERGER = "Web Search · mergers"
SOURCE_COMPANY = "Web Search · company"
SOURCE_CALENDAR = "Web Search · calendar"

_TICKER_QUERIES: list[tuple[str, str]] = [
    ('"{symbol}" earnings EPS revenue beat miss report', SOURCE_EARNINGS),
    ('"{symbol}" merger acquisition buyout takeover deal', SOURCE_MERGER),
    ('"{symbol}" stock company news', SOURCE_COMPANY),
]

_MARKET_QUERIES: list[tuple[str, str]] = [
    ("stocks earnings reports today beat miss revenue", SOURCE_EARNINGS),
    ("earnings calendar stocks reporting today", SOURCE_CALENDAR),
    ("stocks merger acquisition deal announced today", SOURCE_MERGER),
]


def fetch_web_news() -> list[dict[str, Any]]:
    """Primary discovery: Google News searches for earnings, mergers, and company news."""
    if not WEB_SEARCH_ENABLED:
        return []

    watchlist = normalized_watchlist(get_setting("watchlist", []))[:MAX_WEB_TICKERS_PER_CYCLE]
    seen_urls: set[str] = set()
    items: list[dict[str, Any]] = []
    per_source: dict[str, int] = {}

    def add_batch(batch: list[dict[str, Any]]) -> int:
        added = 0
        for raw in batch:
            title = (raw.get("title") or "").strip()
            if not title or is_title_noise(title):
                continue
            url = resolve_article_url(raw.get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            raw["url"] = url
            items.append(raw)
            added += 1
        return added

    for query, source in _MARKET_QUERIES:
        batch = search_google_news(query, source_label=source, limit=MAX_WEB_RESULTS_PER_QUERY)
        count = add_batch(batch)
        if count:
            per_source[source] = per_source.get(source, 0) + count

    today = datetime.utcnow().strftime("%B %d %Y")
    for symbol in watchlist:
        for template, source in _TICKER_QUERIES:
            query = template.format(symbol=symbol)
            if source == SOURCE_CALENDAR:
                query = f"{symbol} earnings report {today}"
            batch = search_google_news(query, source_label=source, limit=MAX_WEB_RESULTS_PER_QUERY)
            count = add_batch(batch)
            if count:
                per_source[source] = per_source.get(source, 0) + count

        cal_batch = search_google_news(
            f"{symbol} earnings date calendar {today}",
            source_label=SOURCE_CALENDAR,
            limit=MAX_WEB_RESULTS_PER_QUERY,
        )
        count = add_batch(cal_batch)
        if count:
            per_source[SOURCE_CALENDAR] = per_source.get(SOURCE_CALENDAR, 0) + count

    if items:
        logger.info("Web search: %d headlines (%s)", len(items), per_source)
    return items
