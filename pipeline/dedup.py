"""Skip LLM drafting when the same story was recently drafted."""

import hashlib
import re
from datetime import datetime, timedelta

from sqlmodel import select

from config import DRAFT_DEDUP_HOURS
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline

logger = setup_logging()

# Statuses that mean we already handled this story
_DEDUP_STATUSES = ("pending", "posted", "approved", "rejected", "stale")


def story_fingerprint(title: str, source: str) -> str:
    """Stable key from normalized title + source (same story, different URLs)."""
    normalized = re.sub(r"\s+", " ", title.strip().lower())
    raw = f"{source.strip().lower()}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def was_recently_drafted(title: str, source: str, hours: int | None = None) -> bool:
    """True if this story got a draft in the last N hours."""
    hours = hours if hours is not None else DRAFT_DEDUP_HOURS
    fp = story_fingerprint(title, source)
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    with get_session() as session:
        rows = session.exec(
            select(Draft, Headline)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(Draft.created_at >= cutoff)
            .where(Draft.status.in_(_DEDUP_STATUSES))
        ).all()

        for _draft, headline in rows:
            if story_fingerprint(headline.title, headline.source) == fp:
                return True

    return False
