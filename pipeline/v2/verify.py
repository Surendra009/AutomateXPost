"""Verify evidence packs into Claims (rules + scout LLM).

Step 3: rule gates first, then scout LLM keep/drop + fact extract for
packs that already meet the evidence minimum bar.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from config import FILTER_MODEL, FILTER_PROVIDER
from logging_config import setup_logging
from pipeline.earnings_freshness import is_current_reporting_period
from pipeline.llm_providers import call_llm
from pipeline.v2.types import Claim, ClaimStatus, EvidencePack, Intent

logger = setup_logging()

SCOUT_SYSTEM_PROMPT = """You are the scout/verifier for a professional markets X account.
You judge ONE candidate event using only the intent + evidence provided.
Never invent numbers, periods, or quotes that are not in the evidence.

Return strict JSON only:
{
  "keep": true|false,
  "confidence": 0.0-1.0,
  "assertion": "one sentence factual claim",
  "tickers": ["NFLX"],
  "period": "Q2-2026" or null,
  "facts": {"eps_actual": "...", "eps_estimate": "...", "key_points": ["..."]},
  "reason": "short why keep or drop"
}

KEEP only when:
- Evidence supports a NEW material event for this intent (print, decision, speak, policy)
- Period matches the intent when the intent has a period (earnings)
- Not a preview, schedule tease, recap, listicle, or wrong-quarter story
- At least one concrete fact is grounded in the evidence snippets

DROP when:
- Wrong fiscal period / old quarter
- Preview / "due to report" / "set to announce"
- Recap / roundup / "last year" primary story
- Opinion, howto, or no trader-relevant fact
- Evidence contradicts the intent or is too thin to trust

