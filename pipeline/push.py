"""Web push notifications for new drafts."""

from __future__ import annotations

import json
from datetime import datetime

from sqlmodel import select

from config import VAPID_CLAIMS_EMAIL, VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY
from database import get_session, get_setting
from logging_config import setup_logging
from models import PushSubscription

logger = setup_logging()


def push_configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def get_vapid_public_key() -> str:
    return VAPID_PUBLIC_KEY


def save_subscription(endpoint: str, p256dh: str, auth: str) -> None:
    with get_session() as session:
        existing = session.exec(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        ).first()
        if existing:
            existing.p256dh = p256dh
            existing.auth = auth
            session.add(existing)
        else:
            session.add(PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth))
        session.commit()


def remove_subscription(endpoint: str) -> None:
    with get_session() as session:
        row = session.exec(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        ).first()
        if row:
            session.delete(row)
            session.commit()


def notify_new_drafts(count: int, *, high_impact: int = 0) -> int:
    """Send push to all subscribers. Returns number sent."""
    if not push_configured() or not get_setting("push_enabled", True):
        return 0
    if count <= 0:
        return 0

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed — push disabled")
        return 0

    title = f"{count} new draft{'s' if count != 1 else ''}"
    body = "High-impact story waiting" if high_impact else "Review in PostPilot"
    payload = json.dumps({"title": title, "body": body, "count": count})
    sent = 0
    stale: list[str] = []

    with get_session() as session:
        subs = session.exec(select(PushSubscription)).all()

    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
            )
            sent += 1
        except Exception as exc:
            name = type(exc).__name__
            if "WebPushException" in name or "410" in str(exc) or "404" in str(exc):
                stale.append(sub.endpoint)
            logger.debug("Push failed for %s: %s", sub.endpoint[:40], exc)

    for endpoint in stale:
        remove_subscription(endpoint)

    if sent:
        logger.info("Push sent to %d subscribers", sent)
    return sent
