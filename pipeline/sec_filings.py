"""SEC 8-K filings — template drafts from structured Item codes (zero LLM)."""

from __future__ import annotations

import re
from datetime import datetime
from time import mktime
from typing import Any

import feedparser
import httpx

from config import MAX_SEC_DRAFTS_PER_CYCLE, SEC_EDGAR_8K_FEED, SEC_USER_AGENT
from database import get_setting
from logging_config import setup_logging
from pipeline.draft_budget import DraftBudget
from pipeline.finnhub_api import finnhub_get, get_finnhub_key
from pipeline.structured_common import content_hash, save_structured_draft
from pipeline.watchlist_scope import in_watchlist

logger = setup_logging()

SEC_SOURCE = "SEC EDGAR 8-K"
_feed_cache: list[dict[str, Any]] | None = None


def clear_sec_feed_cache() -> None:
    global _feed_cache
    _feed_cache = None


def get_sec_8k_entries() -> list[dict[str, Any]]:
    global _feed_cache
    if _feed_cache is None:
        _feed_cache = fetch_sec_8k_feed()
    return _feed_cache

TITLE_RE = re.compile(r"8-K\s*-\s*(.+?)\s*\(\d+\)", re.I)
ITEM_RE = re.compile(r"Item\s+([\d.]+):\s*([^<\n]+)", re.I)

# item_code -> (line2 takeaway, category, impact, format)
MATERIAL_ITEMS: dict[str, tuple[str, str, str, str]] = {
    "2.02": ("Quarterly results filed with the SEC", "earnings", "high", "BREAKING"),
    "2.01": ("Deal closed — balance sheet and growth story change", "regulatory", "high", "BREAKING"),
    "1.01": ("New contract or deal terms now public", "regulatory", "med", "CONTEXT"),
    "5.02": ("Leadership change can shift strategy and sentiment", "regulatory", "med", "CONTEXT"),
}

HTTP_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/atom+xml, application/xml, text/xml, */*",
}


def _parse_sec_feed(raw: str) -> list[dict[str, Any]]:
    feed = feedparser.parse(raw)
    items: list[dict[str, Any]] = []
    for entry in feed.entries[:40]:
        link = getattr(entry, "link", "") or ""
        title = getattr(entry, "title", "").strip()
        if not title or not link:
            continue

        summary_html = ""
        if getattr(entry, "summary", None):
            summary_html = str(entry.summary)
        elif getattr(entry, "content", None):
            summary_html = entry.content[0].get("value", "")

        published_at = datetime.utcnow()
        for attr in ("published_parsed", "updated_parsed"):
            parsed = getattr(entry, attr, None)
            if parsed:
                try:
                    published_at = datetime.fromtimestamp(mktime(parsed))
                    break
                except (ValueError, OverflowError):
                    pass

        accession = ""
        entry_id = getattr(entry, "id", "") or ""
        if "accession-number=" in entry_id:
            accession = entry_id.split("accession-number=")[-1]

        items.append({
            "title": title,
            "url": link,
            "summary_html": summary_html,
            "published_at": published_at,
            "accession": accession,
        })
    return items


def fetch_sec_8k_feed() -> list[dict[str, Any]]:
    source, url = SEC_EDGAR_8K_FEED
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=HTTP_HEADERS) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return _parse_sec_feed(resp.text)
    except Exception as exc:
        logger.warning("Failed to fetch SEC 8-K feed: %s", exc)
        return []


def _parse_company_name(title: str) -> str | None:
    match = TITLE_RE.search(title)
    if not match:
        return None
    name = match.group(1).strip()
    name = re.sub(r",\s*/(NV|DE|MD|FL)$", "", name, flags=re.I)
    return name


def _parse_items(summary_html: str) -> list[tuple[str, str]]:
    plain = re.sub(r"<[^>]+>", "\n", summary_html)
    return [(m.group(1), m.group(2).strip()) for m in ITEM_RE.finditer(plain)]


def _primary_material_item(items: list[tuple[str, str]]) -> tuple[str, str] | None:
    priority = ("2.02", "2.01", "5.02", "1.01")
    codes = {code: desc for code, desc in items}
    for code in priority:
        if code in codes:
            return code, codes[code]
    return None


def _resolve_ticker(company_name: str, cache: dict[str, str | None]) -> str | None:
    if company_name in cache:
        return cache[company_name]

    if not get_finnhub_key():
        cache[company_name] = None
        return None

    data, err = finnhub_get("search", {"q": company_name})
    if err or not data:
        cache[company_name] = None
        return None

    for hit in (data.get("result") or [])[:8]:
        sym = (hit.get("symbol") or "").upper()
        if sym and re.fullmatch(r"[A-Z]{1,5}", sym):
            cache[company_name] = sym
            return sym

    cache[company_name] = None
    return None


def _build_draft(company: str, item_code: str, symbol: str) -> tuple[str, str, str, str, str, str, float]:
    takeaway, category, impact, fmt = MATERIAL_ITEMS[item_code]
    short_co = company
    if len(short_co) > 40:
        short_co = short_co.rsplit(",", 1)[0][:40].strip()

    if item_code == "2.02":
        line1 = f"{symbol} filed Q results with the SEC"
    elif item_code == "2.01":
        line1 = f"{symbol} closed an acquisition or asset sale"
    elif item_code == "5.02":
        line1 = f"{symbol} disclosed a leadership change"
    else:
        line1 = f"{symbol} signed a material agreement"

    line2 = takeaway
    draft = f"{line1}\n{line2}\n\n${symbol}"
    title = f"8-K: {company} — Item {item_code}"
    summary = f"{company} filed 8-K Item {item_code}: {takeaway}"
    confidence = 0.92 if impact == "high" else 0.87
    return title, summary, draft, category, impact, fmt, confidence


def process_sec_filings(budget: DraftBudget | None = None) -> tuple[int, int]:
    """Create drafts from SEC 8-K Item metadata. Returns (ingested, drafts)."""
    watchlist = get_setting("watchlist", [])
    if not watchlist:
        return 0, 0

    entries = get_sec_8k_entries()
    ticker_cache: dict[str, str | None] = {}

    ingested = 0
    drafts_created = 0

    for entry in entries:
        if budget is not None and budget.remaining <= 0:
            break
        if drafts_created >= MAX_SEC_DRAFTS_PER_CYCLE:
            break

        company = _parse_company_name(entry["title"])
        if not company:
            continue

        items = _parse_items(entry["summary_html"])
        material = _primary_material_item(items)
        if not material:
            continue

        item_code, _item_desc = material
        symbol = _resolve_ticker(company, ticker_cache)
        if not in_watchlist(symbol, watchlist):
            continue

        chash = content_hash(SEC_SOURCE, entry["accession"] or entry["url"], item_code)
        title, summary, draft_text, category, impact, fmt, confidence = _build_draft(
            company, item_code, symbol
        )

        if save_structured_draft(
            source=SEC_SOURCE,
            url=entry["url"],
            title=title,
            summary=summary,
            draft_text=draft_text,
            tickers=symbol,
            category=category,
            impact=impact,
            fmt=fmt,
            confidence=confidence,
            chash=chash,
            published_at=entry["published_at"],
            budget=budget,
        ):
            ingested += 1
            drafts_created += 1

    if drafts_created:
        logger.info("SEC 8-K: created %d structured drafts", drafts_created)
    return ingested, drafts_created
