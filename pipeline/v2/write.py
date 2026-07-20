"""Write drafts from approved Claims only.

Step 5: build posts from keep Claims + evidence (templates first).
Honors dry_run — when true, returns previews without queue inserts.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from rapidfuzz import fuzz
from sqlmodel import select

from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.draft_budget import DraftBudget
from pipeline.earnings_parse import EarningsFacts, extract_earnings_highlights, format_earnings_draft
from pipeline.macro_calendar import MACRO_TAKEAWAY
from pipeline.story_key import normalize_title
from pipeline.structured_common import content_hash, save_structured_draft
from pipeline.v2.types import Claim, EvidencePack, Intent

logger = setup_logging()

V2_SOURCE = "Pipeline v2"
_MAX_V2_DRAFTS = 5
_MIN_WRITE_CONFIDENCE = 0.65

# Scout assertions are LLM paraphrases — the same story reads differently every
# cycle, so exact/85+ title dedup never fires. Same-story paraphrases score
# ~60-75 on token_set_ratio while distinct same-theme stories score ~35-45.
_V2_DUP_HOURS = 24
_V2_ASSERTION_FUZZY = 55


def _recent_v2_duplicate(
    *,
    category: str,
    url: str,
    summary: str,
    seen: list[tuple[str, str, str]],
) -> str | None:
    """Reason string when this claim matches a same-cycle or recent v2 draft."""
    norm = normalize_title(summary or "")

    for seen_category, seen_url, seen_norm in seen:
        if url and url == seen_url:
            return "same evidence URL drafted this cycle"
        if (
            norm
            and seen_norm
            and seen_category == category
            and fuzz.token_set_ratio(norm, seen_norm) >= _V2_ASSERTION_FUZZY
        ):
            return "similar assertion drafted this cycle"

    cutoff = datetime.utcnow() - timedelta(hours=_V2_DUP_HOURS)
    with get_session() as session:
        rows = session.exec(
            select(Draft, Headline)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(Headline.source == V2_SOURCE)
            .where(Draft.created_at >= cutoff)
        ).all()

    for draft, headline in rows:
        if url and headline.url == url:
            return "same evidence URL drafted recently"
        if not norm or draft.category != category:
            continue
        other = normalize_title(headline.summary or headline.title)
        if other and fuzz.token_set_ratio(norm, other) >= _V2_ASSERTION_FUZZY:
            return "similar assertion drafted recently"
    return None


def write_from_claims(
    claims: list[Claim],
    *,
    intents: list[Intent] | None = None,
    packs: list[EvidencePack] | None = None,
    dry_run: bool = True,
    budget: DraftBudget | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Create pending drafts from status=keep claims.

    Returns (created_count, previews). Previews always include text that
    would be queued; created_count is 0 when dry_run=True.
    """
    intents_by_id = {i.id: i for i in (intents or [])}
    packs_by_id = {p.intent_id: p for p in (packs or [])}
    eligible = sorted(
        [c for c in claims if c.status == "keep" and c.confidence >= _MIN_WRITE_CONFIDENCE],
        key=lambda c: c.confidence,
        reverse=True,
    )
    previews: list[dict[str, Any]] = []
    created = 0
    seen: list[tuple[str, str, str]] = []  # (category, url, normalized assertion)

    for claim in eligible:
        if len(previews) >= _MAX_V2_DRAFTS:
            break
        intent = intents_by_id.get(claim.intent_id)
        pack = packs_by_id.get(claim.intent_id)
        built = _build_draft(claim, intent=intent, pack=pack)
        if not built:
            continue
        title, summary, draft_text, category, impact, fmt = built

        # Prefer an http(s) evidence URL for the headline row
        url = next(
            (u for u in (claim.evidence_urls or []) if str(u).startswith("http")),
            f"https://v2.local/{claim.intent_id}",
        )
        dup_reason = _recent_v2_duplicate(
            category=category, url=url, summary=summary, seen=seen
        )
        if dup_reason:
            logger.info("v2 write: skipping %s — %s", claim.intent_id, dup_reason)
            continue
        seen.append((category, url, normalize_title(summary or "")))

        preview = {
            "intent_id": claim.intent_id,
            "kind": claim.kind,
            "title": title,
            "category": category,
            "impact": impact,
            "confidence": claim.confidence,
            "text": draft_text[:1200],
            "dry_run": dry_run,
        }
        previews.append(preview)

        if dry_run:
            continue

        if budget is not None and budget.remaining <= 0:
            break

        ticker_list = [t.strip().upper() for t in (claim.tickers or [])[:4] if str(t).strip()]
        tickers_csv = ",".join(ticker_list) if ticker_list else "SPY"
        # Stable per intent (ids embed date/period/status) so the same intent
        # can't re-draft every cycle with a reworded assertion.
        chash = content_hash(V2_SOURCE, claim.intent_id)
        # earnings_ticker_blocked expects a single symbol
        block_ticker = ticker_list[0] if ticker_list else "SPY"
        ok = save_structured_draft(
            source=V2_SOURCE,
            url=url,
            title=title,
            summary=summary,
            draft_text=draft_text,
            tickers=tickers_csv if category != "earnings" else block_ticker,
            category=category,
            impact=impact,
            fmt=fmt,
            confidence=claim.confidence,
            chash=chash,
            budget=budget,
        )
        if ok:
            created += 1

    skipped_low = sum(1 for c in claims if c.status == "keep" and c.confidence < _MIN_WRITE_CONFIDENCE)
    if dry_run:
        logger.info(
            "v2 write: dry-run — %d keep previews (0 queued, skipped_low_conf=%d)",
            len(previews),
            skipped_low,
        )
    else:
        logger.info(
            "v2 write: queued %d drafts from %d eligible keep claims (skipped_low_conf=%d)",
            created,
            len(eligible),
            skipped_low,
        )
    return created, previews


