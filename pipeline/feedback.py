"""Learn noise patterns from user rejections — skip similar headlines before LLM calls."""

from __future__ import annotations

import re
from datetime import datetime

from rapidfuzz import fuzz
from sqlmodel import select

from config import REJECTION_FUZZY_THRESHOLD, REJECTION_LEARN_THRESHOLD
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline, RejectionFeedback, RejectionNote

logger = setup_logging()

# Prefixes stripped before comparing / learning title shapes
_TITLE_PREFIX = re.compile(
    r"^(?:breaking|update|exclusive|live updates?|just in|watch|video):\s*",
    re.I,
)

# Short phrases extracted from rejected titles and turned into regex blocks
_PHRASE_EXTRACT = re.compile(
    r"\b("
    r"investors (?:await|watch|eye)|markets (?:rise|fall|slip|gain|drop|rally)|"
    r"what to (?:watch|know)|here'?s what|stocks (?:rise|fall|slip)|"
    r"wall street (?:rises|falls|waits)|traders (?:await|watch)|"
    r"ahead of (?:fed|fomc|earnings)|as markets (?:open|close)"
    r")\b",
    re.I,
)


def normalize_title(title: str) -> str:
    text = title.strip().lower()
    text = _TITLE_PREFIX.sub("", text)
    text = re.sub(r"[^\w\s$]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _derive_pattern(title: str) -> str | None:
    """Extract a reusable regex fragment from a rejected headline."""
    match = _PHRASE_EXTRACT.search(title)
    if match:
        return match.group(1).lower()
    # Fall back to first 5 words if title is short enough to be distinctive
    words = normalize_title(title).split()
    if 3 <= len(words) <= 8:
        return r"\b" + re.escape(" ".join(words[:5])) + r"\b"
    return None


def _compiled_learned_patterns(rows: list[RejectionFeedback]) -> list[re.Pattern]:
    patterns: list[re.Pattern] = []
    seen: set[str] = set()
    for row in rows:
        if row.pattern and row.pattern not in seen:
            seen.add(row.pattern)
            try:
                patterns.append(re.compile(row.pattern, re.I))
            except re.error:
                continue
    return patterns


def record_rejection(draft_id: int, reason: str = "other", note: str = "") -> None:
    """Increment learned-noise stats and store rejection reason for drafter hints."""
    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if not draft:
            return
        headline = session.get(Headline, draft.headline_id)
        if not headline:
            return

        session.add(
            RejectionNote(
                draft_id=draft_id,
                reason=reason or "other",
                note=(note or "")[:500],
                draft_text_sample=draft.text[:280],
            )
        )

        norm = normalize_title(headline.title)
        if not norm:
            return

        row = session.exec(
            select(RejectionFeedback).where(RejectionFeedback.normalized_title == norm)
        ).first()

        now = datetime.utcnow()
        if row:
            row.reject_count += 1
            row.last_rejected_at = now
            row.title_sample = headline.title[:240]
        else:
            row = RejectionFeedback(
                normalized_title=norm,
                title_sample=headline.title[:240],
                reject_count=1,
                pattern=None,
                last_rejected_at=now,
            )
            session.add(row)

        if row.reject_count >= REJECTION_LEARN_THRESHOLD and not row.pattern:
            derived = _derive_pattern(headline.title)
            if derived:
                row.pattern = derived
                logger.info(
                    "Learned noise pattern from %d rejections: %s",
                    row.reject_count,
                    derived,
                )
        elif row.reject_count == 1 and not row.pattern and _PHRASE_EXTRACT.search(headline.title):
            derived = _derive_pattern(headline.title)
            if derived:
                row.pattern = derived
                logger.info("Learned wire-noise pattern on first reject: %s", derived)

        session.add(row)
        session.commit()


def is_learned_noise(title: str) -> str | None:
    """Return discard reason if title matches a learned rejection pattern."""
    norm = normalize_title(title)
    if not norm:
        return None

    with get_session() as session:
        rows = session.exec(
            select(RejectionFeedback).where(
                RejectionFeedback.reject_count >= REJECTION_LEARN_THRESHOLD
            )
        ).all()

        if not rows:
            return None

        for row in rows:
            if row.normalized_title == norm:
                return f"learned rejection ({row.reject_count}x): exact title match"
            if fuzz.ratio(norm, row.normalized_title) >= REJECTION_FUZZY_THRESHOLD:
                return f"learned rejection ({row.reject_count}x): similar to past reject"

        for pattern in _compiled_learned_patterns(rows):
            if pattern.search(title):
                return f"learned rejection pattern: {pattern.pattern}"

    return None


_REASON_HINTS = {
    "too_vague": "User rejected vague posts — be specific with company + action + number.",
    "wrong_ticker": "User rejected wrong ticker — verify tickers match the headline subject.",
    "bad_hook": "User rejected weak hooks — line 1 must be scroll-stopping, not generic.",
    "too_long": "User rejected long posts — keep to 3 body lines, under 300 characters.",
    "off_topic": "User rejected off-topic angle — stay on the headline story only.",
    "listicle": "User rejected listicle/roundup tone — never write stock-pick listicles.",
    "other": "User rejected similar posts — improve specificity and hook.",
}


def drafter_feedback_hints(limit: int = 8) -> str:
    """Recent rejection reasons for the Sonnet drafter prompt."""
    with get_session() as session:
        notes = session.exec(
            select(RejectionNote).order_by(RejectionNote.created_at.desc()).limit(limit)
        ).all()

    if not notes:
        return ""

    lines: list[str] = []
    seen: set[str] = set()
    for note in notes:
        key = note.reason
        if key in seen:
            continue
        seen.add(key)
        hint = _REASON_HINTS.get(key, _REASON_HINTS["other"])
        lines.append(f"- {hint}")
        if note.note:
            lines.append(f"  Example note: {note.note[:120]}")
    return "\n".join(lines)


def feedback_stats() -> dict:
    """Summary for settings / pipeline status."""
    with get_session() as session:
        rows = session.exec(select(RejectionFeedback)).all()
        learned = [r for r in rows if r.reject_count >= REJECTION_LEARN_THRESHOLD]
        return {
            "tracked_titles": len(rows),
            "learned_patterns": len(learned),
            "samples": [r.title_sample for r in learned[:5]],
        }


def backfill_from_rejected_drafts() -> int:
    """Seed feedback table from existing rejected drafts (runs once when empty)."""
    with get_session() as session:
        existing = session.exec(select(RejectionFeedback).limit(1)).first()
        if existing:
            return 0

        rows = session.exec(
            select(Draft, Headline)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(Draft.status == "rejected")
        ).all()

        counts: dict[str, tuple[str, int]] = {}
        for draft, headline in rows:
            norm = normalize_title(headline.title)
            if not norm:
                continue
            sample, count = counts.get(norm, (headline.title, 0))
            counts[norm] = (sample, count + 1)

        added = 0
        for norm, (sample, count) in counts.items():
            pattern = _derive_pattern(sample) if count >= REJECTION_LEARN_THRESHOLD else None
            session.add(
                RejectionFeedback(
                    normalized_title=norm,
                    title_sample=sample[:240],
                    reject_count=count,
                    pattern=pattern,
                    last_rejected_at=datetime.utcnow(),
                )
            )
            added += 1

        if added:
            session.commit()
            logger.info("Backfilled %d rejection feedback rows", added)
        return added
