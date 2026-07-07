"""RSS and Finnhub news ingestion."""

import hashlib
import re
from datetime import datetime, timedelta
from time import mktime
from typing import Any

import feedparser
import httpx
from rapidfuzz import fuzz
from sqlmodel import select

from config import FINNHUB_KEY, MAX_NEWS_AGE_HOURS, RSS_FEEDS, AI_RSS_FEEDS, SEC_EDGAR_8K_FEED, SEC_USER_AGENT
from database import get_session, get_setting
from logging_config import setup_logging
from models import Headline
from pipeline.freshness import is_fresh, news_cutoff

logger = setup_logging()

HTTP_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


def _content_hash(title: str, url: str) -> str:
    return hashlib.sha256(f"{title.strip().lower()}|{url}".encode()).hexdigest()[:32]


def _parse_date(entry: Any) -> datetime:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime.fromtimestamp(mktime(parsed))
            except (ValueError, OverflowError):
                pass
    return datetime.utcnow()


def _get_summary(entry: Any) -> str:
    for attr in ("summary", "description", "content"):
        val = getattr(entry, attr, None)
        if val:
            if isinstance(val, list) and val:
                return re.sub(r"<[^>]+>", "", val[0].get("value", ""))[:500]
            return re.sub(r"<[^>]+>", "", str(val))[:500]
    return ""


def _is_duplicate(session, title: str, content_hash: str) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=48)
    existing = session.exec(
        select(Headline).where(Headline.published_at >= cutoff)
    ).all()

    for h in existing:
        if h.hash == content_hash:
            return True
        if fuzz.ratio(title.lower(), h.title.lower()) > 90:
            return True
    return False


def _entries_from_feed(source: str, url: str, raw: str) -> list[dict]:
    feed = feedparser.parse(raw)
    items = []
    for entry in feed.entries[:30]:
        link = getattr(entry, "link", "") or ""
        title = getattr(entry, "title", "").strip()
        if not title or not link:
            continue
        items.append({
            "source": source,
            "url": link,
            "title": title,
            "summary": _get_summary(entry),
            "published_at": _parse_date(entry),
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
    if not FINNHUB_KEY:
        return []
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                "https://finnhub.io/api/v1/news",
                params={"category": "general", "token": FINNHUB_KEY},
            )
            resp.raise_for_status()
            data = resp.json()
        return _finnhub_items(data, "Finnhub")
    except Exception as e:
        logger.warning("Failed to fetch Finnhub general news: %s", e)
        return []


def fetch_finnhub_company_news(watchlist: list[str]) -> list[dict]:
    """Per-ticker company news — requires FINNHUB_KEY and watchlist tickers."""
    if not FINNHUB_KEY or not watchlist:
        return []

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=2)).isoformat()
    to_date = today.isoformat()
    items: list[dict] = []

    try:
        with httpx.Client(timeout=30) as client:
            for symbol in watchlist[:15]:
                symbol = symbol.upper().strip()
                if not symbol:
                    continue
                resp = client.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={
                        "symbol": symbol,
                        "from": from_date,
                        "to": to_date,
                        "token": FINNHUB_KEY,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                items.extend(_finnhub_items(data, f"Finnhub ${symbol}", limit=15))
    except Exception as e:
        logger.warning("Failed to fetch Finnhub company news: %s", e)

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
            "published_at": datetime.fromtimestamp(item.get("datetime", 0)),
        })
    return items


def ingest_headlines() -> tuple[int, dict[str, int]]:
    """Fetch all feeds and insert new headlines. Returns (new_count, per_source_counts)."""
    watchlist = get_setting("watchlist", [])
    source_batches: list[tuple[str, list[dict]]] = []

    for source, url in RSS_FEEDS:
        items = fetch_rss_feed(source, url)
        source_batches.append((source, items))

    for source, url in AI_RSS_FEEDS:
        items = fetch_rss_feed(source, url)
        source_batches.append((source, items))

    sec_items = fetch_sec_edgar_feed()
    source_batches.append((SEC_EDGAR_8K_FEED[0], sec_items))

    finnhub_general = fetch_finnhub_general_news()
    source_batches.append(("Finnhub", finnhub_general))

    finnhub_company = fetch_finnhub_company_news(watchlist)
    if finnhub_company:
        source_batches.append(("Finnhub watchlist", finnhub_company))

    per_source: dict[str, int] = {}
    skipped_stale: dict[str, int] = {}
    new_count = 0

    with get_session() as session:
        for source, items in source_batches:
            added = 0
            skipped = 0
            for item in items:
                if not is_fresh(item["published_at"]):
                    skipped += 1
                    continue
                chash = _content_hash(item["title"], item["url"])
                if _is_duplicate(session, item["title"], chash):
                    continue
                headline = Headline(
                    source=item["source"],
                    url=item["url"],
                    title=item["title"],
                    summary=item["summary"],
                    published_at=item["published_at"],
                    hash=chash,
                    status="new",
                )
                session.add(headline)
                new_count += 1
                added += 1
            per_source[source] = added
            if skipped:
                skipped_stale[source] = skipped
        session.commit()

    if skipped_stale:
        logger.info("Skipped stale headlines (>%dh): %s", MAX_NEWS_AGE_HOURS, skipped_stale)
    logger.info("Ingested %d new headlines: %s", new_count, per_source)
    return new_count, per_source


def get_unfiltered_headlines(limit: int = 50) -> list[Headline]:
    cutoff = news_cutoff()
    with get_session() as session:
        return list(
            session.exec(
                select(Headline)
                .where(Headline.status == "new", Headline.published_at >= cutoff)
                .order_by(Headline.published_at.desc())
                .limit(limit)
            ).all()
        )
