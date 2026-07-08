"""User-configurable duplicate handling."""

from __future__ import annotations

from database import get_setting

DEDUP_MODES = ("pipeline", "queue", "off")

DEDUP_MODE_LABELS = {
    "pipeline": "Before draft (recommended)",
    "queue": "In queue (show best only)",
    "off": "Off",
}


def get_dedup_mode() -> str:
    mode = get_setting("dedup_mode", "pipeline")
    return mode if mode in DEDUP_MODES else "pipeline"


def dedup_at_pipeline() -> bool:
    """Block duplicate stories before creating drafts (saves LLM cost)."""
    return get_dedup_mode() == "pipeline"


def dedup_at_ingest() -> bool:
    """Cross-source title dedup when ingesting RSS/API headlines."""
    return get_dedup_mode() == "pipeline"


def dedup_at_queue() -> bool:
    """Collapse duplicate pending drafts when loading the queue."""
    return get_dedup_mode() in ("pipeline", "queue")
