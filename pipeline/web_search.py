"""Web news search via Serper (Google News API)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from config import MAX_WEB_RESULTS_PER_QUERY, SERPER_API_KEY, SERPER_NEWS_RECENCY, WEB_SEARCH_ENABLED
from logging_config import setup_logging
from security import redact_secrets

logger = setup_logging()

SERPER_NEWS_URL = "https://google.serper.dev/news"

_RELATIVE_DATE = re.compile(
    r"(?P<n>\d+)\s*(?P<unit>minute|minutes|min|hour|hours|hr|day|days|week|weeks)\s*ago",
    re.I,
)


def serper_configured() -> bool:
    return bool(SERPER_API_KEY)


def _parse_serper_date(date_str: str | None) -> datetime:
    """Best-effort parse Serper's relative or absolute date strings."""
    if not date_str:
        return datetime.utcnow()

    text = date_str.strip()
    match = _RELATIVE_DATE.search(text)
    if match:
        n = int(match.group("n"))
        unit = match.group("unit").lower()
        if unit.startswith("min"):
            return datetime.utcnow() - timedelta(minutes=n)
        if unit.startswith("h"):
            return datetime.utcnow() - timedelta(hours=n)
        if unit.startswith("d"):
            return datetime.utcnow() - timedelta(days=n)
        if unit.startswith("w"):
            return datetime.utcnow() - timedelta(weeks=n)

    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue

    return datetime.utcnow()


def search_news(
    query: str,
    *,
    source_label: str = "Web Search",
    limit: int | None = None,
    recency: str | None = None,
) -> list[dict[str, Any]]:
    """Search Google News through Serper. Returns headline dicts for ingest."""
    if not WEB_SEARCH_ENABLED:
        return []
    if not SERPER_API_KEY:
        logger.warning("Web search skipped — set SERPER_API_KEY on Railway")
        return []

    limit = limit if limit is not None else MAX_WEB_RESULTS_PER_QUERY
    payload = {
        "q": query,
        "num": min(limit, 10),
        "tbs": recency or SERPER_NEWS_RECENCY,
    }

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                SERPER_NEWS_URL,
                headers={
                    "X-API-KEY": SERPER_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 401:
            logger.error("Serper rejected API key (HTTP 401) — check SERPER_API_KEY")
        elif code == 402:
            logger.error("Serper account out of credits (HTTP 402)")
        else:
            logger.warning("Serper news search failed (%s): HTTP %s", query[:60], code)
        return []
    except Exception as exc:
        logger.warning("Serper news search failed (%s): %s", query[:60], redact_secrets(str(exc)))
        return []

    rows = data.get("news") or []
    items: list[dict[str, Any]] = []
    for row in rows[:limit]:
        title = (row.get("title") or "").strip()
        link = (row.get("link") or "").strip()
        if not title or not link:
            continue
        snippet = (row.get("snippet") or "").strip()
        publisher = (row.get("source") or "").strip()
        items.append(
            {
                "source": source_label,
                "url": link,
                "title": title,
                "summary": snippet[:500],
                "published_at": _parse_serper_date(row.get("date")),
                "search_query": query,
                "publisher": publisher,
            }
        )

    if items:
        logger.debug("Serper: %d results for %r", len(items), query[:80])
    return items


# Backwards-compatible alias (all callers should use search_news).
search_google_news = search_news
