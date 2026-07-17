"""Write drafts from approved Claims only.

Step 5: build posts from keep Claims + evidence (templates first).
Honors dry_run — when true, returns previews without queue inserts.
"""

from __future__ import annotations

import re
from typing import Any

from logging_config import setup_logging
from pipeline.draft_budget import DraftBudget
from pipeline.earnings_parse import EarningsFacts, extract_earnings_highlights, format_earnings_draft
from pipeline.macro_calendar import MACRO_TAKEAWAY
from pipeline.structured_common import content_hash, save_structured_draft
from pipeline.v2.types import Claim, EvidencePack, Intent

logger = setup_logging()

V2_SOURCE = "Pipeline v2"
_MAX_V2_DRAFTS = 3


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
    eligible = [c for c in claims if c.status == "keep"]
    previews: list[dict[str, Any]] = []
    created = 0

    for claim in eligible:
        if len(previews) >= _MAX_V2_DRAFTS:
            break
        intent = intents_by_id.get(claim.intent_id)
        pack = packs_by_id.get(claim.intent_id)
        built = _build_draft(claim, intent=intent, pack=pack)
        if not built:
            continue
        title, summary, draft_text, category, impact, fmt = built
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

        tickers = " ".join(f"${t}" for t in (claim.tickers or [])[:4]) or "$SPY"
        url = (claim.evidence_urls[0] if claim.evidence_urls else "") or f"v2://claim/{claim.intent_id}"
        chash = content_hash(V2_SOURCE, claim.intent_id, title, draft_text[:200])
        ok = save_structured_draft(
            source=V2_SOURCE,
            url=url if url.startswith("http") else f"https://v2.local/{claim.intent_id}",
            title=title,
            summary=summary,
            draft_text=draft_text,
            tickers=tickers.replace("$", "").replace("  ", " ").strip() or "SPY",
            category=category,
            impact=impact,
            fmt=fmt,
            confidence=max(claim.confidence, 0.7),
            chash=chash,
            budget=budget,
        )
        if ok:
            created += 1

    if dry_run:
        logger.info("v2 write: dry-run — %d keep previews (0 queued)", len(previews))
    else:
        logger.info("v2 write: queued %d drafts from %d keep claims", created, len(eligible))
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
    return None


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
    highlights = extract_earnings_highlights(blob, ticker=ticker, max_bullets=6, allow_llm=False) if blob else []
    # Prefer scout key_points when regex highlights are thin
    key_points = claim.facts.get("key_points") if isinstance(claim.facts, dict) else None
    if isinstance(key_points, list):
        for point in key_points:
            text = str(point).strip()
            if text and text not in highlights:
                highlights.append(text)
            if len(highlights) >= 6:
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
    # One grounded detail from evidence if available
    detail = _first_useful_snippet(pack)
    line2 = detail or takeaway
    tickers = " ".join(f"${t}" for t in (claim.tickers or ["SPY"])[:3])
    draft = f"{line1}\n{line2}\n\n{tickers}".strip()
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