Be strict. When uncertain, keep=false.
"""


def verify_packs(
    intents: list[Intent],
    packs: list[EvidencePack],
) -> list[Claim]:
    """Turn packs into keep / drop / waiting claims."""
    by_id = {intent.id: intent for intent in intents}
    claims: list[Claim] = []

    for pack in packs:
        intent = by_id.get(pack.intent_id)
        if intent is None:
            claims.append(
                Claim(
                    intent_id=pack.intent_id,
                    kind="company_material",
                    status="drop",
                    reason="unknown_intent",
                )
            )
            continue

        ruled = _rule_gate(intent, pack)
        if ruled is not None:
            claims.append(ruled)
            continue

        claims.append(_scout_claim(intent, pack))

    keep = sum(1 for c in claims if c.status == "keep")
    drop = sum(1 for c in claims if c.status == "drop")
    waiting = sum(1 for c in claims if c.status == "waiting")
    logger.info("v2 verify: keep=%d drop=%d waiting=%d", keep, drop, waiting)
    return claims


def _rule_gate(intent: Intent, pack: EvidencePack) -> Claim | None:
    """Cheap deterministic gates. Return a Claim to short-circuit scout, else None."""
    urls = [item.url for item in pack.items if item.url]
    meta = intent.metadata or {}
    status = (meta.get("status") or "").lower()

    if status == "preview" or "preview_waiting_for_print" in pack.gaps:
        return Claim(
            intent_id=intent.id,
            kind=intent.kind,
            status="waiting",
            tickers=list(intent.tickers),
            period=intent.period,
            evidence_urls=urls,
            gaps=list(pack.gaps) or ["preview_waiting_for_print"],
            reason=pack.notes or "preview — waiting for print",
        )

    if not pack.meets_minimum:
        return Claim(
            intent_id=intent.id,
            kind=intent.kind,
            status="waiting",
            tickers=list(intent.tickers),
            period=intent.period,
            evidence_urls=urls,
            gaps=list(pack.gaps) or ["insufficient_evidence"],
            reason=pack.notes or "evidence below minimum bar",
        )

    if intent.kind == "earnings_print":
        q, y = _parse_period(intent.period)
        if q and y and not is_current_reporting_period(q, y, as_of=datetime.utcnow().date(), require_period=True):
            return Claim(
                intent_id=intent.id,
                kind=intent.kind,
                status="drop",
                tickers=list(intent.tickers),
                period=intent.period,
                evidence_urls=urls,
                reason=f"off-season period {intent.period}",
                confidence=0.95,
            )
        if not intent.period:
            return Claim(
                intent_id=intent.id,
                kind=intent.kind,
                status="drop",
                tickers=list(intent.tickers),
                evidence_urls=urls,
                reason="earnings intent missing period",
                confidence=0.9,
            )

    # Junk titles across pack (listicle / howto)
    if _pack_looks_like_junk(pack):
        return Claim(
            intent_id=intent.id,
            kind=intent.kind,
            status="drop",
            tickers=list(intent.tickers),
            period=intent.period,
            evidence_urls=urls,
            reason="evidence titles look like junk/listicle",
            confidence=0.85,
        )

    return None


def _scout_claim(intent: Intent, pack: EvidencePack) -> Claim:
    """Scout LLM (or deterministic fallback) for packs that passed rules + minimum bar."""
    urls = [item.url for item in pack.items if item.url]
    parsed = _call_scout_llm(intent, pack)
    if parsed is None:
        return _fallback_claim(intent, pack)

    keep = bool(parsed.get("keep"))
    status: ClaimStatus = "keep" if keep else "drop"
    tickers = parsed.get("tickers") or list(intent.tickers)
    if not isinstance(tickers, list):
        tickers = list(intent.tickers)
    tickers = [str(t).upper() for t in tickers if t][:6] or list(intent.tickers)

    period = parsed.get("period") or intent.period
    if isinstance(period, str):
        period = period.strip() or intent.period
    else:
        period = intent.period

    # Hard override: scout cannot keep an off-season earnings period
    if keep and intent.kind == "earnings_print":
        q, y = _parse_period(period if isinstance(period, str) else intent.period)
        if q and y and not is_current_reporting_period(q, y, as_of=datetime.utcnow().date(), require_period=True):
            return Claim(
                intent_id=intent.id,
                kind=intent.kind,
                status="drop",
                assertion=str(parsed.get("assertion") or "")[:240],
                tickers=tickers,
                period=f"Q{q}-{y}",
                evidence_urls=urls,
                reason="scout keep rejected — off-season period",
                confidence=0.95,
            )

    facts = parsed.get("facts") if isinstance(parsed.get("facts"), dict) else {}
    try:
        confidence = float(parsed.get("confidence") or (0.8 if keep else 0.7))
    except (TypeError, ValueError):
        confidence = 0.75 if keep else 0.7
    confidence = max(0.0, min(1.0, confidence))

    return Claim(
        intent_id=intent.id,
        kind=intent.kind,
        status=status,
        assertion=str(parsed.get("assertion") or "")[:280],
        tickers=tickers,
        period=period if isinstance(period, str) else intent.period,
        evidence_urls=urls,
        facts=facts,
        confidence=confidence,
        reason=str(parsed.get("reason") or ("scout keep" if keep else "scout drop"))[:240],
    )


def _call_scout_llm(intent: Intent, pack: EvidencePack) -> dict[str, Any] | None:
    user = _scout_user_payload(intent, pack)
    raw = call_llm(
        SCOUT_SYSTEM_PROMPT,
        user,
        model=FILTER_MODEL,
        provider=FILTER_PROVIDER,
        max_tokens=700,
        retry=True,
        role="filter",
    )
    if not raw:
        logger.info("v2 scout: no LLM response for %s — using fallback", intent.id)
        return None
    parsed = _parse_scout_json(raw)
    if not parsed:
        logger.warning("v2 scout: JSON parse failed for %s", intent.id)
        return None
    return parsed


def _scout_user_payload(intent: Intent, pack: EvidencePack) -> str:
    evidence_lines = []
    for idx, item in enumerate(pack.items[:6], start=1):
        evidence_lines.append(
            f"{idx}. tier={item.source_tier} title={item.title[:140]}\n"
            f"   url={item.url[:180]}\n"
            f"   snippet={(item.snippet or '')[:320]}"
        )
    meta = {k: intent.metadata.get(k) for k in (
        "status", "date", "hour", "actual", "estimate", "unit",
        "eps_actual", "eps_estimate", "revenue_actual", "revenue_estimate",
        "event_name", "standing",
    ) if k in (intent.metadata or {})}
    return (
        f"INTENT\n"
        f"id: {intent.id}\n"
        f"kind: {intent.kind}\n"
        f"tickers: {intent.tickers}\n"
        f"period: {intent.period}\n"
        f"label: {intent.label}\n"
        f"metadata: {json.dumps(meta, default=str)}\n"
        f"pack_notes: {pack.notes}\n"
        f"gaps: {pack.gaps}\n\n"
        f"EVIDENCE\n" + ("\n".join(evidence_lines) if evidence_lines else "(none)")
    )


def _fallback_claim(intent: Intent, pack: EvidencePack) -> Claim:
    """Deterministic keep when LLM unavailable but pack already met the bar."""
    urls = [item.url for item in pack.items if item.url]
    meta = intent.metadata or {}
    facts: dict[str, Any] = {}
    if intent.kind == "earnings_print":
        for key in ("eps_actual", "eps_estimate", "revenue_actual", "revenue_estimate"):
            if meta.get(key) is not None:
                facts[key] = meta.get(key)
        assertion = f"{intent.tickers[0] if intent.tickers else intent.label} {intent.period} results per calendar/evidence"
        return Claim(
            intent_id=intent.id,
            kind=intent.kind,
            status="keep",
            assertion=assertion[:280],
            tickers=list(intent.tickers),
            period=intent.period,
            evidence_urls=urls,
            facts=facts,
            confidence=0.72,
            reason="fallback keep — scout LLM unavailable; pack met minimum bar",
        )

    if intent.kind in ("company_material", "ai_catalyst", "politics_policy"):
        title = next((i.title for i in pack.items if i.title), intent.label or intent.kind)
        return Claim(
            intent_id=intent.id,
            kind=intent.kind,
            status="keep",
            assertion=str(title)[:280],
            tickers=list(intent.tickers),
            period=intent.period,
            evidence_urls=urls,
            facts=facts,
            confidence=0.68,
            reason="fallback keep — scout LLM unavailable; pack met minimum bar",
        )

    label = intent.label or intent.kind
    if meta.get("actual") is not None:
        facts["actual"] = meta.get("actual")
        facts["estimate"] = meta.get("estimate")
        facts["unit"] = meta.get("unit")
        assertion = f"{label} print {meta.get('actual')} vs {meta.get('estimate')} est"
    else:
        assertion = f"{label} market-relevant update per evidence"

    return Claim(
        intent_id=intent.id,
        kind=intent.kind,
        status="keep",
        assertion=assertion[:280],
        tickers=list(intent.tickers),
        period=intent.period,
        evidence_urls=urls,
        facts=facts,
        confidence=0.68,
        reason="fallback keep — scout LLM unavailable; pack met minimum bar",
    )


def _parse_scout_json(text: str) -> dict[str, Any] | None:
    blob = text.strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob)
        blob = re.sub(r"\s*```$", "", blob)
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = blob.find("{")
    end = blob.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(blob[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


def _parse_period(period: str | None) -> tuple[int | None, int | None]:
    if not period:
        return None, None
    match = re.search(r"Q([1-4])\s*[-/]?\s*(20\d{2})", period, re.I)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


_JUNK_TITLE = re.compile(
    r"\b("
    r"top \d+|stocks to (buy|watch)|what to know|"
    r"how to|opinion|op-ed|newsletter|"
    r"things to know|week ahead"
    r")\b",
    re.I,
)


def _pack_looks_like_junk(pack: EvidencePack) -> bool:
    non_calendar = [i for i in pack.items if i.metadata.get("kind") != "calendar"]
    if not non_calendar:
        return False
    junk = sum(1 for i in non_calendar if _JUNK_TITLE.search(i.title or ""))
    return junk >= max(1, len(non_calendar)) and junk == len(non_calendar)
