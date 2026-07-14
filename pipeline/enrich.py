"""Fetch and extract full article text for richer drafting."""

from __future__ import annotations

import io
import re
from urllib.parse import urlparse

import httpx

from config import ARTICLE_FETCH_CATEGORIES, MIN_SUMMARY_CHARS_FOR_SKIP_FETCH, SEC_USER_AGENT
from logging_config import setup_logging
from models import Headline
from pipeline.url_resolve import resolve_article_url

logger = setup_logging()

MAX_ARTICLE_CHARS = 5000
MAX_PRESS_CHARS = 12000
FETCH_TIMEOUT = 18.0

SKIP_FETCH_DOMAINS = {
    "twitter.com", "x.com", "youtube.com", "reddit.com",
}

_PDF_CT = re.compile(r"application/pdf", re.I)


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

    if classification.get("impact") == "high":
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


def _looks_like_pdf_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or ".pdf?" in url.lower() or "/pdf/" in path


def fetch_pdf_text(url: str, *, max_chars: int = MAX_PRESS_CHARS) -> str:
    """Download a PDF and extract text (press releases, IR filings)."""
    try:
        with httpx.Client(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": SEC_USER_AGENT},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()
            if not (_PDF_CT.search(content_type) or _looks_like_pdf_url(url) or resp.content[:5] == b"%PDF-"):
                return ""
            data = resp.content
    except Exception as exc:
        logger.debug("PDF download failed %s: %s", url[:80], exc)
        return ""

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for page in reader.pages[:25]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(pages)
        cleaned = _clean_text(text, max_chars=max_chars)
        if len(cleaned) > 200:
            logger.info("PDF extract %d chars from %s", len(cleaned), url[:80])
        return cleaned
    except Exception as exc:
        logger.debug("PDF parse failed %s: %s", url[:80], exc)
        return ""


def fetch_article_text(url: str, *, max_chars: int | None = None) -> str:
    """Return extracted article body text (HTML or PDF), or empty string on failure."""
    url = resolve_article_url(url)
    if not url or not url.startswith("http"):
        return ""

    max_chars = max_chars if max_chars is not None else MAX_ARTICLE_CHARS

    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if any(host == d or host.endswith(f".{d}") for d in SKIP_FETCH_DOMAINS):
            return ""
    except Exception:
        pass

    if _looks_like_pdf_url(url):
        return fetch_pdf_text(url, max_chars=max_chars)

    try:
        import trafilatura

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return _fallback_fetch(url, max_chars=max_chars)

        # trafilatura sometimes gets PDF binary as "html"
        if isinstance(downloaded, str) and downloaded.startswith("%PDF-"):
            return fetch_pdf_text(url, max_chars=max_chars)

        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        if text:
            return _clean_text(text, max_chars=max_chars)
        return _fallback_fetch(url, max_chars=max_chars)
    except Exception as e:
        logger.debug("Article extract failed for %s: %s", url[:80], e)
        return _fallback_fetch(url, max_chars=max_chars)


def _fallback_fetch(url: str, *, max_chars: int = MAX_ARTICLE_CHARS) -> str:
    """Basic HTML strip fallback, with PDF detection via Content-Type."""
    try:
        with httpx.Client(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": SEC_USER_AGENT},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()
            if _PDF_CT.search(content_type) or resp.content[:5] == b"%PDF-":
                return fetch_pdf_text(url, max_chars=max_chars)
            html = resp.text
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return _clean_text(text, max_chars=max_chars)
    except Exception as e:
        logger.debug("Fallback fetch failed for %s: %s", url[:80], e)
        return ""


def _clean_text(text: str, *, max_chars: int = MAX_ARTICLE_CHARS) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]
