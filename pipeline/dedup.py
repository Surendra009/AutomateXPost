"""Skip drafting when the same story was recently drafted."""

from datetime import datetime, timedelta

from rapidfuzz import fuzz
from sqlmodel import select

from config import DRAFT_DEDUP_HOURS, INGEST_TITLE_FUZZY_THRESHOLD
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.dedup_mode import dedup_at_pipeline
from pipeline.story_key import normalize_title, story_fingerprint, title_fingerprint

logger = setup_logging()

_DEDUP_STATUSES = ("pending", "posted", "approved", "rejected", "stale")


def was_recently_drafted(title: str, source: str, hours: int | None = None) -> bool:
    """True if this story (including cross-source matches) got a draft recently."""
    if not dedup_at_pipeline():
        return False
    hours = hours if hours is not None else DRAFT_DEDUP_HOURS
    source_fp = story_fingerprint(title, source)
    cross_fp = title_fingerprint(title)
    norm = normalize_title(title)
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    with get_session() as session:
        rows = session.exec(
            select(Draft, Headline)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(Draft.created_at >= cutoff)
            .where(Draft.status.in_(_DEDUP_STATUSES))
        ).all()

        for _draft, headline in rows:
            if story_fingerprint(headline.title, headline.source) == source_fp:
                return True
            if headline.title_fp and headline.title_fp == cross_fp:
                return True
            if norm:
                other = normalize_title(headline.title)
                if other and fuzz.ratio(norm, other) >= INGEST_TITLE_FUZZY_THRESHOLD:
                    return True

    return False
