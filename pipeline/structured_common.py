"""Shared helpers for zero-LLM structured API drafts."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlmodel import select

from database import get_session
from models import Draft, Headline
from pipeline.dedup import was_recently_drafted
from pipeline.draft_budget import DraftBudget
from pipeline.story_key import title_fingerprint


def content_hash(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def headline_exists(chash: str) -> bool:
    with get_session() as session:
        return session.exec(select(Headline).where(Headline.hash == chash)).first() is not None


def save_structured_draft(
    *,
    source: str,
    url: str,
    title: str,
    summary: str,
    draft_text: str,
    tickers: str,
    category: str,
    impact: str,
    fmt: str,
    confidence: float,
    chash: str,
    published_at: datetime | None = None,
    budget: DraftBudget | None = None,
) -> bool:
    """Insert headline + pending draft. Returns True if created."""
    if budget is not None and budget.remaining <= 0:
        return False
    if was_recently_drafted(title, source):
        return False
    if headline_exists(chash):
        return False

    now = published_at or datetime.utcnow()
    with get_session() as session:
        headline = Headline(
            source=source,
            url=url,
            title=title,
            summary=summary,
            published_at=now,
            hash=chash,
            title_fp=title_fingerprint(title),
            status="drafted",
        )
        session.add(headline)
        session.flush()

        draft = Draft(
            headline_id=headline.id,
            text=draft_text,
            format=fmt,
            impact=impact,
            category=category,
            tickers=tickers,
            confidence=confidence,
            status="pending",
            created_at=datetime.utcnow(),
        )
        session.add(draft)
        session.commit()

    if budget is not None:
        budget.try_take(1)
    return True
