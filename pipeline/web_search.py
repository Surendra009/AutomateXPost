"""Web search via Google News RSS (no API key)."""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime
from time import mktime
from typing import Any

import feedparser
import httpx

from config import SEC_USER_AGENT
from logging_config import setup_logging

logger = setup_logging()

HTTP_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


def _parse_date(entry: Any) -> datetime | None:
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


def search_google_news(
    query: str,
    *,
    source_label: str = "Web Search",
    limit: int = 8,
) -> list[dict[str, Any]]:
    encoded = urllib.parse.quote(query)
    feed_url = (
        f"https://news.google.com/rss/search?q={encoded}"
        "&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=HTTP_HEADERS) as client:
            resp = client.get(feed_url)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
    except Exception as exc:
        logger.warning("Google News search failed (%s): %s", query[:60], exc)
        return []

    items: list[dict[str, Any]] = []
    for entry in feed.entries[:limit]:
        link = getattr(entry, "link", "") or ""
        title = getattr(entry, "title", "").strip()
        if not title or not link:
            continue
        published_at = _parse_date(entry)
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
    return items
