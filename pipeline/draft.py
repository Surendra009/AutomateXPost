"""LLM draft step — generate X post text."""

import json
from datetime import datetime

from config import ANTHROPIC_API_KEY, DRAFT_MODEL
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.filter import _call_claude, _parse_json_array

logger = setup_logging()

DRAFT_SYSTEM_PROMPT = """You write X posts for a market-news account followed by traders and portfolio managers. Posts must read like a sharp human reporter sharing a quick take — not a wire headline, not a bot summary.

## Voice
- Natural, direct prose. Contractions are fine ("it's", "didn't").
- Lead with the most newsworthy fact — usually a number, surprise, or concrete development.
- Explain why it matters in the same breath (vs expectations, for which tickers, timing).
- Never just rephrase the headline. Add detail from the summary and key_facts.

## Hard rules
- No emojis, no hashtags. Cashtags for tickers ($NVDA).
- Only use facts from the input. Attribute unconfirmed items with "reportedly".
- Never invent numbers, quotes, or price moves not in the source material.
- Never predict price direction or give buy/sell advice.

## Formats

BREAKING (max 280 chars) — urgent news just hitting. Sentence case (not ALL CAPS). Open with what happened + the key number, then a short "so what" clause.
Good: "Nvidia beat Q4 — revenue came in at $22.1B vs ~$20.4B expected, with data center sales up 93% YoY. The beat was driven almost entirely by AI chip demand. $NVDA"
Bad: "NVDA BEATS Q4 ESTIMATES ON STRONG DATA CENTER REVENUE"

CONTEXT (max 280 chars) — how the market is reacting or connecting dots between names. Include a specific detail (%, level, comparison).
Good: "Fed held rates at 4.25-4.50% but the dot plot now shows two cuts penciled in for 2026, up from one in December. $SPY little changed; rate-sensitive $XLRE and $XLU leading."
Bad: "Fed holds rates steady and signals possible cuts."

SUMMARY (max 500 chars) — bigger stories only. 2-3 short paragraphs separated by blank lines. First graf: what happened. Second: context (vs expectations, history, who else is affected). Third (optional): what to watch next.
Good: "OpenAI is reportedly in talks to raise $40B at a $300B valuation, per sources familiar with the matter.\n\nThat would roughly double its last round and cement it as the most valuable private AI company. Microsoft ($MSFT), which owns ~49% of OpenAI, and Google ($GOOGL) would face a further-capitalized rival in enterprise AI."
Bad: "OpenAI is raising money at a high valuation which is big news for AI."

Return JSON only: {"format": "BREAKING"|"CONTEXT"|"SUMMARY", "text": "...", "confidence": 0.0-1.0}"""


def _build_draft_prompt(headline: Headline, classification: dict) -> str:
    tickers = classification.get("tickers", [])
    key_facts = classification.get("key_facts", [])
    facts_block = ""
    if key_facts:
        facts_block = "Key facts to weave in:\n" + "\n".join(f"- {f}" for f in key_facts) + "\n"

    summary = headline.summary.strip() if headline.summary else "(no summary available — use headline carefully, stay conservative)"

    return (
        f"Headline: {headline.title}\n"
        f"Source: {headline.source}\n"
        f"Article summary:\n{summary[:600]}\n\n"
        f"{facts_block}"
        f"Why traders care: {classification.get('angle', '')}\n"
        f"Impact: {classification.get('impact', 'med')}\n"
        f"Category: {classification.get('category', 'other')}\n"
        f"Tickers: {', '.join(tickers) if tickers else 'none'}\n\n"
        "Write a post that adds value beyond the headline. Return a single JSON object."
    )


def draft_posts(filtered: list[tuple[Headline, dict]]) -> int:
    """Generate drafts for filtered headlines. Returns count created."""
    if not filtered:
        return 0

    created = 0
    for headline, classification in filtered:
        prompt = _build_draft_prompt(headline, classification)
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
                draft_data = json.loads(text)
                parsed = [draft_data]
            except json.JSONDecodeError:
                logger.warning("Skipping draft for headline %s — bad JSON", headline.id)
                continue

        draft_data = parsed[0]
        text = draft_data.get("text", "").strip()
        if not text:
            continue

        # Reject drafts that are basically just the headline
        if _is_headline_echo(text, headline.title):
            logger.info("Skipping headline-echo draft for %s", headline.id)
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


def _is_headline_echo(text: str, title: str) -> bool:
    """True if draft is too similar to the headline (likely low-effort)."""
    from rapidfuzz import fuzz

    normalized_text = " ".join(text.lower().split())[:120]
    normalized_title = " ".join(title.lower().split())
    return fuzz.ratio(normalized_text, normalized_title) > 75
