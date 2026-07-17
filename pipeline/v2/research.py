"""Directed research — LLM proposes follow-ups; fetchers fill pack gaps.

Step 4: for keep/waiting claims with researchable gaps, ask the researcher
LLM for queries, fetch more evidence, reassess the pack, then re-scout.
"""

from __future__ import annotations

import json
import re
from typing import Any

from config import FILTER_MODEL, FILTER_PROVIDER
from logging_config import setup_logging
from pipeline.llm_providers import call_llm
from pipeline.v2.evidence import fetch_followup_items, reassess_pack
from pipeline.v2.types import Claim, EvidencePack, Intent
from pipeline.v2.verify import verify_packs

logger = setup_logging()

_MAX_RESEARCH_PER_CYCLE = 5
_RESEARCHABLE_GAPS = frozenset(
    {
        "need_body",
        "need_primary_or_wire",
        "need_news_confirmation",
        "need_fed_coverage",
        "need_relevant_headline",
        "need_evidence",
        "need_numbers",
    }
)

RESEARCH_SYSTEM_PROMPT = """You plan follow-up news searches to fill evidence gaps for a markets post.
Return strict JSON only:
{
  "queries": ["search query 1", "search query 2"],
  "reason": "what gap these queries target"
}

Rules:
- 1 to 3 highly specific queries (Google News style)
- Include ticker/period/label when known
- Prefer primary sources and Tier-1 wires
- No queries for previews or wrong-quarter hunts
- If gaps cannot be fixed by search, return {"queries": [], "reason": "..."}
"""


def research_gaps(
    intents: list[Intent],
    claims: list[Claim],
    packs: list[EvidencePack],
) -> tuple[list[Claim], list[EvidencePack], int]:
    """Fatten packs for researchable gaps; re-verify affected claims."""
    by_intent = {intent.id: intent for intent in intents}
    packs_by_id = {pack.intent_id: pack for pack in packs}
    researched_ids: list[str] = []

    candidates = [c for c in claims if _should_research(c, packs_by_id.get(c.intent_id))]
    for claim in candidates[:_MAX_RESEARCH_PER_CYCLE]:
        intent = by_intent.get(claim.intent_id)
        pack = packs_by_id.get(claim.intent_id)
        if intent is None or pack is None:
            continue

        queries = _plan_queries(intent, claim, pack)
        if not queries:
            continue

        new_items = fetch_followup_items(intent, queries)
        if not new_items:
            logger.info("v2 research: no new items for %s queries=%s", intent.id, queries)
            continue

        pack.items.extend(new_items)
        reassess_pack(intent, pack)
        researched_ids.append(intent.id)
        logger.info(
            "v2 research: %s +%d items meets_minimum=%s gaps=%s",
            intent.id,
            len(new_items),
            pack.meets_minimum,
            pack.gaps,
        )

    if researched_ids:
        subset_intents = [by_intent[i] for i in researched_ids if i in by_intent]
        subset_packs = [packs_by_id[i] for i in researched_ids if i in packs_by_id]
        refreshed = {c.intent_id: c for c in verify_packs(subset_intents, subset_packs)}
        claims = [refreshed.get(c.intent_id, c) for c in claims]

    logger.info(
        "v2 research: attempted=%d researched=%d",
        min(len(candidates), _MAX_RESEARCH_PER_CYCLE),
        len(researched_ids),
    )
    return claims, list(packs_by_id.values()), len(researched_ids)


def _should_research(claim: Claim, pack: EvidencePack | None) -> bool:
    if pack is None:
        return False
    if claim.status == "drop":
        return False
    gaps = set(claim.gaps or []) | set(pack.gaps or [])
    if "preview_waiting_for_print" in gaps:
        return False
    # Keep claims with body gaps, or waiting claims that might unlock with more news
    if claim.status == "keep" and gaps & _RESEARCHABLE_GAPS:
        return True
    if claim.status == "waiting" and gaps & _RESEARCHABLE_GAPS:
        return True
    return False


def _plan_queries(intent: Intent, claim: Claim, pack: EvidencePack) -> list[str]:
    planned = _call_researcher_llm(intent, claim, pack)
    if planned:
        return planned[:3]
    return _deterministic_queries(intent, pack)[:3]


def _call_researcher_llm(intent: Intent, claim: Claim, pack: EvidencePack) -> list[str]:
    payload = {
        "intent": {
            "id": intent.id,
            "kind": intent.kind,
            "tickers": intent.tickers,
            "period": intent.period,
            "label": intent.label,
        },
        "claim_status": claim.status,
        "gaps": sorted(set(claim.gaps or []) | set(pack.gaps or [])),
        "existing_titles": [i.title[:120] for i in pack.items[:5]],
    }
    raw = call_llm(
        RESEARCH_SYSTEM_PROMPT,
        json.dumps(payload, default=str),
        model=FILTER_MODEL,
        provider=FILTER_PROVIDER,
        max_tokens=400,
        retry=True,
        role="filter",
    )
    if not raw:
        return []
    parsed = _parse_json_obj(raw)
    if not parsed:
        return []
    queries = parsed.get("queries") or []
    if not isinstance(queries, list):
        return []
    return [str(q).strip() for q in queries if str(q).strip()][:3]


def _deterministic_queries(intent: Intent, pack: EvidencePack) -> list[str]:
    gaps = set(pack.gaps or [])
    label = intent.label or (intent.tickers[0] if intent.tickers else intent.kind)
    period = intent.period or ""
    out: list[str] = []

    if intent.kind == "earnings_print":
        symbol = intent.tickers[0] if intent.tickers else label
        if "need_body" in gaps or "need_primary_or_wire" in gaps:
            out.append(f'"{symbol}" {period} earnings (release OR results OR "press release")')
            out.append(f'"{symbol}" {period} (EPS OR revenue) (beat OR miss)')
        elif "need_numbers" in gaps:
            out.append(f'"{symbol}" {period} EPS revenue results')
    elif intent.kind in ("fed_decision", "fed_speak"):
        out.append('FOMC OR Powell OR "Federal Reserve" (statement OR speech OR decision) when:1d')
        out.append('"Federal Reserve" rates (inflation OR policy) when:1d')
    elif intent.kind == "company_material":
        symbol = intent.tickers[0] if intent.tickers else label
        out.append(f'"{symbol}" (merger OR acquisition OR guidance OR CEO) when:1d')
        out.append(f'"{symbol}" stock news -opinion when:1d')
    elif intent.kind == "ai_catalyst":
        out.append(f"{label} (model OR launch OR release OR partnership) when:1d")
        out.append("(OpenAI OR Anthropic OR Nvidia OR Google) AI (launch OR model) when:1d")
    elif intent.kind == "politics_policy":
        out.append(f"{label} (tariff OR sanction OR bill OR ban) markets when:1d")
        out.append("(White House OR Congress) (trade OR tariff) stocks when:1d")
    else:
        out.append(f'"{label}" (beats OR misses OR rises OR falls) when:1d')
        out.append(f'"{label}" economy markets when:1d')

    # Prefer unused queries vs ones already on the intent
    existing = {q.lower() for q in (intent.queries or [])}
    return [q for q in out if q.lower() not in existing] or out


def _parse_json_obj(text: str) -> dict[str, Any] | None:
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
