"""Write drafts from approved Claims only.

Step 0: dry-run — never creates queue drafts. Step 5 wires templates/writer LLM.
"""

from __future__ import annotations

from logging_config import setup_logging
from pipeline.v2.types import Claim

logger = setup_logging()


def write_from_claims(claims: list[Claim], *, dry_run: bool = True) -> int:
    """Create pending drafts from status=keep claims. Step 0 always returns 0."""
    eligible = [c for c in claims if c.status == "keep"]
    if dry_run:
        logger.info(
            "v2 write: dry-run — would draft %d keep claims (scaffold creates none)",
            len(eligible),
        )
        return 0

    # Step 5: template or writer LLM from Claim + evidence quotes only.
    logger.info("v2 write: not implemented — %d eligible claims ignored", len(eligible))
    return 0
