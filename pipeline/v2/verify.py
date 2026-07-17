"""Verify evidence packs into Claims (rules + scout LLM).

Step 0: no LLM. Packs that do not meet minimum → waiting.
Later: rules (age/period/junk) then scout LLM keep/drop + fact extract.
"""

from __future__ import annotations

from logging_config import setup_logging
from pipeline.v2.types import Claim, EvidencePack, Intent

logger = setup_logging()


def verify_packs(
    intents: list[Intent],
    packs: list[EvidencePack],
) -> list[Claim]:
    """Turn packs into keep / drop / waiting claims. Step 0: all waiting or empty."""
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

        if not pack.meets_minimum:
            claims.append(
                Claim(
                    intent_id=intent.id,
                    kind=intent.kind,
                    status="waiting",
                    tickers=list(intent.tickers),
                    period=intent.period,
                    gaps=list(pack.gaps) or ["insufficient_evidence"],
                    reason=pack.notes or "evidence below minimum bar",
                )
            )
            continue

        # Step 3 will run scout LLM here for keep/drop + assertion/facts.
        claims.append(
            Claim(
                intent_id=intent.id,
                kind=intent.kind,
                status="waiting",
                tickers=list(intent.tickers),
                period=intent.period,
                evidence_urls=[item.url for item in pack.items if item.url],
                gaps=["scout_llm_not_wired"],
                reason="Evidence ready — scout verifier not implemented yet",
            )
        )

    keep = sum(1 for c in claims if c.status == "keep")
    drop = sum(1 for c in claims if c.status == "drop")
    waiting = sum(1 for c in claims if c.status == "waiting")
    logger.info("v2 verify: keep=%d drop=%d waiting=%d", keep, drop, waiting)
    return claims
