"""Shared helpers for posting new drafts to external channels."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import select

from config import MAX_CHANNEL_DRAFTS_PER_CYCLE
from database import get_session
from models import Draft, Headline


def drafts_since(since: datetime, limit: int | None = None) -> list[tuple[Draft, Headline | None]]:
    """Pending drafts created since a pipeline cycle started, highest impact first."""
    cap = limit if limit is not None else MAX_CHANNEL_DRAFTS_PER_CYCLE
    with get_session() as session:
        drafts = list(
            session.exec(
                select(Draft)
                .where(Draft.status == "pending", Draft.created_at >= since)
                .order_by(Draft.created_at.desc())
            ).all()
        )
        if not drafts:
            return []

        headline_ids = [d.headline_id for d in drafts if d.headline_id]
        headlines: dict[int, Headline] = {}
        if headline_ids:
            rows = session.exec(select(Headline).where(Headline.id.in_(headline_ids))).all()
            headlines = {h.id: h for h in rows if h.id is not None}

        impact_rank = {"high": 3, "med": 2, "low": 1}
        drafts.sort(
            key=lambda d: (impact_rank.get(d.impact or "med", 2), d.created_at or since),
            reverse=True,
        )

        pairs: list[tuple[Draft, Headline | None]] = []
        for draft in drafts[:cap]:
            headline = headlines.get(draft.headline_id) if draft.headline_id else None
            pairs.append((draft, headline))
        return pairs
