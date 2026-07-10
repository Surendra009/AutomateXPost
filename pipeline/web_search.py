"""Web news search via Google News RSS (free, no API key)."""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime
from time import mktime
from typing import Any

import feedparser
import httpx

from config import MAX_WEB_RESULTS_PER_QUERY, SEC_USER_AGENT, WEB_SEARCH_ENABLED
from logging_config import setup_logging
from security import redact_secrets

logger = setup_logging()

HTTP_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


def web_search_configured() -> bool:
    """Google News RSS needs no API key — enabled when WEB_SEARCH_ENABLED is true."""
    return WEB_SEARCH_ENABLED


# Backwards compat after Serper experiment
serper_configured = web_search_configured


def _parse_feed_date(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime.fromtimestamp(mktime(parsed))
            except (ValueError, OverflowError):
                pass
    return None


def _get_summary(entry: Any) -> str:
    for attr in ("summary", "description", "content"):
        val = getattr(entry, attr, None)
        if val:
            if isinstance(val, list) and val:
                return re.sub(r"<[^>]+>", "", val[0].get("value", ""))[:500]
            return re.sub(r"<[^>]+>", "", str(val))[:500]
    return ""


def search_news(
    query: str,
    *,
    source_label: str = "Web Search",
    limit: int | None = None,
    recency: str | None = None,
) -> list[dict[str, Any]]:
    """Search Google News RSS (free). `recency` is ignored — freshness gated at ingest."""
    if not WEB_SEARCH_ENABLED:
        return []

    limit = limit if limit is not None else MAX_WEB_RESULTS_PER_QUERY
    encoded = urllib.parse.quote(query)
    # when:1d biases results to the past day when supported by Google News
    when = "1d" if (recency or "1d") in ("1d", "qdr:d", "pd", "day") else ""
    when_param = f"+when:{when}" if when else ""
    feed_url = (
        f"https://news.google.com/rss/search?q={encoded}{when_param}"
        "&hl=en-US&gl=US&ceid=US:en"
    )

    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=HTTP_HEADERS) as client:
            resp = client.get(feed_url)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
    except Exception as exc:
        logger.warning("Google News search failed (%s): %s", query[:60], redact_secrets(str(exc)))
        return []

    items: list[dict[str, Any]] = []
    for entry in feed.entries[:limit]:
        link = getattr(entry, "link", "") or ""
        title = getattr(entry, "title", "").strip()
        if not title or not link:
            continue
        published_at = _parse_feed_date(entry)
        if not published_at:
            continue
        items.append(
            {
                "source": source_label,
                "url": link,
                "title": title,
                "summary": _get_summary(entry),
                "published_at": published_at,
                "search_query": query,
            }
        )

    if items:
        logger.debug("Google News: %d results for %r", len(items), query[:80])
    return items


search_google_news = search_news
