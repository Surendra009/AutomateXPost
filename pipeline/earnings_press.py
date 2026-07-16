"""Fetch official earnings press releases — Finnhub + company IR site + PDF."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from config import EARNINGS_PRESS_DAYS_BACK, SEC_USER_AGENT, WEB_SEARCH_ENABLED
from logging_config import setup_logging
from pipeline.enrich import MAX_PRESS_CHARS, fetch_article_text
from pipeline.finnhub_api import finnhub_get, get_finnhub_key
from pipeline.web_search import search_news

logger = setup_logging()

_EARNINGS_PRESS_TITLE = re.compile(
    r"\b("
    r"earnings|quarterly results|financial results|q[1-4]|"
    r"reports (?:first|second|third|fourth)|fiscal|"
    r"net income|reports (?:record )?(?:first|second|third|fourth)?\s*quarter"
    r")\b",
    re.I,
)
_IR_HOST_HINTS = (
    "investor", "ir.", "investors.", "newsroom", "media.",
    "jpmorganchase", "bankofamerica", "wellsfargo", "citigroup",
    "goldmansachs", "morganstanley",
)
_PDF_HREF = re.compile(
    r'href=["\']([^"\']+\.pdf[^"\']*)["\']',
    re.I,
)
_EARNINGS_HREF = re.compile(
    r'href=["\']([^"\']*(?:earnings|quarterly|financial[-_]?results|press[-_]?release)[^"\']*)["\']',
    re.I,
)

# Direct IR landing pages for large caps (banks + mega tech) where PDF is common.
KNOWN_IR_PAGES: dict[str, str] = {
    "JPM": "https://www.jpmorganchase.com/ir",
    "BAC": "https://investor.bankofamerica.com",
    "WFC": "https://www.wellsfargo.com/about/investor-relations/",
    "C": "https://www.citigroup.com/global/investors",
    "GS": "https://www.goldmansachs.com/investor-relations/",
    "MS": "https://www.morganstanley.com/about-us-ir",
    "AAPL": "https://investor.apple.com",
    "MSFT": "https://www.microsoft.com/en-us/investor",
    "GOOGL": "https://abc.xyz/investor/",
    "GOOG": "https://abc.xyz/investor/",
    "AMZN": "https://ir.aboutamazon.com",
    "META": "https://investor.atmeta.com",
    "NVDA": "https://investor.nvidia.com",
    "TSLA": "https://ir.tesla.com",
    "NFLX": "https://ir.netflix.net",
}


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
    # Reject titles that name a different quarter/year than the print we're drafting
    title_q = re.search(r"\bQ([1-4])\b", title, re.I)
    if quarter and title_q and int(title_q.group(1)) != int(quarter):
        return False
    title_years = [int(y) for y in re.findall(r"\b(20\d{2})\b", title)]
    if year and title_years and int(year) not in title_years:
        # Allow titles with no conflicting year; reject explicit wrong years
        return False
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
    if url.endswith(".pdf") or ".pdf" in url:
        score += 20
    if "results" in title.lower() or "earnings" in title.lower():
        score += 10
    return score


def _fetch_release_body(url: str, description: str = "") -> str:
    """Fetch HTML or PDF body; fall back to Finnhub description."""
    article = ""
    if url.startswith("http"):
        article = fetch_article_text(url, max_chars=MAX_PRESS_CHARS)
    if not article and len(description) > 300:
        article = description
    return article


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
    for row in ranked[:8]:
        title = row.get("title") or ""
        if not _matches_earnings_release(title, quarter, year):
            continue
        url = (row.get("url") or "").strip()
        description = row.get("description") or ""
        article = _fetch_release_body(url, description)
        if article and len(article) > 250:
            logger.info("Earnings press release %s: %s", symbol, title[:80])
            return article[:MAX_PRESS_CHARS], url, title
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
                base = host.replace("www.", "")
                if "investor" not in host and "ir." not in host:
                    queries.append(
                        f"site:investor.{base} {symbol} {q} earnings results {year_s}".strip()
                    )
        except Exception:
            pass

    known = KNOWN_IR_PAGES.get(symbol.upper())
    if known:
        try:
            host = urlparse(known).netloc
            if host:
                queries.append(
                    f"site:{host} {symbol} {q} earnings results {year_s}".strip()
                )
        except Exception:
            pass

    queries.append(
        f'"{symbol}" investor relations {q} earnings press release PDF {year_s}'.strip()
    )
    queries.append(
        f'"{symbol}" {q} "financial results" OR "earnings results" {year_s}'.strip()
    )
    return queries[:5]


def _looks_ir_or_company(url: str, weburl: str | None) -> bool:
    host = urlparse(url).netloc.lower()
    if any(hint in host for hint in _IR_HOST_HINTS):
        return True
    if weburl:
        company_host = urlparse(weburl).netloc.lower().replace("www.", "")
        if company_host and company_host in host:
            return True
    return False


def fetch_ir_earnings_press(
    symbol: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
) -> tuple[str, str, str]:
    """Search company IR site via Google News RSS, then fetch HTML or PDF body."""
    if not WEB_SEARCH_ENABLED:
        return "", "", ""

    weburl = get_company_website(symbol)
    seen: set[str] = set()

    for query in _ir_search_queries(symbol, weburl, quarter, year):
        batch = search_news(
            query,
            source_label="IR · earnings press",
            limit=6,
            recency="7d",
        )
        for item in batch:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            if not title or not url or url in seen:
                continue
            seen.add(url)
            if not _looks_ir_or_company(url, weburl) and ".pdf" not in url.lower():
                # Allow major wires as secondary if IR not found later
                continue
            if not _EARNINGS_PRESS_TITLE.search(title) and ".pdf" not in url.lower():
                continue
            article = _fetch_release_body(url)
            if article and len(article) > 300:
                logger.info("IR earnings press %s: %s", symbol, title[:80])
                return article[:MAX_PRESS_CHARS], url, title
    return "", "", ""


def _extract_pdf_links(page_url: str, html: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in _PDF_HREF.finditer(html):
        href = match.group(1).strip()
        absolute = urljoin(page_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        lower = absolute.lower()
        if any(k in lower for k in ("earning", "result", "quarter", "financial", "press", "8-k", "8k")):
            links.insert(0, absolute)
        else:
            links.append(absolute)
    return links[:8]


def fetch_ir_page_pdf(
    symbol: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
) -> tuple[str, str, str]:
    """Crawl known IR landing page for earnings PDF links and extract text."""
    page = KNOWN_IR_PAGES.get(symbol.upper())
    if not page:
        weburl = get_company_website(symbol)
        if not weburl:
            return "", "", ""
        try:
            host = urlparse(weburl).netloc.replace("www.", "")
            page = f"https://investor.{host}/"
        except Exception:
            return "", "", ""

    try:
        with httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": SEC_USER_AGENT},
        ) as client:
            resp = client.get(page)
            resp.raise_for_status()
            html = resp.text[:500_000]
    except Exception as exc:
        logger.debug("IR page fetch failed %s: %s", page[:80], exc)
        return "", "", ""

    pdf_links = _extract_pdf_links(page, html)
    # Also follow earnings HTML links that may contain PDFs
    for match in _EARNINGS_HREF.finditer(html):
        href = match.group(1).strip()
        absolute = urljoin(page, href)
        if absolute.lower().endswith(".pdf") and absolute not in pdf_links:
            pdf_links.insert(0, absolute)

    q = _quarter_label(quarter)
    year_s = str(year) if year else ""
    for pdf_url in pdf_links:
        # Prefer PDFs mentioning quarter/year/earnings in the URL path
        path = pdf_url.lower()
        if q and q.lower() not in path and year_s and year_s not in path:
            if not any(k in path for k in ("earning", "result", "quarter", "financial")):
                continue
        article = fetch_article_text(pdf_url, max_chars=MAX_PRESS_CHARS)
        if article and len(article) > 400:
            title = f"{symbol} earnings release PDF"
            if q:
                title = f"{symbol} {q} earnings release PDF"
            logger.info("IR page PDF %s: %s", symbol, pdf_url[:100])
            return article[:MAX_PRESS_CHARS], pdf_url, title
    return "", "", ""


def fetch_wire_earnings_article(
    symbol: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
) -> tuple[str, str, str]:
    """Fallback: major news wires often reprint bank segment detail when IR PDF fails."""
    if not WEB_SEARCH_ENABLED:
        return "", "", ""

    q = _quarter_label(quarter)
    year_s = str(year) if year else ""
    queries = [
        f'"{symbol}" {q} earnings results revenue {year_s}'.strip(),
        f'"{symbol}" {q} "net interest income" OR "investment banking" OR CET1 {year_s}'.strip(),
        f'"{symbol}" beat earnings segment results {year_s}'.strip(),
    ]
    seen: set[str] = set()
    for query in queries:
        batch = search_news(query, source_label="Wire · earnings", limit=5, recency="7d")
        for item in batch:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            if not title or not url or url in seen:
                continue
            seen.add(url)
            if not _EARNINGS_PRESS_TITLE.search(title) and symbol.upper() not in title.upper():
                continue
            article = fetch_article_text(url, max_chars=MAX_PRESS_CHARS)
            if article and len(article) > 400:
                logger.info("Wire earnings article %s: %s", symbol, title[:80])
                return article[:MAX_PRESS_CHARS], url, title
    return "", "", ""


def fetch_earnings_press_release(
    symbol: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
) -> tuple[str, str, str]:
    """Official release first (Finnhub → IR RSS → IR page PDF), then wire fallback."""
    text, url, title = fetch_finnhub_press_release(symbol, quarter=quarter, year=year)
    if text:
        return text, url, title

    text, url, title = fetch_ir_earnings_press(symbol, quarter=quarter, year=year)
    if text:
        return text, url, title

    text, url, title = fetch_ir_page_pdf(symbol, quarter=quarter, year=year)
    if text:
        return text, url, title

    return fetch_wire_earnings_article(symbol, quarter=quarter, year=year)


def fetch_press_html(url: str) -> str:
    """Raw HTML for bullet-list extraction (empty for PDFs)."""
    if not url or not url.startswith("http"):
        return ""
    if url.lower().endswith(".pdf") or ".pdf?" in url.lower():
        return ""
    try:
        with httpx.Client(
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": SEC_USER_AGENT},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()
            if "application/pdf" in content_type:
                return ""
            return resp.text[:500_000]
    except Exception as exc:
        logger.debug("Press HTML fetch failed %s: %s", url[:80], exc)
        return ""
