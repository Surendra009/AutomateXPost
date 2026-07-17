"""Orchestrate one v2 cycle: Intent → Evidence → Verify → Research → Write.

Runs alongside the legacy pipeline. Step 2 fetches evidence packs and
enforces minimum bars; scout/write remain dry-run stubs (no drafts).
"""

from __future__ import annotations

import time
from typing import Any

from logging_config import setup_logging
from pipeline.v2.evidence import fetch_evidence_packs
from pipeline.v2.intent import build_intent_board
from pipeline.v2.research import research_gaps
from pipeline.v2.types import CycleReport
from pipeline.v2.verify import verify_packs
from pipeline.v2.write import write_from_claims

logger = setup_logging()


def run_v2_cycle(*, enabled: bool = True, dry_run: bool = True) -> CycleReport:
    """Execute the claim-centric pipeline once. Safe no-op when disabled."""
    started = time.perf_counter()
    report = CycleReport(enabled=enabled, dry_run=dry_run)

    if not enabled:
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.debug("v2 cycle skipped — disabled")
        return report

    try:
        intents = build_intent_board()
        report.intents = len(intents)

        packs = fetch_evidence_packs(intents)
        report.packs = len(packs)
        report.packs_ready = sum(1 for pack in packs if pack.meets_minimum)
        packs_by_id = {pack.intent_id: pack for pack in packs}

        report.intent_summaries = []
        for intent in intents:
            pack = packs_by_id.get(intent.id)
            report.intent_summaries.append(
                {
                    "id": intent.id,
                    "kind": intent.kind,
                    "tickers": intent.tickers,
                    "period": intent.period,
                    "label": intent.label,
                    "meets_minimum": bool(pack and pack.meets_minimum),
                    "gaps": list(pack.gaps) if pack else [],
                    "evidence_count": len(pack.items) if pack else 0,
                    "notes": (pack.notes if pack else "")[:160],
                }
            )

        claims = verify_packs(intents, packs)
        claims, packs, researched = research_gaps(claims, packs)
        report.researched = researched
        report.claims_keep = sum(1 for c in claims if c.status == "keep")
        report.claims_drop = sum(1 for c in claims if c.status == "drop")
        report.claims_waiting = sum(1 for c in claims if c.status == "waiting")

        report.drafted = write_from_claims(claims, dry_run=dry_run)
    except Exception as exc:
        logger.exception("v2 cycle failed: %s", exc)
        report.errors.append(str(exc)[:240])

    report.duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "v2 cycle done: intents=%d packs_ready=%d keep=%d drop=%d waiting=%d "
        "researched=%d drafted=%d dry_run=%s ms=%d",
        report.intents,
        report.packs_ready,
        report.claims_keep,
        report.claims_drop,
        report.claims_waiting,
        report.researched,
        report.drafted,
        report.dry_run,
        report.duration_ms,
    )
    return report


def report_for_status(report: CycleReport | None) -> dict[str, Any]:
    """Shape stored on pipeline status / settings."""
    if report is None:
        return {"enabled": False, "ran": False}
    data = report.to_dict()
    data["ran"] = True
    return data
