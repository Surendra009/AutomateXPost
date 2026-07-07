"""Overnight pipeline schedule — quiet 10 PM–5 AM, catch-up at 5 AM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (
    OVERNIGHT_CATCHUP_HOUR,
    OVERNIGHT_CATCHUP_MAX_AGE_HOURS,
    OVERNIGHT_QUIET_END_HOUR,
    OVERNIGHT_QUIET_START_HOUR,
    PIPELINE_TIMEZONE,
)
from database import get_setting

CATCHUP_SETTING_KEY = "pipeline_last_overnight_catchup_at"


@dataclass(frozen=True)
class ScheduleDecision:
    run: bool
    mode: str  # normal | catchup | skipped | manual
    reason: str
    max_news_age_hours: int | None = None


def local_now(tz_name: str | None = None) -> datetime:
    tz = ZoneInfo(tz_name or PIPELINE_TIMEZONE)
    return datetime.now(tz)


def is_overnight_quiet_hours(now: datetime | None = None) -> bool:
    """True during 10 PM – 4:59 AM local (skip scheduled runs)."""
    now = now or local_now()
    hour = now.hour
    return hour >= OVERNIGHT_QUIET_START_HOUR or hour < OVERNIGHT_QUIET_END_HOUR


def _parse_catchup_at(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except ValueError:
        return None


def catchup_completed_today(now: datetime | None = None) -> bool:
    now = now or local_now()
    last = _parse_catchup_at(get_setting(CATCHUP_SETTING_KEY), now.tzinfo)
    return last is not None and last.date() == now.date()


def evaluate_schedule(*, force: bool = False, now: datetime | None = None) -> ScheduleDecision:
    """Decide whether the scheduler should run and in which mode."""
    if force:
        return ScheduleDecision(
            run=True,
            mode="manual",
            reason="manual trigger",
        )

    now = now or local_now()

    if is_overnight_quiet_hours(now):
        return ScheduleDecision(
            run=False,
            mode="skipped",
            reason="overnight quiet hours (10pm–5am)",
        )

    if now.hour == OVERNIGHT_CATCHUP_HOUR:
        if catchup_completed_today(now):
            return ScheduleDecision(
                run=False,
                mode="skipped",
                reason="5am catch-up already completed today",
            )
        return ScheduleDecision(
            run=True,
            mode="catchup",
            reason="5am overnight catch-up (10pm–5am window)",
            max_news_age_hours=OVERNIGHT_CATCHUP_MAX_AGE_HOURS,
        )

    return ScheduleDecision(
        run=True,
        mode="normal",
        reason="active hours (6am–10pm)",
    )


def schedule_status(now: datetime | None = None) -> dict:
    """Snapshot for settings UI and pipeline status."""
    now = now or local_now()
    decision = evaluate_schedule(now=now)
    return {
        "timezone": str(now.tzinfo),
        "local_time": now.isoformat(),
        "quiet_hours": is_overnight_quiet_hours(now),
        "quiet_window": f"{OVERNIGHT_QUIET_START_HOUR}:00–{OVERNIGHT_QUIET_END_HOUR}:00",
        "next_mode": decision.mode if decision.run else "skipped",
        "schedule_reason": decision.reason,
        "catchup_completed_today": catchup_completed_today(now),
        "last_overnight_catchup_at": get_setting(CATCHUP_SETTING_KEY),
    }
