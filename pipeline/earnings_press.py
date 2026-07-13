"""Fetch official earnings press releases — Finnhub + company IR site."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from config import EARNINGS_PRESS_DAYS_BACK, SEC_USER_AGENT, WEB_SEARCH_ENABLED
from logging_config import setup_logging
from pipeline.enrich import fetch_article_text
from pipeline.finnhub_api import finnhub_get, get_finnhub_key
from pipeline.web_search import search_news

logger = setup_logging()

_EARNINGS_PRESS_TITLE = re.compile(
    r"\b("
    r"earnings|quarterly results|financial results|q[1-4]|"
    r"reports (?:first|second|third|fourth)|fiscal"
    r")\b",
    re.I,
)
_IR_HOST_HINTS = ("investor", "ir.", "investors.", "newsroom", "media.")


def get_company_profile(symbol: str) -> dict[str, Any]:
    if not get_finnhub_key():
        return {}
    data, err = finnhub_get("stock/profile2", {"symbol": symbol.upper()})
    if err or not isinstance(data, dict):
        return {}
    return data


def get_company_website(symbol: str) -> str | None:
    profile = get_company_profile(symbol)
    for key in ("weburl", "website", "url"):
        value = (profile.get(key) or "").strip()
        if value.startswith("http"):
            return value
    return None


def _press_release_items(symbol: str, days_back: int) -> list[dict[str, Any]]:
    if not get_finnhub_key():
        return []

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=days_back)).isoformat()
    to_date = today.isoformat()
    data, err = finnhub_get(
        "press-releases",
        {"symbol": symbol.upper(), "from": from_date, "to": to_date},
    )
    if err or not data:
        return []

    if isinstance(data, dict):
        items = data.get("majorDevelopment") or data.get("pressReleases") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    else:
        return []

    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("headline") or item.get("title") or "").strip()
        url = (item.get("url") or item.get("link") or "").strip()
        if not title:
            continue
        rows.append(
            {
                "title": title,
                "url": url,
                "datetime": item.get("datetime") or item.get("date"),
                "description": (item.get("description") or item.get("text") or "").strip(),
            }
        )
    return rows


def _quarter_label(quarter: int | None) -> str:
    return f"Q{quarter}" if quarter else ""


def _matches_earnings_release(title: str, quarter: int | None, year: int | None) -> bool:
    if not _EARNINGS_PRESS_TITLE.search(title):
        return False
    if quarter:
        q_pat = re.compile(rf"\bQ{quarter}\b", re.I)
        if not q_pat.search(title) and "quarter" not in title.lower():
            # Allow generic "quarterly results" without explicit Q label
            pass
    if year:
        if str(year) not in title:
            pass
    return True


def _score_press_row(row: dict[str, Any], quarter: int | None, year: int | None) -> int:
    title = row.get("title") or ""
    score = 0
    if _EARNINGS_PRESS_TITLE.search(title):
        score += 40
    if quarter and re.search(rf"\bQ{quarter}\b", title, re.I):
        score += 30
    if year and str(year) in title:
        score += 15
    url = (row.get("url") or "").lower()
    if any(hint in url for hint in _IR_HOST_HINTS):
        score += 25
    if "results" in title.lower():
        score += 10
    return score


def fetch_finnhub_press_release(
    symbol: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
    days_back: int | None = None,
) -> tuple[str, str, str]:
    """Return (article_text, url, title) from the best matching Finnhub press release."""
    days_back = days_back if days_back is not None else EARNINGS_PRESS_DAYS_BACK
    rows = _press_release_items(symbol, days_back)
    if not rows:
        return "", "", ""

    ranked = sorted(rows, key=lambda row: _score_press_row(row, quarter, year), reverse=True)
    for row in ranked[:6]:
        title = row.get("title") or ""
        if not _matches_earnings_release(title, quarter, year):
            continue
        url = (row.get("url") or "").strip()
        description = row.get("description") or ""
        article = ""
        if url.startswith("http"):
            article = fetch_article_text(url)
        if not article and len(description) > 300:
            article = description
        if article and len(article) > 250:
            logger.info("Earnings press release %s: %s", symbol, title[:80])
            return article[:8000], url, title
    return "", "", ""


def _ir_search_queries(symbol: str, weburl: str | None, quarter: int | None, year: int | None) -> list[str]:
    q = _quarter_label(quarter)
    year_s = str(year) if year else ""
    queries: list[str] = []

    if weburl:
        try:
            host = urlparse(weburl).netloc
            if host:
                queries.append(
                    f"site:{host} {symbol} {q} earnings press release {year_s}".strip()
                )
                if "investor" not in host and "ir." not in host:
                    base = host.replace("www.", "")
                    queries.append(
                        f"site:investor.{base} {symbol} {q} earnings results {year_s}".strip()
                    )
        except Exception:
            pass

    queries.append(
        f'"{symbol}" investor relations {q} earnings press release {year_s}'.strip()
    )
    return queries[:3]


def fetch_ir_earnings_press(
    symbol: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
) -> tuple[str, str, str]:
    """Search company IR site via Google News RSS, then fetch the press release body."""
    if not WEB_SEARCH_ENABLED:
        return "", "", ""

    weburl = get_company_website(symbol)
    seen: set[str] = set()

    for query in _ir_search_queries(symbol, weburl, quarter, year):
        batch = search_news(
            query,
            source_label="IR · earnings press",
            limit=5,
            recency="7d",
        )
        for item in batch:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            if not title or not url or url in seen:
                continue
            seen.add(url)
            host = urlparse(url).netloc.lower()
            if not any(hint in host for hint in _IR_HOST_HINTS) and weburl:
                company_host = urlparse(weburl).netloc.lower().replace("www.", "")
                if company_host and company_host not in host:
                    continue
            if not _EARNINGS_PRESS_TITLE.search(title):
                continue
            article = fetch_article_text(url)
            if article and len(article) > 300:
                logger.info("IR earnings press %s: %s", symbol, title[:80])
                return article[:8000], url, title
    return "", "", ""


def fetch_earnings_press_release(
    symbol: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
) -> tuple[str, str, str]:
    """Best-effort official earnings release: Finnhub press API, then IR site search."""
    text, url, title = fetch_finnhub_press_release(symbol, quarter=quarter, year=year)
    if text:
        return text, url, title
    return fetch_ir_earnings_press(symbol, quarter=quarter, year=year)


def fetch_press_html(url: str) -> str:
    """Raw HTML for bullet-list extraction."""
    if not url or not url.startswith("http"):
        return ""
    try:
        with httpx.Client(
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": SEC_USER_AGENT},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text[:500_000]
    except Exception as exc:
        logger.debug("Press HTML fetch failed %s: %s", url[:80], exc)
        return ""
