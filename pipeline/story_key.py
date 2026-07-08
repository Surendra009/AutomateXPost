"""Normalized story keys for cross-source dedup and classification cache."""

from __future__ import annotations

import hashlib
import re

from rapidfuzz import fuzz

from config import INGEST_TITLE_FUZZY_THRESHOLD

# Strip wire prefixes and source attributions before comparing titles across feeds
_TITLE_PREFIX = re.compile(
    r"^(?:breaking|update|exclusive|live updates?|just in|watch|video):\s*",
    re.I,
)
_SOURCE_PREFIX = re.compile(
    r"^(?:reuters|bloomberg|cnbc|wsj|marketwatch|yahoo finance|financial times|"
    r"associated press|ap news|the wall street journal)"
    r"\s*[-–—|:]\s*",
    re.I,
)
_SOURCE_SUFFIX = re.compile(
    r"\s*[-–—|:]\s*(?:reuters|bloomberg|cnbc|wsj|marketwatch|yahoo finance|"
    r"financial times|associated press|ap news|the wall street journal)\s*$",
    re.I,
)
_STOPWORDS = frozenset(
    "a an the and or but in on at to for of is are was be by as it its from with "
    "says said after over into out up down that this these those than then not".split()
)
# Map wire variants to one token so cross-source headlines overlap
_SYNONYM_GROUPS = (
    frozenset({"strike", "strikes", "attack", "attacks", "hit", "hits", "missile", "missiles", "fire", "fires"}),
    frozenset({"surge", "surges", "jump", "jumps", "rise", "rises", "climb", "climbs", "soar", "soars"}),
    frozenset({"oil", "crude", "energy", "petroleum"}),
    frozenset({"target", "targets", "base", "bases", "facility", "facilities", "site", "sites"}),
    frozenset({"price", "prices", "pricing"}),
    frozenset({"launch", "launches", "launched"}),
)


def normalize_title(title: str) -> str:
    """Normalize headline text for cross-source comparison."""
    text = title.strip().lower()
    text = _TITLE_PREFIX.sub("", text)
    text = _SOURCE_PREFIX.sub("", text)
    text = _SOURCE_SUFFIX.sub("", text)
    text = re.sub(r"\bu\.s\.?\b", "us", text)
    text = re.sub(r"[^\w\s$%.\-']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _significant_tokens(text: str) -> set[str]:
    raw = {
        w
        for w in text.split()
        if len(w) > 2 and w not in _STOPWORDS and not w.isdigit()
    }
    canonical: set[str] = set()
    for token in raw:
        for group in _SYNONYM_GROUPS:
            if token in group:
                canonical.add(next(iter(group)))
                break
        else:
            canonical.add(token)
    return canonical


def titles_similar(
    a: str,
    b: str,
    *,
    threshold: int | None = None,
) -> bool:
    """True when two headlines likely describe the same story (cross-wire)."""
    threshold = threshold if threshold is not None else INGEST_TITLE_FUZZY_THRESHOLD
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    fuzzy_score = max(
        fuzz.token_set_ratio(na, nb),
        fuzz.partial_ratio(na, nb),
        fuzz.WRatio(na, nb),
    )
    if fuzzy_score >= threshold:
        return True

    tokens_a = _significant_tokens(na)
    tokens_b = _significant_tokens(nb)
    if not tokens_a or not tokens_b:
        return False

    overlap = tokens_a & tokens_b
    if len(overlap) < 3:
        return False

    smaller = min(len(tokens_a), len(tokens_b))
    if smaller >= 3 and len(overlap) / smaller >= 0.5:
        return True

    union = tokens_a | tokens_b
    return len(overlap) / len(union) >= 0.4


def title_fingerprint(title: str) -> str:
    """Cross-source key — same story from CNBC/Bloomberg/Yahoo maps to one hash."""
    normalized = normalize_title(title)
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def story_fingerprint(title: str, source: str) -> str:
    """Per-source key for draft dedup and classification cache."""
    normalized = normalize_title(title)
    raw = f"{source.strip().lower()}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
