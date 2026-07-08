"""Cross-source ingest dedup — skip stories already seen from another feed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import select

from config import INGEST_DEDUP_HOURS, INGEST_TITLE_FUZZY_THRESHOLD
from database import get_session
from models import Headline
from pipeline.story_key import normalize_title, title_fingerprint, titles_similar


@dataclass
class IngestDedupIndex:
    """In-memory index of recent headlines for fast cross-source dedup."""

    url_hashes: set[str] = field(default_factory=set)
    title_fps: set[str] = field(default_factory=set)
    normalized_titles: list[str] = field(default_factory=list)

    def is_duplicate(
        self,
        title: str,
        url_hash: str,
        *,
        fuzzy_threshold: int = INGEST_TITLE_FUZZY_THRESHOLD,
    ) -> str | None:
        if url_hash in self.url_hashes:
            return "duplicate url"

        fp = title_fingerprint(title)
        if fp in self.title_fps:
            return "cross-source title match"

        norm = normalize_title(title)
        if norm:
            for existing in self.normalized_titles:
                if titles_similar(norm, existing, threshold=fuzzy_threshold):
                    return "cross-source fuzzy title"

        return None

    def add(self, title: str, url_hash: str) -> None:
        self.url_hashes.add(url_hash)
        self.title_fps.add(title_fingerprint(title))
        norm = normalize_title(title)
        if norm:
            self.normalized_titles.append(norm)


def load_ingest_dedup_index(hours: int | None = None) -> IngestDedupIndex:
    """Build dedup index from recent headlines in the database."""
    hours = hours if hours is not None else INGEST_DEDUP_HOURS
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    index = IngestDedupIndex()

    with get_session() as session:
        rows = session.exec(
            select(Headline).where(Headline.published_at >= cutoff)
        ).all()
        for row in rows:
            index.add(row.title, row.hash)

    return index
