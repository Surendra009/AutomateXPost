"""Shared types for the claim-centric pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

IntentKind = Literal[
    "earnings_print",
    "company_material",
    "macro_print",
    "fed_decision",
    "fed_speak",
    "ai_catalyst",
    "politics_policy",
    "macro_reaction",
]

ClaimStatus = Literal["keep", "drop", "waiting"]


@dataclass
class Intent:
    """Something the cycle must try to resolve (or explicitly miss)."""

    kind: IntentKind
    id: str
    tickers: list[str] = field(default_factory=list)
    period: str | None = None  # e.g. "Q2-2026"
    label: str | None = None  # e.g. "CPI", "FOMC"
    window_hours: float = 8.0
    queries: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceItem:
    """One retrieved source for an intent."""

    url: str
    title: str
    source_tier: int  # 1=IR/official, 2=tier-1 wire, 3=search/other
    published_at: datetime | None = None
    snippet: str = ""
    body_chars: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.published_at is not None:
            data["published_at"] = self.published_at.isoformat()
        return data


@dataclass
class EvidencePack:
    """All evidence gathered for one intent."""

    intent_id: str
    items: list[EvidenceItem] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)  # e.g. need_guidance, need_ir
    meets_minimum: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "items": [item.to_dict() for item in self.items],
            "gaps": list(self.gaps),
            "meets_minimum": self.meets_minimum,
            "notes": self.notes,
        }


@dataclass
class Claim:
    """Verified assertion ready for research/write (or dropped/waiting)."""

    intent_id: str
    kind: IntentKind
    status: ClaimStatus
    assertion: str = ""
    tickers: list[str] = field(default_factory=list)
    period: str | None = None
    evidence_urls: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CycleReport:
    """Coverage metrics for one v2 cycle — the learning signal."""

    intents: int = 0
    packs: int = 0
    packs_ready: int = 0
    claims_keep: int = 0
    claims_drop: int = 0
    claims_waiting: int = 0
    researched: int = 0
    drafted: int = 0
    errors: list[str] = field(default_factory=list)
    intent_summaries: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    dry_run: bool = True  # Step 0: never writes drafts
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
