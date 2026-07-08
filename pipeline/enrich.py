"""Fetch and extract full article text for richer drafting."""

import re

import httpx

from config import ARTICLE_FETCH_CATEGORIES, MIN_SUMMARY_CHARS_FOR_SKIP_FETCH
from logging_config import setup_logging
from models import Headline
from pipeline.url_resolve import resolve_article_url

logger = setup_logging()

MAX_ARTICLE_CHARS = 4000
FETCH_TIMEOUT = 12.0

SKIP_FETCH_DOMAINS = {
    "twitter.com", "x.com", "youtube.com", "reddit.com",
}


def should_fetch_article(headline: Headline, classification: dict) -> bool:
    """Return True if we need the full article body for drafting."""
    category = classification.get("category", "other")
    if category in ARTICLE_FETCH_CATEGORIES:
        return True

    summary = (headline.summary or "").strip()
    if len(summary) < MIN_SUMMARY_CHARS_FOR_SKIP_FETCH:
        return True

    if headline.source == "SEC EDGAR 8-K" and len(summary) < 200:
        return True

    return False


def get_article_text_for_draft(headline: Headline, classification: dict) -> str:
    """Fetch article only when RSS summary alone isn't enough."""
    if not should_fetch_article(headline, classification):
        logger.debug(
            "Skipping article fetch for headline %s (summary=%d chars, category=%s)",
            headline.id,
            len(headline.summary or ""),
            classification.get("category"),
        )
        return ""

    return fetch_article_text(headline.url)


def fetch_article_text(url: str) -> str:
    """Return extracted article body text, or empty string on failure."""
    url = resolve_article_url(url)
    if not url or not url.startswith("http"):
        return ""

    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if any(host == d or host.endswith(f".{d}") for d in SKIP_FETCH_DOMAINS):
            return ""
    except Exception:
        pass

    try:
        import trafilatura

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return _fallback_fetch(url)

        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        if text:
            return _clean_text(text)
        return _fallback_fetch(url)
    except Exception as e:
        logger.debug("Article extract failed for %s: %s", url[:80], e)
        return _fallback_fetch(url)


def _fallback_fetch(url: str) -> str:
    """Basic HTML strip fallback when trafilatura fails."""
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "PostPilot/1.0 (news bot)"})
            resp.raise_for_status()
            html = resp.text
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return _clean_text(text)
    except Exception as e:
        logger.debug("Fallback fetch failed for %s: %s", url[:80], e)
        return ""


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_ARTICLE_CHARS]
