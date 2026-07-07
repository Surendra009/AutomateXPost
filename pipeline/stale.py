"""Expire old pending drafts so the queue stays fresh."""

from datetime import datetime, timedelta

from sqlmodel import select

from config import MAX_NEWS_AGE_HOURS, STALE_DRAFT_HOURS
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.freshness import news_cutoff

logger = setup_logging()


def expire_stale_drafts() -> int:
    """Mark pending drafts stale if the story or draft exceeds the freshness window."""
    draft_cutoff = datetime.utcnow() - timedelta(hours=STALE_DRAFT_HOURS)
    story_cutoff = news_cutoff()
    expired = 0

    with get_session() as session:
        pending = session.exec(select(Draft).where(Draft.status == "pending")).all()
        for draft in pending:
            headline = session.get(Headline, draft.headline_id)
            story_old = headline is not None and headline.published_at < story_cutoff
            draft_old = draft.created_at < draft_cutoff
            if story_old or draft_old:
                draft.status = "stale"
                session.add(draft)
                expired += 1
        session.commit()

    if expired:
        logger.info(
            "Expired %d stale drafts (story/draft older than %dh)",
            expired,
            MAX_NEWS_AGE_HOURS,
        )
    return expired
