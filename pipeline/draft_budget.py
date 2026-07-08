"""Shared per-cycle draft cap across structured and LLM paths."""

from __future__ import annotations

from config import MAX_DRAFTS_PER_CYCLE


class DraftBudget:
    """Track drafts created in one pipeline cycle."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit if limit is not None else MAX_DRAFTS_PER_CYCLE
        self.created = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.created)

    def try_take(self, n: int = 1) -> bool:
        if self.created + n > self.limit:
            return False
        self.created += n
        return True
