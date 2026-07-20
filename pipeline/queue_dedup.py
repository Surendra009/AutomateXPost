"""Collapse duplicate pending drafts before showing the queue."""

from __future__ import annotations

import re
from datetime import datetime

from rapidfuzz import fuzz
from sqlmodel import select

from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.earnings_dedup import earnings_group_key
from pipeline.story_key import normalize_title, title_fingerprint

logger = setup_logging()

_QUEUE_FUZZY_THRESHOLD = 85
_V2_SOURCE = "Pipeline v2"
_V2_LABEL_TITLE = re.compile(r"^([^:]{1,32}):\s")


def _group_key(draft: Draft, headline: Headline) -> str:
    earnings_key = earnings_group_key(draft)
    if earnings_key:
        return earnings_key
    v2_key = _v2_label_key(draft, headline)
    if v2_key:
        return v2_key
    return _story_key(headline)


def _v2_label_key(draft: Draft, headline: Headline) -> str | None:
    """One visible v2 draft per intent label per day.

    v2 titles like "Fed: <assertion>" embed an LLM paraphrase that changes
    every cycle, so title fingerprints never collide for the same story.
    """
    if headline.source != _V2_SOURCE:
        return None
    match = _V2_LABEL_TITLE.match(headline.title or "")
    if not match:
        return None
    day = (
        draft.created_at.date().isoformat()
        if draft.created_at
        else datetime.utcnow().date().isoformat()
    )
    return f"v2:{match.group(1).strip().lower()}:{day}"


def _story_key(headline: Headline) -> str:
    if headline.title_fp:
        return headline.title_fp
    return title_fingerprint(headline.title)


def _pick_best(group: list[tuple[Draft, Headline]]) -> tuple[Draft, Headline]:
    impact_rank = {"high": 3, "med": 2, "low": 1}

    def score(item: tuple[Draft, Headline]) -> tuple:
        draft, headline = item
        created = draft.created_at.timestamp() if draft.created_at else 0
        return (
            created,
            float(draft.confidence or 0),
            impact_rank.get(draft.impact, 1),
            headline.published_at.timestamp() if headline.published_at else 0,
        )

    return max(group, key=score)


def _merge_fuzzy_groups(
    groups: dict[str, list[tuple[Draft, Headline]]],
) -> dict[str, list[tuple[Draft, Headline]]]:
    """Merge groups whose headlines are fuzzy duplicates (same event, different wires)."""
    keys = list(groups.keys())
    merged: dict[str, list[tuple[Draft, Headline]]] = {}
    used: set[str] = set()

    for key_a in keys:
        if key_a in used:
            continue
        if key_a.startswith("earnings:"):
            merged[key_a] = list(groups[key_a])
            used.add(key_a)
            continue
        combined = list(groups[key_a])
        rep_title = normalize_title(groups[key_a][0][1].title)

        for key_b in keys:
            if key_b == key_a or key_b in used:
                continue
            other_title = normalize_title(groups[key_b][0][1].title)
            if fuzz.ratio(rep_title, other_title) >= _QUEUE_FUZZY_THRESHOLD:
                combined.extend(groups[key_b])
                used.add(key_b)

        merged[key_a] = combined
        used.add(key_a)

    return merged


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
        key = _group_key(pair[0], pair[1])
        groups.setdefault(key, []).append(pair)

    groups = _merge_fuzzy_groups(groups)

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
