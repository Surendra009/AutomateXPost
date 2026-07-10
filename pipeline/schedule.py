"""Overnight pipeline schedule — quiet 10 PM–5 AM, catch-up at 5 AM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (
    EARNINGS_WINDOW_END_HOUR,
    MARKET_CLOSE_HOUR,
    OVERNIGHT_CATCHUP_HOUR,
    OVERNIGHT_CATCHUP_MAX_AGE_HOURS,
    OVERNIGHT_QUIET_END_HOUR,
    OVERNIGHT_QUIET_START_HOUR,
    PIPELINE_TIMEZONE,
    WEEKEND_INTERVAL_HOURS,
)
from database import get_setting
from logging_config import setup_logging

logger = setup_logging()

CATCHUP_SETTING_KEY = "pipeline_last_overnight_catchup_at"


@dataclass(frozen=True)
class ScheduleDecision:
    run: bool
    mode: str  # normal | catchup | skipped | manual
    reason: str
    max_news_age_hours: int | None = None


def local_now(tz_name: str | None = None) -> datetime:
    name = tz_name or PIPELINE_TIMEZONE
    try:
        tz = ZoneInfo(name)
    except Exception:
        logger.warning("Timezone %s unavailable, using UTC", name)
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


def is_overnight_quiet_hours(now: datetime | None = None) -> bool:
    """True during 10 PM – 4:59 AM local (skip scheduled runs)."""
    now = now or local_now()
    hour = now.hour
    return hour >= OVERNIGHT_QUIET_START_HOUR or hour < OVERNIGHT_QUIET_END_HOUR


def is_weekend(now: datetime | None = None) -> bool:
    """True on Saturday or Sunday (local time)."""
    now = now or local_now()
    return now.weekday() >= 5


def _parse_run_at(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        return dt.astimezone(tz)
    except ValueError:
        return None


def weekend_run_due(now: datetime | None = None) -> bool:
    """True if enough time has passed since the last pipeline run for weekend throttle."""
    now = now or local_now()
    last = _parse_run_at(get_setting("pipeline_last_run_at"), now.tzinfo)
    if last is None:
        return True
    elapsed_hours = (now - last).total_seconds() / 3600
    return elapsed_hours >= WEEKEND_INTERVAL_HOURS


def _parse_catchup_at(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
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
            max_news_age_hours=OVERNIGHT_CATCHUP_MAX_AGE_HOURS,
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
            pass  # fall through to weekend / normal checks
        else:
            return ScheduleDecision(
                run=True,
                mode="catchup",
                reason="5am overnight catch-up (10pm–5am window)",
                max_news_age_hours=OVERNIGHT_CATCHUP_MAX_AGE_HOURS,
            )

    if is_weekend(now) and not weekend_run_due(now):
        return ScheduleDecision(
            run=False,
            mode="skipped",
            reason=f"weekend throttle (every {WEEKEND_INTERVAL_HOURS}h)",
        )

    if now.hour == OVERNIGHT_CATCHUP_HOUR and catchup_completed_today(now):
        return ScheduleDecision(
            run=False,
            mode="skipped",
            reason="5am catch-up already completed today",
        )

    mode = "weekend" if is_weekend(now) else "normal"
    reason = (
        f"weekend active hours (every {WEEKEND_INTERVAL_HOURS}h)"
        if is_weekend(now)
        else "active hours (6am–10pm)"
    )
    return ScheduleDecision(
        run=True,
        mode=mode,
        reason=reason,
    )


def schedule_status(now: datetime | None = None) -> dict:
    """Snapshot for settings UI and pipeline status."""
    now = now or local_now()
    decision = evaluate_schedule(now=now)
    return {
        "timezone": str(now.tzinfo),
        "local_time": now.isoformat(),
        "is_weekend": is_weekend(now),
        "weekend_interval_hours": WEEKEND_INTERVAL_HOURS,
        "weekend_run_due": weekend_run_due(now) if is_weekend(now) else None,
        "quiet_hours": is_overnight_quiet_hours(now),
        "quiet_window": f"{OVERNIGHT_QUIET_START_HOUR}:00–{OVERNIGHT_QUIET_END_HOUR}:00",
        "market_hours": is_market_hours(now),
        "earnings_window": is_earnings_window(now),
        "pipeline_interval_seconds": pipeline_interval_seconds(now),
        "next_mode": decision.mode if decision.run else "skipped",
        "schedule_reason": decision.reason,
        "catchup_completed_today": catchup_completed_today(now),
        "last_overnight_catchup_at": get_setting(CATCHUP_SETTING_KEY),
    }


def is_earnings_window(now: datetime | None = None) -> bool:
    """Mon–Fri after the close through early evening — faster polling for AMC results."""
    now = now or local_now()
    if now.weekday() >= 5:
        return False
    return MARKET_CLOSE_HOUR <= now.hour < EARNINGS_WINDOW_END_HOUR


def is_market_hours(now: datetime | None = None) -> bool:
    """US market session window (premarket through close, Mon–Fri)."""
    from config import MARKET_CLOSE_HOUR, PREMARKET_START_HOUR

    now = now or local_now()
    if now.weekday() >= 5:
        return False
    return PREMARKET_START_HOUR <= now.hour < MARKET_CLOSE_HOUR


def pipeline_interval_seconds(now: datetime | None = None) -> int:
    from config import MARKET_HOURS_INTERVAL_SECONDS, PIPELINE_INTERVAL_SECONDS

    if is_market_hours(now) or is_earnings_window(now):
        return MARKET_HOURS_INTERVAL_SECONDS
    return PIPELINE_INTERVAL_SECONDS
