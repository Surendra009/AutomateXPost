"""Skip drafting when the same story was recently drafted."""

from datetime import datetime, timedelta

from sqlmodel import select

from config import DRAFT_DEDUP_HOURS, INGEST_DEDUP_HOURS, INGEST_TITLE_FUZZY_THRESHOLD
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.dedup_mode import dedup_before_draft
from pipeline.story_key import normalize_title, story_fingerprint, title_fingerprint, titles_similar

logger = setup_logging()

_DEDUP_STATUSES = ("pending", "posted", "approved", "rejected", "stale")
_ACTIVE_DRAFT_STATUSES = ("pending", "scheduled")
_QUEUE_HOOK_FUZZY = 85

# Titles drafted successfully during the current pipeline cycle (in-memory).
_cycle_drafted_titles: list[str] = []


def reset_cycle_dedup() -> None:
    """Clear in-cycle draft tracker at the start of each pipeline run."""
    _cycle_drafted_titles.clear()


def mark_story_drafted(title: str) -> None:
    """Record a story drafted this cycle so later paths skip cross-source dupes."""
    norm = normalize_title(title)
    if norm:
        _cycle_drafted_titles.append(norm)


def drafted_this_cycle(title: str) -> bool:
    """True if a similar story was already drafted in the current pipeline cycle."""
    norm = normalize_title(title)
    if not norm:
        return False
    for existing in _cycle_drafted_titles:
        if titles_similar(norm, existing, threshold=INGEST_TITLE_FUZZY_THRESHOLD):
            return True
    return False


def should_skip_story(title: str, source: str, hours: int | None = None) -> bool:
    """Unified gate for all draft creation paths."""
    if not dedup_before_draft():
        return False
    if drafted_this_cycle(title):
        return True
    return was_recently_drafted(title, source, hours=hours)


def _draft_hook(text: str) -> str:
    import re

    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if all(re.fullmatch(r"\$[A-Z]{1,5}", t) for t in ln.split()):
            continue
        return normalize_title(ln)
    return ""


def _hooks_match(title: str, draft_text: str) -> bool:
    hook = _draft_hook(draft_text)
    norm_title = normalize_title(title)
    if not hook or not norm_title:
        return False
    return titles_similar(hook, norm_title, threshold=_QUEUE_HOOK_FUZZY)


def _title_fp_for_headline(headline: Headline) -> str:
    return headline.title_fp or title_fingerprint(headline.title)


def story_has_active_draft(title: str) -> bool:
    """True if this story already has a pending or scheduled draft in the queue."""
    cross_fp = title_fingerprint(title)
    norm = normalize_title(title)

    with get_session() as session:
        rows = session.exec(
            select(Draft, Headline)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(Draft.status.in_(_ACTIVE_DRAFT_STATUSES))
        ).all()

        for _draft, headline in rows:
            if _title_fp_for_headline(headline) == cross_fp:
                return True
            if _hooks_match(title, _draft.text):
                return True
            if norm and titles_similar(title, headline.title):
                return True
    return False


def title_recently_ingested(title: str, hours: int | None = None) -> bool:
    """True if a headline with the same story was ingested recently."""
    hours = hours if hours is not None else INGEST_DEDUP_HOURS
    cross_fp = title_fingerprint(title)
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    with get_session() as session:
        rows = session.exec(
            select(Headline).where(Headline.published_at >= cutoff)
        ).all()

        for headline in rows:
            if _title_fp_for_headline(headline) == cross_fp:
                return True
            if titles_similar(title, headline.title):
                return True
    return False


def was_recently_drafted(title: str, source: str, hours: int | None = None) -> bool:
    """True if this story (including cross-source matches) got a draft recently."""
    if not dedup_before_draft():
        return False
    if story_has_active_draft(title):
        return True

    hours = hours if hours is not None else DRAFT_DEDUP_HOURS
    source_fp = story_fingerprint(title, source)
    cross_fp = title_fingerprint(title)
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
            headline_fp = headline.title_fp or title_fingerprint(headline.title)
            if headline_fp == cross_fp:
                return True
            if _hooks_match(title, _draft.text):
                return True
            if titles_similar(title, headline.title):
                return True

    return False
