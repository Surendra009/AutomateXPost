"""Fetch evidence packs per intent (search, IR, wires).

Step 2: retrieve sources for each intent and enforce a minimum bar.
LLM is not used here — only APIs and parsers.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from config import WEB_SEARCH_ENABLED
from logging_config import setup_logging
from pipeline.enrich import fetch_article_text
from pipeline.v2.types import EvidenceItem, EvidencePack, Intent
from pipeline.web_search import search_news, web_search_configured

logger = setup_logging()

_MAX_QUERIES_PER_INTENT = 2
_MAX_SEARCH_HITS = 5
_MAX_ARTICLE_FETCHES_PER_INTENT = 2
_MIN_BODY_CHARS = 280

_TIER1_HOST_FRAGMENTS = (
    "sec.gov",
    "federalreserve.gov",
    "bls.gov",
    "bea.gov",
    "census.gov",
    "treasury.gov",
    "newswire",
    "businesswire",
    "prnewswire",
    "globenewswire",
    "investor.",
    "ir.",
)
_TIER2_HOST_FRAGMENTS = (
    "reuters.com",
    "bloomberg.com",
    "wsj.com",
    "cnbc.com",
    "ft.com",
    "apnews.com",
    "marketwatch.com",
    "barrons.com",
    "nytimes.com",
    "washingtonpost.com",
)

_FED_TITLE = re.compile(
    r"\b(fed|fomc|powell|federal reserve|rate decision|interest rate)\b",
    re.I,
)
_MACRO_TITLE = re.compile(
    r"\b(cpi|ppi|inflation|payroll|nfp|gdp|retail sales|jobless|unemployment)\b",
    re.I,
)


def fetch_evidence_packs(intents: list[Intent]) -> list[EvidencePack]:
    """Gather evidence for each intent and mark minimum-bar readiness."""
    packs: list[EvidencePack] = []
    for intent in intents:
        try:
            pack = _fetch_one(intent)
        except Exception as exc:
            logger.warning("v2 evidence failed for %s: %s", intent.id, exc)
            pack = EvidencePack(
                intent_id=intent.id,
                items=[],
                gaps=["fetch_error"],
                meets_minimum=False,
                notes=str(exc)[:200],
            )
        packs.append(pack)

    ready = sum(1 for p in packs if p.meets_minimum)
    logger.info("v2 evidence: %d packs (%d ready)", len(packs), ready)
    return packs


def _fetch_one(intent: Intent) -> EvidencePack:
    if intent.kind == "earnings_print":
        return _evidence_earnings(intent)
    if intent.kind in ("macro_print", "fed_decision", "fed_speak", "macro_reaction"):
        return _evidence_macro_fed(intent)
    return EvidencePack(
        intent_id=intent.id,
        gaps=["unsupported_kind"],
        meets_minimum=False,
        notes=f"No evidence fetcher for kind={intent.kind}",
    )


def _evidence_earnings(intent: Intent) -> EvidencePack:
    items: list[EvidenceItem] = []
    meta = intent.metadata or {}
    status = (meta.get("status") or "").lower()
    symbol = (intent.tickers[0] if intent.tickers else intent.label or "").upper()
    quarter = meta.get("quarter")
    year = meta.get("year")
    try:
        quarter_i = int(quarter) if quarter not in (None, "") else None
    except (TypeError, ValueError):
        quarter_i = None
    try:
        year_i = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year_i = None

    calendar_item = _calendar_earnings_item(intent, symbol)
    if calendar_item:
        items.append(calendar_item)

    # Official / IR / wire body (best signal for fat drafts later)
    if symbol and status == "reported" and WEB_SEARCH_ENABLED:
        ir_item = _fetch_earnings_primary(symbol, quarter_i, year_i)
        if ir_item:
            items.append(ir_item)

    items.extend(_search_items(intent, source_label=f"v2 · earnings · {symbol}"))
    items = _dedupe_items(items)
    _enrich_bodies(items, limit=_MAX_ARTICLE_FETCHES_PER_INTENT)
    return _assess_earnings(intent, items)


def _evidence_macro_fed(intent: Intent) -> EvidencePack:
    items: list[EvidenceItem] = []
    meta = intent.metadata or {}
    status = (meta.get("status") or "").lower()
    label = intent.label or intent.kind

    calendar_item = _calendar_macro_item(intent)
    if calendar_item:
        items.append(calendar_item)

    items.extend(_search_items(intent, source_label=f"v2 · {intent.kind} · {label}"))
    items = _dedupe_items(items)
    _enrich_bodies(items, limit=_MAX_ARTICLE_FETCHES_PER_INTENT)
    return _assess_macro_fed(intent, items)


def fetch_followup_items(intent: Intent, queries: list[str]) -> list[EvidenceItem]:
    """Run extra search queries + light body enrich for research follow-ups."""
    if not queries:
        return []
    prior = list(intent.queries or [])
    intent.queries = list(queries)[:_MAX_QUERIES_PER_INTENT]
    try:
        items = _search_items(intent, source_label=f"v2 · research · {intent.label or intent.kind}")
        _enrich_bodies(items, limit=1)
        for item in items:
            item.metadata["kind"] = "research"
        return items
    finally:
        intent.queries = prior


def reassess_pack(intent: Intent, pack: EvidencePack) -> EvidencePack:
    """Recompute gaps / meets_minimum after research merges new items."""
    items = _dedupe_items(list(pack.items))
    if intent.kind == "earnings_print":
        assessed = _assess_earnings(intent, items)
    elif intent.kind in ("macro_print", "fed_decision", "fed_speak", "macro_reaction"):
        assessed = _assess_macro_fed(intent, items)
    else:
        assessed = EvidencePack(
            intent_id=intent.id,
            items=items,
            gaps=["unsupported_kind"],
            meets_minimum=False,
            notes=f"No assessor for kind={intent.kind}",
        )
    pack.items = assessed.items
    pack.gaps = assessed.gaps
    pack.meets_minimum = assessed.meets_minimum
    pack.notes = assessed.notes
    return pack


def _assess_earnings(intent: Intent, items: list[EvidenceItem]) -> EvidencePack:
    meta = intent.metadata or {}
    status = (meta.get("status") or "").lower()
    gaps: list[str] = []
    if status == "preview":
        gaps.append("preview_waiting_for_print")
    if not intent.period:
        gaps.append("need_period")
    if not _has_calendar_numbers(meta) and not _has_numeric_body(items):
        gaps.append("need_numbers")
    if not _has_tier(items, max_tier=2) and not _has_body(items, _MIN_BODY_CHARS):
        gaps.append("need_primary_or_wire")
    if not _has_body(items, _MIN_BODY_CHARS):
        gaps.append("need_body")

    meets = False
    notes = ""
    if status == "preview":
        notes = "Preview intent — waiting for print"
    elif not intent.period:
        notes = "Missing fiscal period"
    elif _has_calendar_numbers(meta) and (
        _has_body(items, _MIN_BODY_CHARS) or _has_tier(items, max_tier=2)
    ):
        meets = True
        gaps = [g for g in gaps if g != "need_numbers"]
        if _has_body(items, _MIN_BODY_CHARS):
            gaps = [g for g in gaps if g not in ("need_body", "need_primary_or_wire")]
        notes = "Calendar numbers + supporting source"
    elif _has_body(items, _MIN_BODY_CHARS) and _has_numeric_body(items):
        meets = True
        gaps = []
        notes = "Primary/wire body with numbers"
    else:
        notes = "Below earnings minimum bar"

    return EvidencePack(
        intent_id=intent.id,
        items=items,
        gaps=gaps,
        meets_minimum=meets,
        notes=notes,
    )


def _assess_macro_fed(intent: Intent, items: list[EvidenceItem]) -> EvidencePack:
    meta = intent.metadata or {}
    status = (meta.get("status") or "").lower()
    gaps: list[str] = []
    if status == "preview":
        gaps.append("preview_waiting_for_print")

    relevant = [i for i in items if _title_relevant_macro_fed(i.title, intent)]
    if not relevant and items:
        gaps.append("need_relevant_headline")

    meets = False
    notes = ""
    if status == "preview":
        notes = "Macro preview — waiting for print"
    elif intent.kind == "fed_speak" or meta.get("standing"):
        if len(relevant) >= 1 and (
            _has_tier(relevant, max_tier=2) or _has_body(relevant, 120)
        ):
            meets = True
            gaps = []
            notes = "Fed speak/news evidence found"
        else:
            gaps.append("need_fed_coverage")
            notes = "No usable Fed news yet"
    elif status == "reported" and _macro_has_actual(meta):
        if relevant or _has_tier(items, max_tier=2):
            meets = True
            gaps = [g for g in gaps if g != "preview_waiting_for_print"]
            if not _has_body(items, _MIN_BODY_CHARS):
                gaps.append("need_body")
            notes = "Macro print + news confirmation"
        else:
            gaps.append("need_news_confirmation")
            notes = "Have calendar print, awaiting news confirmation"
    elif _has_tier(relevant, max_tier=2) or (
        len(relevant) >= 2 and _has_body(relevant, 120)
    ):
        meets = True
        gaps = []
        notes = "Wire/search confirmation without calendar actual"
    else:
        gaps.append("need_evidence")
        notes = "Below macro/Fed minimum bar"

    return EvidencePack(
        intent_id=intent.id,
        items=items,
        gaps=gaps,
        meets_minimum=meets,
        notes=notes,
    )


def _calendar_earnings_item(intent: Intent, symbol: str) -> EvidenceItem | None:
    meta = intent.metadata or {}
    if not _has_calendar_numbers(meta):
        return None
    date_str = meta.get("date") or ""
    parts = []
    if meta.get("eps_actual") is not None:
        parts.append(f"EPS actual {meta.get('eps_actual')} vs est {meta.get('eps_estimate')}")
    if meta.get("revenue_actual") is not None:
        parts.append(
            f"Revenue actual {meta.get('revenue_actual')} vs est {meta.get('revenue_estimate')}"
        )
    snippet = "; ".join(parts)
    return EvidenceItem(
        url=f"finnhub://earnings/{symbol}/{date_str}",
        title=f"{symbol} {intent.period or ''} earnings calendar".strip(),
        source_tier=1,
        published_at=_parse_meta_date(date_str),
        snippet=snippet,
        body_chars=len(snippet),
        metadata={"kind": "calendar", **{k: meta.get(k) for k in (
            "eps_actual", "eps_estimate", "revenue_actual", "revenue_estimate", "hour", "status"
        )}},
    )


def _calendar_macro_item(intent: Intent) -> EvidenceItem | None:
    meta = intent.metadata or {}
    if not _macro_has_actual(meta) and meta.get("estimate") is None:
        return None
    date_str = meta.get("date") or ""
    label = intent.label or "Macro"
    snippet = (
        f"{label} actual={meta.get('actual')} estimate={meta.get('estimate')} "
        f"unit={meta.get('unit') or ''}"
    ).strip()
    return EvidenceItem(
        url=f"finnhub://macro/{label}/{date_str}",
        title=f"{label} economic calendar",
        source_tier=1,
        published_at=_parse_meta_date(date_str),
        snippet=snippet,
        body_chars=len(snippet),
        metadata={
            "kind": "calendar",
            "actual": meta.get("actual"),
            "estimate": meta.get("estimate"),
            "unit": meta.get("unit"),
            "event_name": meta.get("event_name"),
            "status": meta.get("status"),
        },
    )


def _fetch_earnings_primary(
    symbol: str,
    quarter: int | None,
    year: int | None,
) -> EvidenceItem | None:
    try:
        from pipeline.earnings_press import fetch_earnings_press_release
    except Exception:
        return None
    text, url, title = fetch_earnings_press_release(symbol, quarter=quarter, year=year)
    if not text or not url:
        return None
    tier = 1 if _source_tier(url) == 1 else 2
    return EvidenceItem(
        url=url,
        title=title or f"{symbol} earnings release",
        source_tier=tier,
        snippet=text[:400],
        body_chars=len(text),
        metadata={"kind": "primary_press"},
    )


def _search_items(intent: Intent, *, source_label: str) -> list[EvidenceItem]:
    if not web_search_configured():
        return []
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    for query in (intent.queries or [])[:_MAX_QUERIES_PER_INTENT]:
        try:
            batch = search_news(query, source_label=source_label, limit=_MAX_SEARCH_HITS, recency="1d")
        except Exception as exc:
            logger.debug("v2 search failed %s: %s", query[:60], exc)
            continue
        for raw in batch:
            url = (raw.get("url") or "").strip()
            title = (raw.get("title") or "").strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            published = raw.get("published_at")
            if isinstance(published, str):
                published = _parse_meta_date(published[:10])
            items.append(
                EvidenceItem(
                    url=url,
                    title=title,
                    source_tier=_source_tier(url),
                    published_at=published if isinstance(published, datetime) else None,
                    snippet=(raw.get("summary") or "")[:500],
                    body_chars=0,
                    metadata={"kind": "search", "query": query, "source": raw.get("source")},
                )
            )
    return items


def _enrich_bodies(items: list[EvidenceItem], *, limit: int) -> None:
    """Fetch article text for top non-calendar items missing bodies."""
    fetched = 0
    ranked = sorted(
        (i for i in items if i.metadata.get("kind") != "calendar" and i.body_chars < _MIN_BODY_CHARS),
        key=lambda i: (i.source_tier, -len(i.snippet or "")),
    )
    for item in ranked:
        if fetched >= limit:
            break
        if not item.url.startswith("http"):
            continue
        try:
            text = fetch_article_text(item.url, max_chars=6000) or ""
        except Exception:
            text = ""
        if len(text) < 80:
            continue
        item.snippet = text[:500]
        item.body_chars = len(text)
        item.metadata["body_fetched"] = True
        fetched += 1


def _source_tier(url: str) -> int:
    host = ""
    try:
        host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return 3
    for frag in _TIER1_HOST_FRAGMENTS:
        if frag in host or frag in url.lower():
            return 1
    for frag in _TIER2_HOST_FRAGMENTS:
        if frag in host:
            return 2
    return 3


def _dedupe_items(items: list[EvidenceItem]) -> list[EvidenceItem]:
    seen: set[str] = set()
    out: list[EvidenceItem] = []
    for item in items:
        key = item.url or item.title
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _has_calendar_numbers(meta: dict[str, Any]) -> bool:
    return meta.get("eps_actual") is not None or meta.get("revenue_actual") is not None


def _macro_has_actual(meta: dict[str, Any]) -> bool:
    return meta.get("actual") is not None


def _has_tier(items: list[EvidenceItem], *, max_tier: int) -> bool:
    return any(i.source_tier <= max_tier and i.metadata.get("kind") != "calendar" for i in items)


def _has_body(items: list[EvidenceItem], min_chars: int) -> bool:
    return any(
        i.body_chars >= min_chars and i.metadata.get("kind") != "calendar"
        for i in items
    )


def _has_numeric_body(items: list[EvidenceItem]) -> bool:
    num = re.compile(r"(\$\s?\d|\d+\.\d+%|\bEPS\b|\brevenue\b|\bEPS\b)", re.I)
    for item in items:
        if item.metadata.get("kind") == "calendar":
            continue
        blob = f"{item.title} {item.snippet}"
        if item.body_chars >= 80 and num.search(blob):
            return True
    return False


def _title_relevant_macro_fed(title: str, intent: Intent) -> bool:
    if intent.kind in ("fed_decision", "fed_speak") or (intent.label or "") == "Fed":
        return bool(_FED_TITLE.search(title or ""))
    label = (intent.label or "").lower()
    if label and label in (title or "").lower():
        return True
    return bool(_MACRO_TITLE.search(title or ""))


def _parse_meta_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError:
        return None
