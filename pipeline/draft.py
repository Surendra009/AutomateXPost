"""LLM draft step — generate X post text from editor analysis brief."""

import json
from datetime import datetime

from config import DRAFT_MODEL, MAX_DRAFTS_PER_CYCLE
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.analyze import analyze_headline
from pipeline.enrich import fetch_article_text
from pipeline.filter import _call_claude, _parse_json_array

logger = setup_logging()

DRAFT_SYSTEM_PROMPT = """You turn editor insight briefs into X posts for traders. The brief contains the VALUE — your job is to write it clearly, not to repeat the headline.

## What makes a good post
Must deliver at least ONE of these (stated plainly):
1. vs expectations (beat/miss, above/below forecast)
2. Significance (how big, how rare, what record)
3. Read-through (other tickers/sectors that move because of this)
4. What to watch next (vote, print, deadline, guidance)

## Voice
- Human, direct, confident. Like a Bloomberg-adjacent reporter with 280 chars discipline.
- No emojis, no hashtags. $TICKER cashtags at end or woven in.
- Short sentences. One idea per sentence.
- Use "reportedly" when the brief says so.

## Hard rules
- ONLY use facts from the brief. Never invent.
- Do NOT open by restating the headline verbatim.
- If the brief is thin, return {"skip": true, "reason": "..."}
- Never predict price direction or advise buy/sell.

## Formats
BREAKING (max 280): Fresh news. Lead with the fact + number, then significance or vs expectations in the same breath.
CONTEXT (max 280): Connecting dots — read-through, sector move, reaction to prior news. Must name specific tickers.
SUMMARY (max 500): Major stories. 2 short paragraphs separated by blank line. Graf 1: what happened. Graf 2: why it matters + read-through.

Return JSON: {"skip": false, "format": "...", "text": "...", "confidence": 0.0-1.0, "value_score": 0.0-1.0}
value_score = how much non-headline insight the post delivers (0=worthless, 1=must-read)."""


def _build_draft_prompt(headline: Headline, classification: dict, analysis: dict) -> str:
    brief = (
        f"Headline (DO NOT just repeat this): {headline.title}\n\n"
        f"EDITOR BRIEF:\n"
        f"- Lead fact: {analysis.get('lead_fact', '')}\n"
        f"- Vs expectations: {analysis.get('vs_expectations') or 'n/a'}\n"
        f"- Significance: {analysis.get('significance') or 'n/a'}\n"
        f"- Read-through: {analysis.get('read_through') or 'n/a'}\n"
        f"- Caveat: {analysis.get('caveat') or 'n/a'}\n"
        f"- What to watch: {analysis.get('what_to_watch') or 'n/a'}\n"
        f"- Suggested format: {analysis.get('suggested_format', 'CONTEXT')}\n"
        f"- Tickers: {', '.join(analysis.get('tickers') or classification.get('tickers') or [])}\n"
        f"- Category: {classification.get('category', 'other')}\n"
    )
    return brief + "\nWrite the post. Return JSON."


def draft_posts(filtered: list[tuple[Headline, dict]]) -> int:
    """Generate drafts for filtered headlines. Returns count created."""
    if not filtered:
        return 0

    created = 0
    for headline, classification in filtered:
        if created >= MAX_DRAFTS_PER_CYCLE:
            logger.info("Draft cap reached (%d/cycle)", MAX_DRAFTS_PER_CYCLE)
            break

        article_text = fetch_article_text(headline.url)
        if article_text:
            logger.info("Fetched %d chars for headline %s", len(article_text), headline.id)

        analysis = analyze_headline(headline, classification, article_text)
        if not analysis:
            _discard_headline(headline, "analyze failed")
            continue

        if not analysis.get("publish"):
            reason = analysis.get("skip_reason", "insufficient insight")
            logger.info("Skipping headline %s: %s", headline.id, reason)
            _discard_headline(headline, reason)
            continue

        prompt = _build_draft_prompt(headline, classification, analysis)
        raw = _call_claude(DRAFT_SYSTEM_PROMPT, prompt, DRAFT_MODEL, max_tokens=1024)
        if not raw:
            continue

        parsed = _parse_json_array(raw)
        if not parsed:
            try:
                text = raw.strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                parsed = [json.loads(text)]
            except json.JSONDecodeError:
                logger.warning("Skipping draft for headline %s — bad JSON", headline.id)
                continue

        draft_data = parsed[0]
        if draft_data.get("skip"):
            logger.info("Drafter skipped headline %s: %s", headline.id, draft_data.get("reason"))
            _discard_headline(headline, draft_data.get("reason", "drafter skip"))
            continue

        text = draft_data.get("text", "").strip()
        if not text:
            continue

        value_score = float(draft_data.get("value_score", 0.5))
        if value_score < 0.55:
            logger.info("Low value_score (%.2f) for headline %s", value_score, headline.id)
            _discard_headline(headline, "low value score")
            continue

        if _is_headline_echo(text, headline.title):
            logger.info("Skipping headline-echo draft for %s", headline.id)
            _discard_headline(headline, "headline echo")
            continue

        tickers = analysis.get("tickers") or classification.get("tickers", [])
        with get_session() as session:
            draft = Draft(
                headline_id=headline.id,
                text=text,
                format=draft_data.get("format", analysis.get("suggested_format", "CONTEXT")),
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


def _discard_headline(headline: Headline, reason: str) -> None:
    logger.debug("Discarding headline %s: %s", headline.id, reason)
    with get_session() as session:
        row = session.get(Headline, headline.id)
        if row:
            row.status = "discarded"
            session.add(row)
            session.commit()


def _is_headline_echo(text: str, title: str) -> bool:
    from rapidfuzz import fuzz

    normalized_text = " ".join(text.lower().split())[:120]
    normalized_title = " ".join(title.lower().split())
    return fuzz.ratio(normalized_text, normalized_title) > 75
