"""Normalized story keys for cross-source dedup and classification cache."""

from __future__ import annotations

import hashlib
import re

# Strip wire prefixes and source attributions before comparing titles across feeds
_TITLE_PREFIX = re.compile(
    r"^(?:breaking|update|exclusive|live updates?|just in|watch|video):\s*",
    re.I,
)
_SOURCE_PREFIX = re.compile(
    r"^(?:reuters|bloomberg|cnbc|wsj|marketwatch|yahoo finance|financial times)"
    r"\s*[-–—|:]\s*",
    re.I,
)


def normalize_title(title: str) -> str:
    """Normalize headline text for cross-source comparison."""
    text = title.strip().lower()
    text = _TITLE_PREFIX.sub("", text)
    text = _SOURCE_PREFIX.sub("", text)
    text = re.sub(r"[^\w\s$%.\-']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def title_fingerprint(title: str) -> str:
    """Cross-source key — same story from CNBC/Bloomberg/Yahoo maps to one hash."""
    normalized = normalize_title(title)
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def story_fingerprint(title: str, source: str) -> str:
    """Per-source key for draft dedup and classification cache."""
    normalized = normalize_title(title)
    raw = f"{source.strip().lower()}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
