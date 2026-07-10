"""Parse UTC timestamps from settings/API (naive or Z-suffixed)."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_utc_naive(value: str | None) -> datetime | None:
    """Return a naive UTC datetime for comparison with datetime.utcnow()."""
    if not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None
