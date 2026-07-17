"""Claim-centric pipeline v2 (Intent → Evidence → Verify → Research → Write).

Step 0: scaffold + parallel cycle hook. Does not replace the legacy pipeline
or create drafts yet — only builds a coverage report for comparison.
"""

from pipeline.v2.cycle import run_v2_cycle
from pipeline.v2.types import Claim, CycleReport, EvidencePack, Intent

__all__ = [
    "Claim",
    "CycleReport",
    "EvidencePack",
    "Intent",
    "run_v2_cycle",
]
