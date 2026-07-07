"""Cache Haiku filter results to avoid re-classifying the same story."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from config import CLASSIFICATION_CACHE_HOURS
from database import get_session
from logging_config import setup_logging
from models import ClassificationCache
from pipeline.story_key import story_fingerprint

logger = setup_logging()


def get_cached_classification(title: str, source: str, hours: int | None = None) -> dict | None:
    """Return cached classification if fresh, else None."""
    hours = hours if hours is not None else CLASSIFICATION_CACHE_HOURS
    fp = story_fingerprint(title, source)
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    with get_session() as session:
        row = session.get(ClassificationCache, fp)
        if not row or row.created_at < cutoff:
            return None
        try:
            return json.loads(row.result_json)
        except json.JSONDecodeError:
            return None


def cache_classification(title: str, source: str, classification: dict) -> None:
    """Store a classification result for reuse."""
    fp = story_fingerprint(title, source)
    payload = json.dumps(classification)

    with get_session() as session:
        row = session.get(ClassificationCache, fp)
        if row:
            row.result_json = payload
            row.created_at = datetime.utcnow()
            session.add(row)
        else:
            session.add(
                ClassificationCache(
                    fingerprint=fp,
                    result_json=payload,
                    created_at=datetime.utcnow(),
                )
            )
        session.commit()


def prune_classification_cache(max_age_hours: int = 48) -> int:
    """Remove stale cache rows. Returns number deleted."""
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    deleted = 0

    with get_session() as session:
        from sqlmodel import select

        rows = session.exec(
            select(ClassificationCache).where(ClassificationCache.created_at < cutoff)
        ).all()
        for row in rows:
            session.delete(row)
            deleted += 1
        if deleted:
            session.commit()

    if deleted:
        logger.debug("Pruned %d classification cache entries", deleted)
    return deleted
