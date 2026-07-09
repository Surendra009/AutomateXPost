"""Post new drafts to a Microsoft Teams channel via Incoming Webhook."""

from __future__ import annotations

from datetime import datetime

import httpx

from config import APP_BASE_URL, TEAMS_WEBHOOK_URL
from database import get_setting
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.channel_notify import drafts_since

logger = setup_logging()


def teams_configured() -> bool:
    return bool(TEAMS_WEBHOOK_URL)


def _impact_color(impact: str) -> str:
    return {"high": "C50F1F", "med": "0078D4", "low": "5C5C5C"}.get(impact, "0078D4")


def _format_draft_card(draft: Draft, headline: Headline | None) -> dict:
    tickers = draft.tickers.replace(",", ", ") if draft.tickers else "—"
    source = headline.source if headline else "PostPilot"
    title = headline.title if headline else "New draft"
    body = draft.text.strip()
    if len(body) > 900:
        body = body[:897] + "…"

    facts = [
        {"name": "Category", "value": draft.category or "other"},
        {"name": "Impact", "value": draft.impact or "med"},
        {"name": "Tickers", "value": tickers},
        {"name": "Source", "value": source},
    ]

    section: dict = {
        "activityTitle": title[:120],
        "activitySubtitle": f"{draft.category} · {draft.impact} impact",
        "text": body,
        "facts": facts,
    }
    if APP_BASE_URL:
        section["markdown"] = True
        section["text"] = f"{body}\n\n[Open in PostPilot]({APP_BASE_URL.rstrip('/')}/)"

    return {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": _impact_color(draft.impact or "med"),
        "summary": title[:120],
        "sections": [section],
    }


def _post_to_teams(payload: dict) -> bool:
    if not TEAMS_WEBHOOK_URL:
        return False
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                TEAMS_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Teams webhook failed: %s", exc)
        return False


def send_draft_to_teams(draft: Draft, headline: Headline | None) -> bool:
    return _post_to_teams(_format_draft_card(draft, headline))


def send_teams_test_message() -> bool:
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": "0078D4",
        "summary": "PostPilot connected",
        "sections": [
            {
                "activityTitle": "PostPilot → Microsoft Teams",
                "text": "Webhook is working. New drafts will appear here when the pipeline creates them.",
            }
        ],
    }
    return _post_to_teams(payload)


def notify_teams_new_drafts(since: datetime) -> int:
    if not teams_configured() or not get_setting("teams_enabled", False):
        return 0

    pairs = drafts_since(since)
    if not pairs:
        return 0

    sent = 0
    for draft, headline in pairs:
        if send_draft_to_teams(draft, headline):
            sent += 1

    if sent:
        logger.info("Teams: posted %d draft(s)", sent)
    return sent
