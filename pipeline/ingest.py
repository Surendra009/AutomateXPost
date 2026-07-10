"""RSS and Finnhub news ingestion."""

import hashlib
import re
from datetime import datetime, timedelta
from time import mktime
from typing import Any

import feedparser
import httpx
from sqlmodel import select

from config import (
    AI_RSS_FEEDS,
    FINNHUB_GENERAL_SUPPLEMENT,
    MAX_NEWS_AGE_HOURS,
    RSS_FEEDS,
    SEC_USER_AGENT,
    WEB_SEARCH_ENABLED,
    get_finnhub_key,
)
from database import get_session, get_setting
from logging_config import setup_logging
from models import Headline
from pipeline.finnhub_api import finnhub_get, parse_finnhub_timestamp
from pipeline.freshness import get_max_news_age_hours, is_fresh, news_cutoff
from pipeline.dedup_mode import dedup_at_ingest
from pipeline.ingest_dedup import load_ingest_dedup_index
from pipeline.story_key import title_fingerprint
from pipeline.url_resolve import resolve_article_url
from pipeline.web_news import fetch_web_news
from pipeline.dedup import story_has_active_draft, title_recently_ingested

logger = setup_logging()

HTTP_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


def _content_hash(title: str, url: str) -> str:
    return hashlib.sha256(f"{title.strip().lower()}|{url}".encode()).hexdigest()[:32]


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



def _entries_from_feed(source: str, url: str, raw: str) -> list[dict]:
    feed = feedparser.parse(raw)
    items = []
    for entry in feed.entries[:30]:
        link = getattr(entry, "link", "") or ""
        title = getattr(entry, "title", "").strip()
        if not title or not link:
            continue
        published_at = _parse_date(entry)
        if not published_at:
            continue
        items.append({
            "source": source,
            "url": link,
            "title": title,
            "summary": _get_summary(entry),
            "published_at": published_at,
        })
    return items


def fetch_rss_feed(source: str, url: str) -> list[dict]:
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=HTTP_HEADERS) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return _entries_from_feed(source, url, resp.text)
    except Exception as e:
        logger.warning("Failed to fetch RSS %s: %s", source, e)
        return []


def fetch_sec_edgar_feed() -> list[dict]:
    source, url = SEC_EDGAR_8K_FEED
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=HTTP_HEADERS) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return _entries_from_feed(source, url, resp.text)
    except Exception as e:
        logger.warning("Failed to fetch SEC EDGAR 8-K: %s", e)
        return []


def fetch_finnhub_general_news() -> list[dict]:
    if not get_finnhub_key():
        return []
    data, err = finnhub_get("news", {"category": "general"})
    if err:
        logger.warning("Finnhub general news: %s", err)
        return []
    if not isinstance(data, list):
        return []
    return _finnhub_items(data, "Finnhub")


def fetch_finnhub_company_news(watchlist: list[str]) -> list[dict]:
    """Per-ticker company news — requires FINNHUB_KEY and watchlist tickers."""
    if not get_finnhub_key() or not watchlist:
        return []

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=2)).isoformat()
    to_date = today.isoformat()
    items: list[dict] = []

    for symbol in watchlist[:15]:
        symbol = symbol.upper().strip()
        if not symbol:
            continue
        data, err = finnhub_get(
            "company-news",
            {"symbol": symbol, "from": from_date, "to": to_date},
        )
        if err:
            logger.warning("Finnhub company news %s: %s", symbol, err)
            continue
        if isinstance(data, list):
            items.extend(_finnhub_items(data, f"Finnhub ${symbol}", limit=15))

    return items


def _finnhub_items(data: list[dict], source: str, limit: int = 30) -> list[dict]:
    items = []
    for item in data[:limit]:
        title = item.get("headline", "").strip()
        url = item.get("url", "")
        if not title or not url:
            continue
        items.append({
            "source": source,
            "url": url,
            "title": title,
            "summary": item.get("summary", "")[:500],
            "published_at": parse_finnhub_timestamp(item.get("datetime")),
        })
    return items


def ingest_headlines() -> tuple[int, dict[str, int], int, int]:
    """Fetch all feeds and insert new headlines.

    Returns (new_count, per_source_counts, skipped_stale, skipped_dup).
    """
    watchlist = get_setting("watchlist", [])
    source_batches: list[tuple[str, list[dict]]] = []

    for source, url in RSS_FEEDS:
        items = fetch_rss_feed(source, url)
        source_batches.append((source, items))

    for source, url in AI_RSS_FEEDS:
        items = fetch_rss_feed(source, url)
        source_batches.append((source, items))

    # SEC 8-K and Finnhub company news handled by structured processors (no double-fetch)

    if WEB_SEARCH_ENABLED:
        web_items = fetch_web_news()
        if web_items:
            source_batches.append(("Web Search", web_items))

    if get_finnhub_key() and (FINNHUB_GENERAL_SUPPLEMENT or not WEB_SEARCH_ENABLED):
        finnhub_general = fetch_finnhub_general_news()
        if finnhub_general:
            source_batches.append(("Finnhub", finnhub_general))

    per_source: dict[str, int] = {}
    skipped_stale: dict[str, int] = {}
    skipped_dup: dict[str, int] = {}
    total_stale = 0
    total_dup = 0
    new_count = 0
    dedup_index = load_ingest_dedup_index()

    with get_session() as session:
        for source, items in source_batches:
            added = 0
            skipped = 0
            dupes = 0
            for item in items:
                if not is_fresh(item["published_at"]):
                    skipped += 1
                    continue
                if story_has_active_draft(item["title"]) or title_recently_ingested(item["title"]):
                    dupes += 1
                    continue
                url = resolve_article_url(item["url"])
                chash = _content_hash(item["title"], url)
                if dedup_at_ingest():
                    dup_reason = dedup_index.is_duplicate(item["title"], chash)
                else:
                    dup_reason = None
                if dup_reason:
                    dupes += 1
                    continue
                headline = Headline(
                    source=item["source"],
                    url=url,
                    title=item["title"],
                    summary=item["summary"],
                    published_at=item["published_at"],
                    hash=chash,
                    title_fp=title_fingerprint(item["title"]),
                    status="new",
                )
                session.add(headline)
                dedup_index.add(item["title"], chash)
                new_count += 1
                added += 1
            per_source[source] = added
            total_stale += skipped
            total_dup += dupes
            if skipped:
                skipped_stale[source] = skipped
            if dupes:
                skipped_dup[source] = dupes
        session.commit()

    max_age = get_max_news_age_hours()
    if skipped_stale:
        logger.info("Skipped stale headlines (>%dh): %s", max_age, str(skipped_stale))
    if skipped_dup:
        logger.info("Skipped cross-source duplicates: %s", str(skipped_dup))
    logger.info("Ingested %d new headlines: %s", new_count, str(per_source))
    return new_count, per_source, total_stale, total_dup


def get_unfiltered_headlines(limit: int = 50) -> list[Headline]:
    cutoff = news_cutoff()
    with get_session() as session:
        rows = list(
            session.exec(
                select(Headline)
                .where(Headline.status == "new", Headline.published_at >= cutoff)
                .order_by(Headline.published_at.desc())
                .limit(limit * 2)
            ).all()
        )
    fresh: list[Headline] = []
    for headline in rows:
        if story_has_active_draft(headline.title):
            continue
        fresh.append(headline)
        if len(fresh) >= limit:
            break
    return fresh
