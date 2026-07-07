"""LLM draft step — generate X post text."""

import json
from datetime import datetime

from config import ANTHROPIC_API_KEY, DRAFT_MODEL
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.filter import _call_claude, _parse_json_array

logger = setup_logging()

DRAFT_SYSTEM_PROMPT = (
    "You draft posts for a terse, factual market-news X account. "
    "No emojis, no hashtags, cashtags for tickers ($NVDA). Three "
    "formats: BREAKING = one ALL-CAPS sentence with the key number "
    "(\"NY FED: JUNE YEAR-AHEAD EXPECTED INFLATION 3.02%\"); "
    "CONTEXT = sentence-case market observation (\"$NVDA is now green "
    "on the day while $QQQ is down 1.6%\"); SUMMARY = 2-3 short neutral "
    "paragraphs for bigger stories. Max 280 chars unless SUMMARY. "
    "Never predict price direction, never advise buy/sell, attribute "
    "unconfirmed reports with 'reportedly', never invent numbers. "
    "Return JSON: {format, text, confidence: 0-1}."
)


def _build_draft_prompt(headline: Headline, classification: dict) -> str:
    tickers = classification.get("tickers", [])
    return (
        f"Headline: {headline.title}\n"
        f"Source: {headline.source}\n"
        f"Summary: {headline.summary[:400]}\n"
        f"Impact: {classification.get('impact', 'med')}\n"
        f"Category: {classification.get('category', 'other')}\n"
        f"Angle: {classification.get('angle', '')}\n"
        f"Tickers: {', '.join(tickers) if tickers else 'none'}\n"
        "Return a single JSON object."
    )


def draft_posts(filtered: list[tuple[Headline, dict]]) -> int:
    """Generate drafts for filtered headlines. Returns count created."""
    if not filtered:
        return 0

    created = 0
    for headline, classification in filtered:
        prompt = _build_draft_prompt(headline, classification)
        raw = _call_claude(DRAFT_SYSTEM_PROMPT, prompt, DRAFT_MODEL)
        if not raw:
            continue

        parsed = _parse_json_array(raw)
        if not parsed:
            # Try single object
            try:
                text = raw.strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                draft_data = json.loads(text)
                parsed = [draft_data]
            except json.JSONDecodeError:
                logger.warning("Skipping draft for headline %s — bad JSON", headline.id)
                continue

        draft_data = parsed[0]
        text = draft_data.get("text", "").strip()
        if not text:
            continue

        tickers = classification.get("tickers", [])
        with get_session() as session:
            draft = Draft(
                headline_id=headline.id,
                text=text,
                format=draft_data.get("format", "CONTEXT"),
                impact=classification.get("impact", "med"),
                category=classification.get("category", "other"),
                tickers=",".join(tickers) if tickers else "",
                confidence=float(draft_data.get("confidence", 0.5)),
                status="pending",
                created_at=datetime.utcnow(),
            )
            session.add(draft)
            row = session.get(Headline, headline.id)
            if row:
                row.status = "drafted"
                session.add(row)
            session.commit()
            created += 1

    logger.info("Created %d drafts", created)
    return created
