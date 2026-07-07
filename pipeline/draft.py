"""LLM draft step — formatted multi-line X posts."""

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
from pipeline.freshness import is_fresh

logger = setup_logging()

MAX_CHARS = {"BREAKING": 260, "CONTEXT": 280, "SUMMARY": 380}

DRAFT_SYSTEM_PROMPT = """You write formatted X posts about market news. Use line breaks so it's easy to scan on a phone — not one dense sentence, not a wire headline.

## Layout (use \\n for line breaks)

BREAKING & CONTEXT — 3-4 lines:
```
What happened (short line)

Why it matters or market reaction (short line)

$TICKER
```

SUMMARY — 4-5 lines:
```
What happened (short line)

One line of context — why traders care

$TICKER
```

## Rules
- Sentence case. Never ALL CAPS (except $TICKERS).
- Simple words. No jargon dumps.
- Max 2 numbers total across the whole post.
- No emojis, no hashtags.
- Each line should be short (under ~70 characters).
- Blank line before tickers is optional but looks good.
- Tickers always on the last line.

## Good example
```
Rivian sold 75M shares at ~$20
Raising $1.5B — stock down ~15% on dilution

$RIVN
```

## Bad (never)
- One long run-on sentence
- ALL CAPS wire text
- Packing in guidance ranges, multiple stats, "street consensus"

Return JSON: {"skip": false, "format": "BREAKING"|"CONTEXT"|"SUMMARY", "text": "...", "confidence": 0.0-1.0}
Use \\n in the text string for line breaks."""


def _build_draft_prompt(headline: Headline, classification: dict, analysis: dict) -> str:
    tickers = analysis.get("tickers") or classification.get("tickers", [])
    fmt = analysis.get("suggested_format", "CONTEXT")
    char_limit = MAX_CHARS.get(fmt, 280)
    ticker_str = " ".join(f"${t}" for t in tickers) if tickers else ""

    return (
        f"Headline (don't copy): {headline.title}\n\n"
        f"Line 1 idea (what happened): {analysis.get('hook', '')}\n"
        f"Line 2 idea (why it matters): {analysis.get('why_it_matters') or 'n/a'}\n"
        f"Key number (max one): {analysis.get('one_number') or 'optional'}\n"
        f"Tickers for last line: {ticker_str or 'none'}\n"
        f"Format: {fmt}\n"
        f"Max {char_limit} chars total\n\n"
        "Write a formatted multi-line post. Return JSON."
    )


def _normalize_post(text: str, tickers: list[str]) -> str:
    """Clean up line breaks and ensure tickers on final line."""
    text = text.replace("\\n", "\n").strip()
    lines = [ln.strip() for ln in text.split("\n")]

    # Remove empty lines except keep single blanks between blocks
    cleaned: list[str] = []
    for ln in lines:
        if not ln:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        cleaned.append(ln)

    # Strip ticker lines from body — we'll re-add at end
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

        article_text = fetch_article_text(headline.url)
        analysis = analyze_headline(headline, classification, article_text)
        if not analysis:
            _discard_headline(headline, "analyze failed")
            continue

        if not analysis.get("publish"):
            _discard_headline(headline, analysis.get("skip_reason", "insufficient insight"))
            continue

        tickers = analysis.get("tickers") or classification.get("tickers", [])
        prompt = _build_draft_prompt(headline, classification, analysis)
        raw = _call_claude(DRAFT_SYSTEM_PROMPT, prompt, DRAFT_MODEL, max_tokens=500)
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

        fmt = draft_data.get("format", analysis.get("suggested_format", "CONTEXT"))
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

    logger.info("Created %d drafts", created)
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

    # Long single-line posts without formatting
    if "\n" not in text and len(text) > 100:
        return False

    # No line should be a paragraph
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