def _build_draft(
    claim: Claim,
    *,
    intent: Intent | None,
    pack: EvidencePack | None,
) -> tuple[str, str, str, str, str, str] | None:
    if claim.kind == "earnings_print":
        return _build_earnings_draft(claim, intent=intent, pack=pack)
    if claim.kind in ("macro_print", "fed_decision", "fed_speak", "macro_reaction"):
        return _build_macro_draft(claim, intent=intent, pack=pack)
    if claim.kind in ("company_material", "ai_catalyst", "politics_policy"):
        return _build_theme_draft(claim, intent=intent, pack=pack)
    return None


def _build_theme_draft(
    claim: Claim,
    *,
    intent: Intent | None,
    pack: EvidencePack | None,
) -> tuple[str, str, str, str, str, str] | None:
    assertion = (claim.assertion or "").strip()
    if len(assertion) < 20:
        # Fall back to best evidence title
        if pack:
            for item in pack.items:
                if item.metadata.get("kind") == "calendar":
                    continue
                if len(item.title or "") >= 20:
                    assertion = item.title.strip()
                    break
    if len(assertion) < 20:
        return None

    tickers = claim.tickers or (intent.tickers if intent else []) or []
    ticker_line = " ".join(f"${t}" for t in tickers[:4])
    points: list[str] = []
    key_points = claim.facts.get("key_points") if isinstance(claim.facts, dict) else None
    if isinstance(key_points, list):
        for point in key_points:
            text = re.sub(r"\s+", " ", str(point)).strip()
            if len(text) >= 25:
                points.append(text[:140])
            if len(points) >= 3:
                break
    if not points:
        detail = _first_useful_snippet(pack)
        if detail:
            points.append(detail)

    category = {
        "company_material": "other",
        "ai_catalyst": "ai",
        "politics_policy": "geopolitics",
    }.get(claim.kind, "other")

    lines = [assertion]
    for point in points[:2]:
        if point.lower() not in assertion.lower():
            lines.append(point)
    if ticker_line:
        lines.append("")
        lines.append(ticker_line)
    draft = "\n".join(lines).strip()
    label = (intent.label if intent else None) or claim.kind
    title = f"{label}: {assertion[:90]}"
    impact = "high" if claim.confidence >= 0.85 else "med"
    return title, assertion, draft, category, impact, "BREAKING"

