"""Expire old pending drafts so the queue stays fresh."""

from datetime import datetime, timedelta

from sqlmodel import select

from config import STALE_DRAFT_HOURS
from database import get_session
from logging_config import setup_logging
from models import Draft

logger = setup_logging()


def expire_stale_drafts() -> int:
    """Mark pending drafts older than STALE_DRAFT_HOURS as stale."""
    cutoff = datetime.utcnow() - timedelta(hours=STALE_DRAFT_HOURS)
    with get_session() as session:
        stale = session.exec(
            select(Draft).where(Draft.status == "pending", Draft.created_at < cutoff)
        ).all()
        for draft in stale:
            draft.status = "stale"
            session.add(draft)
        session.commit()
        if stale:
            logger.info("Expired %d stale drafts (older than %dh)", len(stale), STALE_DRAFT_HOURS)
        return len(stale)
