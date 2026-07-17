"""Fetch evidence packs per intent (search, IR, wires).

Step 0: no retrieval. Later steps call search/IR and enforce a minimum bar.
LLM is not used here — only APIs and parsers.
"""

from __future__ import annotations

from logging_config import setup_logging
from pipeline.v2.types import EvidencePack, Intent

logger = setup_logging()


def fetch_evidence_packs(intents: list[Intent]) -> list[EvidencePack]:
    """Gather evidence for each intent. Step 0 returns empty packs."""
    packs: list[EvidencePack] = []
    for intent in intents:
        packs.append(
            EvidencePack(
                intent_id=intent.id,
                items=[],
                gaps=["scaffold_no_retrieval"],
                meets_minimum=False,
                notes="Step 0: evidence fetch not implemented",
            )
        )
    logger.info("v2 evidence: %d packs (%d ready)", len(packs), sum(1 for p in packs if p.meets_minimum))
    return packs