def _build_earnings_draft(
    claim: Claim,
    *,
    intent: Intent | None,
    pack: EvidencePack | None,
) -> tuple[str, str, str, str, str, str] | None:
    ticker = (claim.tickers[0] if claim.tickers else "") or (
        intent.tickers[0] if intent and intent.tickers else ""
    )
    ticker = ticker.upper()
    if not ticker:
        return None

    facts = _earnings_facts_from_claim(claim, intent)
    if not facts.has_numbers():
        return None

    verb = _beat_miss_verb(facts)
    blob = _evidence_blob(pack)
    highlights = (
        extract_earnings_highlights(blob, ticker=ticker, max_bullets=8, allow_llm=True)
        if blob and len(blob) >= 120
        else []
    )
    # Prefer scout key_points when regex highlights are thin
    key_points = claim.facts.get("key_points") if isinstance(claim.facts, dict) else None
    if isinstance(key_points, list):
        for point in key_points:
            text = str(point).strip()
            if text and text not in highlights:
                highlights.append(text)
            if len(highlights) >= 8:
                break

    # Last resort: pull concrete sentences from evidence snippets
    if len(highlights) < 3 and pack:
        for item in pack.items:
            if item.metadata.get("kind") == "calendar":
                continue
            snip = re.sub(r"\s+", " ", (item.snippet or "")).strip()
            if len(snip) < 40:
                continue
            if snip not in highlights:
                highlights.append(snip[:140])
            if len(highlights) >= 5:
                break

    year = None
    if claim.period:
        match = re.search(r"(20\d{2})", claim.period)
        if match:
            year = int(match.group(1))
    if facts.quarter is None and claim.period:
        qmatch = re.search(r"Q([1-4])", claim.period, re.I)
        if qmatch:
            facts.quarter = f"Q{qmatch.group(1)}"

    draft_text = format_earnings_draft(
        ticker,
        verb,
        facts,
        highlights=highlights,
        year=year,
    )
    period = claim.period or facts.quarter or ""
    title = f"{ticker} {period} earnings".strip()
    summary = claim.assertion or title
    impact = "high" if verb in ("beat", "missed") else "med"
    return title, summary, draft_text, "earnings", impact, "SUMMARY"


def _build_macro_draft(
    claim: Claim,
    *,
    intent: Intent | None,
    pack: EvidencePack | None,
) -> tuple[str, str, str, str, str, str] | None:
    label = (intent.label if intent else None) or claim.kind.replace("_", " ").title()
    facts = claim.facts if isinstance(claim.facts, dict) else {}
    meta = (intent.metadata if intent else {}) or {}
    actual = facts.get("actual", meta.get("actual"))
    estimate = facts.get("estimate", meta.get("estimate"))
    unit = facts.get("unit", meta.get("unit")) or ""

    if actual is not None and estimate is not None:
        try:
            a = float(actual)
            e = float(estimate)
            word = "in-line" if abs(a - e) <= 0.02 else ("beat" if a > e else "miss")
        except (TypeError, ValueError):
            word = "print"
        actual_s = _fmt_macro(actual, unit)
        est_s = _fmt_macro(estimate, unit)
        line1 = f"{label} came in at {actual_s} vs {est_s} expected"
        title = f"{label} {actual_s} vs {est_s} est ({word})"
        impact = "high" if word in ("beat", "miss") else "med"
        fmt = "BREAKING"
    elif claim.assertion:
        line1 = claim.assertion.strip()
        title = f"{label}: {line1[:80]}"
        impact = "med"
        fmt = "CONTEXT"
        word = "update"
    else:
        return None

    takeaway = MACRO_TAKEAWAY.get(label, "Macro data moves rates and risk assets")
    # Prefer grounded evidence details over canned takeaways
    details: list[str] = []
    key_points = claim.facts.get("key_points") if isinstance(claim.facts, dict) else None
    if isinstance(key_points, list):
        for point in key_points:
            text = re.sub(r"\s+", " ", str(point)).strip()
            if len(text) >= 30:
                details.append(text[:160])
            if len(details) >= 2:
                break
    if not details:
        detail = _first_useful_snippet(pack)
        if detail:
            details.append(detail)
    line2 = details[0] if details else takeaway
    line3 = details[1] if len(details) > 1 else None
    tickers = " ".join(f"${t}" for t in (claim.tickers or ["SPY"])[:3])
    parts = [line1, line2]
    if line3:
        parts.append(line3)
    parts.extend(["", tickers])
    draft = "\n".join(parts).strip()
    return title, claim.assertion or title, draft, "macro", impact, fmt


