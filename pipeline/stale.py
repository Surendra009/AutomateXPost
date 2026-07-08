"""Expire old pending drafts so the queue stays fresh."""

from datetime import datetime, timedelta

from sqlmodel import select

from config import STALE_DRAFT_HOURS
from database import get_session
from logging_config import setup_logging
from models import Draft

logger = setup_logging()


def expire_stale_drafts() -> int:
    """Remove pending drafts from the queue when older than STALE_DRAFT_HOURS."""
    draft_cutoff = datetime.utcnow() - timedelta(hours=STALE_DRAFT_HOURS)
    expired = 0

    with get_session() as session:
        pending = session.exec(select(Draft).where(Draft.status == "pending")).all()
        for draft in pending:
            if draft.created_at < draft_cutoff:
                draft.status = "stale"
                session.add(draft)
                expired += 1
        session.commit()

    if expired:
        logger.info(
            "Expired %d stale drafts (older than %dh)",
            expired,
            STALE_DRAFT_HOURS,
        )
    return expired
