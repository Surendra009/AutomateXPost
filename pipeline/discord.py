"""Post new drafts to a Discord channel via webhook."""

from __future__ import annotations

from datetime import datetime

import httpx

from config import APP_BASE_URL, DISCORD_WEBHOOK_URL
from database import get_setting
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.channel_notify import drafts_since

logger = setup_logging()


def discord_configured() -> bool:
    return bool(DISCORD_WEBHOOK_URL)


def _impact_color(impact: str) -> int:
    return {"high": 0xC50F1F, "med": 0x0078D4, "low": 0x5C5C5C}.get(impact, 0x5865F2)


def _format_draft_embed(draft: Draft, headline: Headline | None) -> dict:
    tickers = draft.tickers.replace(",", ", ") if draft.tickers else "—"
    source = headline.source if headline else "PostPilot"
    title = (headline.title if headline else "New draft")[:256]
    body = draft.text.strip()
    if len(body) > 3800:
        body = body[:3797] + "…"

    description = body
    if APP_BASE_URL:
        description = f"{body}\n\n[Open in PostPilot]({APP_BASE_URL.rstrip('/')}/)"

    return {
        "title": title,
        "description": description,
        "color": _impact_color(draft.impact or "med"),
        "fields": [
            {"name": "Category", "value": draft.category or "other", "inline": True},
            {"name": "Impact", "value": draft.impact or "med", "inline": True},
            {"name": "Tickers", "value": tickers, "inline": True},
            {"name": "Source", "value": source[:256], "inline": False},
        ],
        "footer": {"text": "PostPilot"},
    }


def _post_to_discord(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(DISCORD_WEBHOOK_URL, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Discord webhook failed: %s", exc)
        return False


def send_draft_to_discord(draft: Draft, headline: Headline | None) -> bool:
    return _post_to_discord({"embeds": [_format_draft_embed(draft, headline)]})


def send_discord_test_message() -> bool:
    return _post_to_discord(
        {
            "embeds": [
                {
                    "title": "PostPilot → Discord",
                    "description": (
                        "Webhook is working. New drafts will appear here when the pipeline creates them."
                    ),
                    "color": 0x5865F2,
                    "footer": {"text": "PostPilot"},
                }
            ]
        }
    )


def notify_discord_new_drafts(since: datetime) -> int:
    """Send new pending drafts from this pipeline cycle to Discord."""
    if not discord_configured() or not get_setting("discord_enabled", True):
        return 0

    pairs = drafts_since(since)
    if not pairs:
        return 0

    sent = 0
    for draft, headline in pairs:
        if send_draft_to_discord(draft, headline):
            sent += 1

    if sent:
        logger.info("Discord: posted %d draft(s)", sent)
    return sent