def _earnings_facts_from_claim(claim: Claim, intent: Intent | None) -> EarningsFacts:
    facts_dict = claim.facts if isinstance(claim.facts, dict) else {}
    meta = (intent.metadata if intent else {}) or {}

    def pick(*keys: str) -> str | None:
        for key in keys:
            val = facts_dict.get(key)
            if val is None:
                val = meta.get(key)
            if val is None:
                continue
            return str(val).strip() or None
        return None

    quarter = None
    if claim.period:
        qmatch = re.search(r"Q([1-4])", claim.period, re.I)
        if qmatch:
            quarter = f"Q{qmatch.group(1)}"

    return EarningsFacts(
        quarter=quarter,
        eps_actual=pick("eps_actual", "epsActual"),
        eps_estimate=pick("eps_estimate", "epsEstimate"),
        revenue_actual=_fmt_rev(pick("revenue_actual", "revenueActual")),
        revenue_estimate=_fmt_rev(pick("revenue_estimate", "revenueEstimate")),
    )


def _fmt_rev(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.replace(",", "").replace("$", "").strip()
    try:
        num = float(raw)
    except ValueError:
        return value
    # Finnhub revenue often in absolute dollars
    if num >= 1_000_000_000:
        return f"${num / 1_000_000_000:.2f}B"
    if num >= 1_000_000:
        return f"${num / 1_000_000:.1f}M"
    return f"${num:g}"


def _beat_miss_verb(facts: EarningsFacts) -> str:
    try:
        if facts.eps_actual and facts.eps_estimate:
            if float(str(facts.eps_actual).replace("$", "")) > float(str(facts.eps_estimate).replace("$", "")):
                return "beat"
            if float(str(facts.eps_actual).replace("$", "")) < float(str(facts.eps_estimate).replace("$", "")):
                return "missed"
    except ValueError:
        pass
    return "reported"


def _evidence_blob(pack: EvidencePack | None) -> str:
    if not pack:
        return ""
    parts: list[str] = []
    for item in pack.items:
        if item.metadata.get("kind") == "calendar":
            continue
        parts.append(item.title)
        if item.snippet:
            parts.append(item.snippet)
    return "\n".join(parts)[:12000]


def _first_useful_snippet(pack: EvidencePack | None) -> str | None:
    if not pack:
        return None
    for item in pack.items:
        if item.metadata.get("kind") == "calendar":
            continue
        snip = re.sub(r"\s+", " ", (item.snippet or item.title or "")).strip()
        if len(snip) >= 40:
            return snip[:160]
    return None


def _fmt_macro(value: Any, unit: str | None) -> str:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return str(value)
    unit_s = (unit or "").strip()
    if unit_s == "%":
        return f"{val:g}%"
    return f"{val:g}{(' ' + unit_s) if unit_s else ''}"
