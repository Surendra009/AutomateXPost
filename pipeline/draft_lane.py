"""Split pending drafts into stock vs politics lanes for the queue UI."""

from __future__ import annotations

POLITICS_CATEGORIES = frozenset({"geopolitics"})
STOCK_CATEGORIES = frozenset({"earnings", "ai", "ipo", "regulatory", "macro", "other"})


def draft_lane(category: str | None, tickers: str | None = None) -> str:
    """Return 'politics' or 'stock' for queue tab routing."""
    cat = (category or "other").lower().strip()
    if cat in POLITICS_CATEGORIES:
        return "politics"
    if cat in STOCK_CATEGORIES:
        return "stock"
    if tickers and str(tickers).strip():
        return "stock"
    return "politics"


def lane_counts(drafts: list[dict]) -> dict[str, int]:
    counts = {"stock": 0, "politics": 0}
    for item in drafts:
        lane = item.get("lane") or draft_lane(item.get("category"), ",".join(item.get("tickers") or []))
        if lane in counts:
            counts[lane] += 1
    return counts
