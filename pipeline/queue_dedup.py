"""Collapse duplicate pending drafts before showing the queue."""

from __future__ import annotations

from sqlmodel import select

from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.story_key import title_fingerprint

logger = setup_logging()


def _story_key(headline: Headline) -> str:
    if headline.title_fp:
        return headline.title_fp
    return title_fingerprint(headline.title)


def _pick_best(group: list[tuple[Draft, Headline]]) -> tuple[Draft, Headline]:
    impact_rank = {"high": 3, "med": 2, "low": 1}

    def score(item: tuple[Draft, Headline]) -> tuple:
        draft, headline = item
        return (
            float(draft.confidence or 0),
            impact_rank.get(draft.impact, 1),
            headline.published_at.timestamp() if headline.published_at else 0,
            draft.created_at.timestamp() if draft.created_at else 0,
        )

    return max(group, key=score)


def dedupe_pending_drafts(
    drafts: list[Draft],
    headlines: dict[int, Headline],
) -> tuple[list[tuple[Draft, Headline]], int]:
    """
    Keep the best pending draft per story key.
    Marks duplicate pending drafts as stale.
    Returns (visible pairs, hidden_count).
    """
    pairs: list[tuple[Draft, Headline]] = []
    for draft in drafts:
        headline = headlines.get(draft.headline_id)
        if headline:
            pairs.append((draft, headline))

    groups: dict[str, list[tuple[Draft, Headline]]] = {}
    for pair in pairs:
        key = _story_key(pair[1])
        groups.setdefault(key, []).append(pair)

    visible: list[tuple[Draft, Headline]] = []
    stale_ids: list[int] = []

    for group in groups.values():
        if len(group) == 1:
            visible.append(group[0])
            continue
        best = _pick_best(group)
        visible.append(best)
        for draft, _headline in group:
            if draft.id != best[0].id:
                stale_ids.append(draft.id)

    hidden = len(stale_ids)
    if stale_ids:
        with get_session() as session:
            rows = session.exec(select(Draft).where(Draft.id.in_(stale_ids))).all()
            for row in rows:
                if row.status == "pending":
                    row.status = "stale"
                    session.add(row)
            session.commit()
        logger.info("Queue dedup: hid %d duplicate pending drafts", hidden)

    visible.sort(key=lambda x: x[0].created_at, reverse=True)
    return visible, hidden
