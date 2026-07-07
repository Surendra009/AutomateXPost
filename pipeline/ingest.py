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

from config import FINNHUB_KEY, RSS_FEEDS
from database import get_session
from logging_config import setup_logging
from models import Headline

logger = setup_logging()


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


def fetch_rss_feed(source: str, url: str) -> list[dict]:
    try:
        feed = feedparser.parse(url)
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
    except Exception as e:
        logger.warning("Failed to fetch RSS %s: %s", source, e)
        return []


def fetch_finnhub_news() -> list[dict]:
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
        items = []
        for item in data[:30]:
            items.append({
                "source": "Finnhub",
                "url": item.get("url", ""),
                "title": item.get("headline", "").strip(),
                "summary": item.get("summary", "")[:500],
                "published_at": datetime.fromtimestamp(item.get("datetime", 0)),
            })
        return items
    except Exception as e:
        logger.warning("Failed to fetch Finnhub news: %s", e)
        return []


def ingest_headlines() -> int:
    """Fetch all feeds and insert new headlines. Returns count of new items."""
    all_items: list[dict] = []
    for source, url in RSS_FEEDS:
        all_items.extend(fetch_rss_feed(source, url))
    all_items.extend(fetch_finnhub_news())

    new_count = 0
    with get_session() as session:
        for item in all_items:
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
        session.commit()

    logger.info("Ingested %d new headlines", new_count)
    return new_count


def get_unfiltered_headlines(limit: int = 50) -> list[Headline]:
    with get_session() as session:
        return list(
            session.exec(
                select(Headline)
                .where(Headline.status == "new")
                .order_by(Headline.published_at.desc())
                .limit(limit)
            ).all()
        )
