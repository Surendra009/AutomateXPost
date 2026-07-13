"""Expire old pending drafts so the queue stays fresh."""

from datetime import datetime, timedelta

from sqlmodel import select

from config import EARNINGS_STALE_DRAFT_HOURS, STALE_DRAFT_HOURS
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.earnings_freshness import earnings_period_is_stale

logger = setup_logging()


def _draft_stale_cutoff(draft: Draft, headline: Headline | None) -> datetime:
    """When a pending draft should expire — earnings use a shorter window."""
    hours = STALE_DRAFT_HOURS
    if draft.category == "earnings":
        hours = EARNINGS_STALE_DRAFT_HOURS
    elif headline and headline.source == "Finnhub Earnings":
        hours = EARNINGS_STALE_DRAFT_HOURS
    return datetime.utcnow() - timedelta(hours=hours)


def expire_stale_drafts() -> int:
    """Remove pending drafts from the queue when older than their stale window."""
    expired = 0

    with get_session() as session:
        pending = session.exec(select(Draft).where(Draft.status == "pending")).all()
        headline_ids = [d.headline_id for d in pending if d.headline_id]
        headlines: dict[int, Headline] = {}
        if headline_ids:
            rows = session.exec(select(Headline).where(Headline.id.in_(headline_ids))).all()
            headlines = {h.id: h for h in rows if h.id is not None}

        for draft in pending:
            headline = headlines.get(draft.headline_id) if draft.headline_id else None
            cutoff = _draft_stale_cutoff(draft, headline)
            story_cutoff = None
            if headline and headline.published_at:
                from pipeline.freshness import news_cutoff

                story_cutoff = news_cutoff(draft.category)

            if draft.created_at < cutoff or (
                story_cutoff is not None and headline.published_at < story_cutoff
            ):
                draft.status = "stale"
                session.add(draft)
                expired += 1
                continue

            if draft.category == "earnings" and headline:
                text = f"{headline.title} {headline.summary or ''}"
                if earnings_period_is_stale(text):
                    draft.status = "stale"
                    session.add(draft)
                    expired += 1
        session.commit()

    if expired:
        logger.info("Expired %d stale drafts", expired)
    return expired
