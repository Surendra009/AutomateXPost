"""Directed research — LLM proposes follow-ups; fetchers fill pack gaps.

Step 0: no-op. Runs only after scout keep + listed gaps (Step 4).
"""

from __future__ import annotations

from logging_config import setup_logging
from pipeline.v2.types import Claim, EvidencePack

logger = setup_logging()


def research_gaps(
    claims: list[Claim],
    packs: list[EvidencePack],
) -> tuple[list[Claim], list[EvidencePack], int]:
    """Fatten thin packs for keep/waiting claims that list gaps.

    Returns updated claims, packs, and count of research passes attempted.
    Step 0: returns inputs unchanged.
    """
    researched = 0
    logger.info("v2 research: skipped (scaffold) — %d claims with gaps", sum(1 for c in claims if c.gaps))
    return claims, packs, researched
