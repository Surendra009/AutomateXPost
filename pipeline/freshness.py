"""News freshness — reject headlines and drafts older than MAX_NEWS_AGE_HOURS."""

from datetime import datetime, timedelta

from sqlmodel import select

from config import MAX_EARNINGS_AGE_HOURS
from pipeline.cycle_context import get_max_news_age_hours
from database import get_session
from logging_config import setup_logging
from models import Headline

logger = setup_logging()


def max_age_hours_for_category(category: str | None) -> int:
    if category == "earnings":
        return MAX_EARNINGS_AGE_HOURS
    return get_max_news_age_hours()


def news_cutoff(category: str | None = None) -> datetime:
    return datetime.utcnow() - timedelta(hours=max_age_hours_for_category(category))


def is_fresh(published_at: datetime, category: str | None = None) -> bool:
    return published_at >= news_cutoff(category)


def age_minutes(dt: datetime) -> int:
    return max(0, int((datetime.utcnow() - dt).total_seconds() / 60))


def format_age(dt: datetime) -> str:
    minutes = age_minutes(dt)
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 1440:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


def discard_stale_headlines() -> int:
    """Mark unprocessed headlines older than the freshness window as discarded."""
    cutoff = news_cutoff()
    with get_session() as session:
        stale = session.exec(
            select(Headline).where(Headline.status == "new", Headline.published_at < cutoff)
        ).all()
        for headline in stale:
            headline.status = "discarded"
            session.add(headline)
        session.commit()
        if stale:
            logger.info(
                "Discarded %d headlines older than %dh",
                len(stale),
                get_max_news_age_hours(),
            )
        return len(stale)
