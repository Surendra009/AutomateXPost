"""Webhook alerts for pipeline and posting failures."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx

from config import ALERT_WEBHOOK_URL
from database import get_session, get_setting, set_setting
from logging_config import setup_logging

logger = setup_logging()

_LAST_ALERT_KEY = "alert_last_sent_at"
_ALERT_COOLDOWN_MINUTES = 30


def send_alert(title: str, message: str, *, level: str = "warning") -> bool:
    """POST to ALERT_WEBHOOK_URL (Slack-compatible JSON). Rate-limited."""
    if not ALERT_WEBHOOK_URL:
        return False

    last = get_setting(_LAST_ALERT_KEY)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if datetime.utcnow() - last_dt < timedelta(minutes=_ALERT_COOLDOWN_MINUTES):
                return False
        except ValueError:
            pass

    payload = {
        "text": f"[PostPilot {level.upper()}] {title}\n{message}",
        "title": title,
        "message": message,
        "level": level,
        "timestamp": datetime.utcnow().isoformat(),
    }
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                ALERT_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        set_setting(_LAST_ALERT_KEY, datetime.utcnow().isoformat())
        logger.info("Alert sent: %s", title)
        return True
    except Exception as exc:
        logger.warning("Alert webhook failed: %s", exc)
        return False


def check_pipeline_health(drafts_created: int, error: str | None) -> None:
    if error:
        send_alert("Pipeline error", error, level="error")
        return

    last_run = get_setting("pipeline_last_run_at")
    if not last_run:
        return

    try:
        last_dt = datetime.fromisoformat(last_run)
    except ValueError:
        return

    if datetime.utcnow() - last_dt > timedelta(hours=24):
        send_alert(
            "Pipeline stale",
            "No successful pipeline run in 24+ hours.",
            level="warning",
        )

    if drafts_created == 0 and datetime.utcnow().hour in (9, 12, 16):
        zero_key = "alert_zero_drafts_today"
        if not get_setting(zero_key):
            send_alert(
                "No drafts created",
                "Latest pipeline cycle created zero drafts during market hours.",
                level="info",
            )
            set_setting(zero_key, datetime.utcnow().date().isoformat())
        elif get_setting(zero_key) != datetime.utcnow().date().isoformat():
            set_setting(zero_key, datetime.utcnow().date().isoformat())
