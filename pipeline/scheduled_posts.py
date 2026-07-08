"""Publish drafts scheduled for a future time."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import select

from database import get_session, get_setting
from logging_config import setup_logging
from models import Draft
from pipeline.post import PostingError, publish_draft

logger = setup_logging()


def process_scheduled_posts() -> int:
    """Post drafts whose scheduled_at has passed. Returns count posted."""
    now = datetime.utcnow()
    daily_cap = get_setting("daily_post_cap", 20)
    cooldown = get_setting("cooldown_minutes", 5)
    posted = 0

    with get_session() as session:
        due = session.exec(
            select(Draft).where(
                Draft.status == "scheduled",
                Draft.scheduled_at <= now,
            )
        ).all()

        for draft in due:
            try:
                publish_draft(draft, daily_cap=daily_cap, cooldown_minutes=cooldown)
                posted += 1
            except PostingError as exc:
                row = session.get(Draft, draft.id)
                if row:
                    row.status = "pending"
                    row.scheduled_at = None
                    row.post_error = str(exc)
                    session.add(row)
                logger.warning("Scheduled post failed for draft %s: %s", draft.id, exc)

        if due:
            session.commit()

    if posted:
        logger.info("Posted %d scheduled drafts", posted)
    return posted
