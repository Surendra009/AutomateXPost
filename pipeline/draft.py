"""LLM draft step — short, human X posts."""

import json
import re
from datetime import datetime

from config import DRAFT_MODEL, MAX_DRAFTS_PER_CYCLE
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.analyze import analyze_headline
from pipeline.enrich import fetch_article_text
from pipeline.filter import _call_claude, _parse_json_array

logger = setup_logging()

MAX_CHARS = {"BREAKING": 200, "CONTEXT": 220, "SUMMARY": 320}

DRAFT_SYSTEM_PROMPT = """You write short X posts about market news. Sound like a real person typing a quick take — not a news wire, not a Bloomberg terminal, not a headline.

## Voice
- Simple everyday words. Write like you're texting a trader friend.
- Sentence case only. Never ALL CAPS (except ticker symbols like $NVDA).
- 1-2 short sentences for BREAKING and CONTEXT. 2-3 max for SUMMARY.
- One idea. One number max (the one that matters most).
- No emojis, no hashtags. Put $TICKER at the end.

## Do NOT
- Dump stats, guidance ranges, or every detail from the article
- Use wire-service phrasing: "signals", "cushion", "read-through", "intraday", "street consensus"
- Chain clauses with colons and dashes
- Sound like CNBC chyron text

## Good examples
BREAKING: "Rivian sold 75M shares at ~$20 to raise $1.5B. Stock down ~15% on dilution. $RIVN"
CONTEXT: "Fed held rates but penciled in two cuts for 2026, up from one. A bit more dovish than expected. $SPY"
SUMMARY: "Nvidia beat earnings — $22.1B revenue vs $20.4B expected, mostly data center.\n\nBig beat but everyone already expected AI demand. $NVDA"

## Bad example (never write like this)
"$RIVN SOLD 75M SHARES AT ~$20.14 TO RAISE $1.51B — DILUTION HIT HARD: STOCK DOWN ~15% INTRADAY. THE CUSHION: Q2 GUIDANCE $1.55–$1.65B BEAT..."

Return JSON: {"skip": false, "format": "BREAKING"|"CONTEXT"|"SUMMARY", "text": "...", "confidence": 0.0-1.0}
If you can't keep it short and human, return {"skip": true, "reason": "..."}"""


def _build_draft_prompt(headline: Headline, classification: dict, analysis: dict) -> str:
    tickers = analysis.get("tickers") or classification.get("tickers", [])
    fmt = analysis.get("suggested_format", "CONTEXT")
    char_limit = MAX_CHARS.get(fmt, 220)

    return (
        f"Headline (don't copy): {headline.title}\n\n"
        f"What happened: {analysis.get('hook', '')}\n"
        f"Why it matters: {analysis.get('why_it_matters') or 'n/a'}\n"
        f"Key number (use at most this one): {analysis.get('one_number') or 'pick one if needed'}\n"
        f"Tickers: {', '.join(tickers) if tickers else 'none'}\n"
        f"Format: {fmt}\n"
        f"Max length: {char_limit} characters\n\n"
        "Write one short X post. Return JSON."
    )


def draft_posts(filtered: list[tuple[Headline, dict]]) -> int:
    if not filtered:
        return 0

    created = 0
    for headline, classification in filtered:
        if created >= MAX_DRAFTS_PER_CYCLE:
            logger.info("Draft cap reached (%d/cycle)", MAX_DRAFTS_PER_CYCLE)
            break

        article_text = fetch_article_text(headline.url)
        analysis = analyze_headline(headline, classification, article_text)
        if not analysis:
            _discard_headline(headline, "analyze failed")
            continue

        if not analysis.get("publish"):
            _discard_headline(headline, analysis.get("skip_reason", "insufficient insight"))
            continue

        prompt = _build_draft_prompt(headline, classification, analysis)
        raw = _call_claude(DRAFT_SYSTEM_PROMPT, prompt, DRAFT_MODEL, max_tokens=400)
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
                continue

        draft_data = parsed[0]
        if draft_data.get("skip"):
            _discard_headline(headline, draft_data.get("reason", "drafter skip"))
            continue

        text = draft_data.get("text", "").strip()
        fmt = draft_data.get("format", analysis.get("suggested_format", "CONTEXT"))

        if not text or not _passes_style_check(text, fmt):
            logger.info("Style check failed for headline %s", headline.id)
            _discard_headline(headline, "style check failed")
            continue

        if _is_headline_echo(text, headline.title):
            _discard_headline(headline, "headline echo")
            continue

        tickers = analysis.get("tickers") or classification.get("tickers", [])
        with get_session() as session:
            draft = Draft(
                headline_id=headline.id,
                text=text,
                format=fmt,
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


def _passes_style_check(text: str, fmt: str) -> bool:
    """Reject wire-service tone, caps lock, stat dumps."""
    limit = MAX_CHARS.get(fmt, 220)
    if len(text) > limit + 60:  # small buffer for SUMMARY newlines
        return False

    # Too much ALL CAPS (excluding tickers)
    letters = [c for c in text if c.isalpha()]
    if letters:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.35:
            return False

    # Too many numbers / dollar amounts = stat dump
    dollar_count = len(re.findall(r"(?:~)?\$[\d,.]+[BMK]?", text))
    pct_count = len(re.findall(r"\d+\.?\d*%", text))
    if dollar_count > 2 or (dollar_count + pct_count) > 3:
        return False

    # Too many sentences (ignore trailing ticker cashtag)
    body = re.sub(r"\s*\$[A-Z]{1,5}(?:\s+\$[A-Z]{1,5})*\s*$", "", text).strip()
    sentences = [s for s in re.split(r"[.!?]+\s+", body) if s]
    max_sentences = 3 if fmt == "SUMMARY" else 2
    if len(sentences) > max_sentences:
        return False

    # Wire-service red flags
    jargon = re.compile(
        r"\b(intraday|street consensus|read-through|signals capital|the cushion|"
        r"year-over-year|yoy|sequentially|guidance range|underwriters hold)\b",
        re.I,
    )
    if jargon.search(text):
        return False

    return True


def _discard_headline(headline: Headline, reason: str) -> None:
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
