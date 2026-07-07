"""Per-cycle overrides (e.g. extended freshness during overnight catch-up)."""

from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from typing import Iterator

from config import MAX_NEWS_AGE_HOURS

_max_news_age_hours: ContextVar[int | None] = ContextVar("max_news_age_hours", default=None)


def get_max_news_age_hours() -> int:
    return _max_news_age_hours.get() or MAX_NEWS_AGE_HOURS


@contextmanager
def cycle_max_news_age(hours: int | None) -> Iterator[None]:
    token = _max_news_age_hours.set(hours)
    try:
        yield
    finally:
        _max_news_age_hours.reset(token)
