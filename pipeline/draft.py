"""LLM draft step — single Sonnet call (analyze + write combined)."""

import json
import re
from datetime import datetime

from config import DRAFT_MODEL, MAX_DRAFTS_PER_CYCLE
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.ai_news import infer_ai_tickers
from pipeline.dedup import was_recently_drafted
from pipeline.enrich import get_article_text_for_draft
from pipeline.filter import _call_claude, _parse_json_array
from pipeline.freshness import is_fresh

logger = setup_logging()

MAX_CHARS = {"BREAKING": 260, "CONTEXT": 280, "SUMMARY": 380}

DRAFT_SYSTEM_PROMPT = """You read tech and stock news, decide if it's worth posting on X, and write the post in one step.

Return JSON only:
{
  "skip": false,
  "skip_reason": "only if skip is true",
  "format": "BREAKING"|"CONTEXT"|"SUMMARY",
  "tickers": ["NVDA"],
  "text": "multi-line post with \\n line breaks",
  "confidence": 0.0-1.0
}

## When to skip (skip=true)
- Vague wire headlines with no specific company or data ("stocks rise", "investors await Fed")
- Rehashed news with no new information
- Can't explain what happened AND why it matters in two short lines
- Minor UI tweaks or fluff without real product/market impact

## Post layout (use \\n in text)
```
What happened (company + specific action)

Why it matters — useful takeaway for investors/builders

$TICKER
```

## Rules
- Be specific: company, product, or one key number
- Line 2 = the "so what" — competitive angle, stock impact, or user impact
- Sentence case. Never ALL CAPS except $TICKERS
- Max 2 numbers in the whole post
- No emojis, no hashtags
- Each line under ~70 characters
- Tickers on the last line (use tickers array too)
- Don't copy the headline verbatim

## Good example
```
Anthropic launched Claude on mobile and web
Cowork agents now run outside the desktop app

$GOOGL $AMZN
```"""


def _build_draft_prompt(headline: Headline, classification: dict, article_text: str) -> str:
    tickers = classification.get("tickers") or []
    ticker_str = ", ".join(tickers) if tickers else "infer from story"
    parts = [
        f"Headline (don't copy): {headline.title}",
        f"Source: {headline.source}",
        f"Category: {classification.get('category', 'other')}",
        f"Impact: {classification.get('impact', 'med')}",
        f"Suggested tickers: {ticker_str}",
    ]
    if headline.summary:
        parts.append(f"Summary: {headline.summary[:500]}")
    if classification.get("angle"):
        parts.append(f"Angle: {classification['angle']}")
    if article_text:
        parts.append(f"Article excerpt:\n{article_text[:2500]}")
    parts.append("\nDecide skip or write the post. Return JSON.")
    return "\n\n".join(parts)


def _parse_draft_response(raw: str) -> dict | None:
    parsed = _parse_json_array(raw)
    if parsed:
        return parsed[0]
    try:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _resolve_tickers(draft_data: dict, classification: dict, headline: Headline) -> list[str]:
    tickers = [t.upper() for t in draft_data.get("tickers") or classification.get("tickers") or []]
    if not tickers:
        tickers = infer_ai_tickers(f"{headline.title} {headline.summary}")
    return tickers


def _normalize_post(text: str, tickers: list[str]) -> str:
    """Clean up line breaks and ensure tickers on final line."""
    text = text.replace("\\n", "\n").strip()
    lines = [ln.strip() for ln in text.split("\n")]

    cleaned: list[str] = []
    for ln in lines:
        if not ln:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        cleaned.append(ln)

    body_lines = []
    ticker_pattern = re.compile(r"^\$[A-Z]{1,5}$")
    for ln in cleaned:
        if ticker_pattern.match(ln.replace(" ", "")) and len(ln.split()) <= 3:
            continue
        body_lines.append(ln)

    while body_lines and body_lines[-1] == "":
        body_lines.pop()

    if tickers:
        ticker_line = " ".join(f"${t.upper()}" for t in tickers)
        body_lines.append("")
        body_lines.append(ticker_line)

    return "\n".join(body_lines).strip()


def draft_posts(filtered: list[tuple[Headline, dict]]) -> int:
    if not filtered:
        return 0

    created = 0
    for headline, classification in filtered:
        if created >= MAX_DRAFTS_PER_CYCLE:
            logger.info("Draft cap reached (%d/cycle)", MAX_DRAFTS_PER_CYCLE)
            break

        if not is_fresh(headline.published_at):
            _discard_headline(headline, "story too old to draft")
            continue

        if was_recently_drafted(headline.title, headline.source):
            _discard_headline(headline, "duplicate story drafted recently")
            logger.debug("Skipping duplicate story: %s", headline.title[:80])
            continue

        article_text = get_article_text_for_draft(headline, classification)
        prompt = _build_draft_prompt(headline, classification, article_text)
        raw = _call_claude(DRAFT_SYSTEM_PROMPT, prompt, DRAFT_MODEL, max_tokens=600)
        if not raw:
            _discard_headline(headline, "draft LLM failed")
            continue

        draft_data = _parse_draft_response(raw)
        if not draft_data:
            logger.warning("Unparseable draft JSON for headline %s", headline.id)
            continue

        if draft_data.get("skip"):
            _discard_headline(headline, draft_data.get("skip_reason", "drafter skip"))
            continue

        tickers = _resolve_tickers(draft_data, classification, headline)
        fmt = draft_data.get("format", "CONTEXT")
        text = _normalize_post(draft_data.get("text", "").strip(), tickers)

        if not text or not _passes_style_check(text, fmt):
            logger.info("Style check failed for headline %s", headline.id)
            _discard_headline(headline, "style check failed")
            continue

        if _is_headline_echo(text, headline.title):
            _discard_headline(headline, "headline echo")
            continue

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

    logger.info("Created %d drafts (1 Sonnet call each)", created)
    return created


def _passes_style_check(text: str, fmt: str) -> bool:
    limit = MAX_CHARS.get(fmt, 280)
    if len(text) > limit + 40:
        return False

    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.35:
        return False

    dollar_count = len(re.findall(r"(?:~)?\$[\d,.]+[BMK]?", text))
    pct_count = len(re.findall(r"\d+\.?\d*%", text))
    if dollar_count > 2 or (dollar_count + pct_count) > 3:
        return False

    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) < 2:
        return False

    if "\n" not in text and len(text) > 100:
        return False

    for ln in lines:
        if len(ln) > 100:
            return False

    if len(lines) > 6:
        return False

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

    flat = " ".join(text.lower().split())[:120]
    normalized_title = " ".join(title.lower().split())
    return fuzz.ratio(flat, normalized_title) > 75
