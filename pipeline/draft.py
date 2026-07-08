"""LLM draft step — single Sonnet call (analyze + write combined)."""

import json
import re
from datetime import datetime

from config import DRAFT_MODEL, MAX_DRAFTS_PER_CYCLE
from database import get_session, get_setting
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.ai_news import infer_ai_tickers
from pipeline.draft_budget import DraftBudget
from pipeline.dedup import was_recently_drafted
from pipeline.classify_cache import get_cached_classification
from pipeline.enrich import get_article_text_for_draft
from pipeline.feedback import drafter_feedback_hints
from pipeline.filter import _call_claude, _parse_json_array
from pipeline.freshness import is_fresh
from pipeline.draft_quality import is_too_generic
from pipeline.templates import try_template_draft

logger = setup_logging()

DRAFT_TEMPERATURE = 1.0
MAX_CHARS = {"BREAKING": 275, "CONTEXT": 295, "SUMMARY": 400}

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
- Can't explain what happened AND why it matters in three substantive lines
- Minor UI tweaks or fluff without real product/market impact

## Post layout (use \\n in text)
```
Hook — your words, not the headline

Key detail — one specific fact (product, deal size, guidance, timeline)

Why it matters — who wins/loses or how the stock reprices

$TICKER
```

## Originality (critical)
- REWRITE in your own words — never paraphrase the headline sentence structure
- Do NOT start line 1 with the same first 5 words as the headline
- Do NOT use wire phrasing: "came in at", "printed at", "according to", "said in a statement"
- Do NOT use filler: "watch guidance on the call", "another step in the AI product war", "forward outlook reset"
- Line 1 must add value beyond restating the headline — lead with the surprise, number, or trade angle
- If key_facts are provided, use at least one in line 1 or 2
- If angle is provided, line 1 must reflect that angle

## Tone
- Sharp market analyst voice — punchy, specific, conversational
- Confident but not hypey; explain the story, don't sell it
- Plain language; avoid jargon ("read-through", "intraday", "yoy")

## Hook rules
- Line 1: company + concrete action OR surprise number OR who loses
- Never open with "Investors", "Markets", "Traders", or "Wall Street"
- Active verbs: launched, beat, cut, acquired, filed, raised

## Rules
- Exactly 3 body lines before tickers
- Target 240–300 characters (CONTEXT/BREAKING); SUMMARY up to ~360
- Line 3 = specific "so what" — stock move, competitor, sector read-through
- Sentence case. Never ALL CAPS except $TICKERS
- Up to 3 numbers total
- No emojis
- Optional one hashtag on macro/earnings days only if allow_hashtags is true
- Cashtags on the last line only
- Each line under ~95 characters

## Examples (vary your structure — don't mimic these exactly)

Earnings beat:
```
NVDA cleared Q4 EPS by 8% — data center drove the beat
Revenue $22.1B vs $20.4B est; guide topped the street
Hyperscaler capex still funneling into GPUs — margin story intact

$NVDA
```

AI launch:
```
OpenAI ships a faster GPT tier with a bigger context window
Latency and price matter more than benchmark bragging
MSFT/Azure and enterprise seats are the monetization path

$MSFT
```

M&A:
```
CRM is buying the data layer it was missing for agents
Deal fills a gap vs MSFT/GOOGL suites — integration risk is the trade

$CRM
```"""


def _build_draft_prompt(
    headline: Headline,
    classification: dict,
    article_text: str,
    *,
    rewrite_note: str = "",
) -> str:
    tickers = classification.get("tickers") or []
    ticker_str = ", ".join(tickers) if tickers else "infer from story"
    hints = drafter_feedback_hints()
    parts = [
        f"Headline (do NOT copy wording or structure): {headline.title}",
        f"Source: {headline.source}",
        f"Category: {classification.get('category', 'other')}",
        f"Impact: {classification.get('impact', 'med')}",
        f"Suggested tickers: {ticker_str}",
        f"allow_hashtags: {get_setting('allow_hashtags', False)}",
    ]
    if rewrite_note:
        parts.append(rewrite_note)
    if hints:
        parts.append(f"User rejection feedback (avoid these mistakes):\n{hints}")
    if headline.summary:
        parts.append(f"Summary (facts only — don't copy phrasing): {headline.summary[:500]}")
    key_facts = classification.get("key_facts") or []
    if key_facts:
        facts = "\n".join(f"- {f}" for f in key_facts[:5])
        parts.append(f"Key facts (use at least one):\n{facts}")
    if classification.get("angle"):
        parts.append(f"Trader angle (line 1 must reflect this): {classification['angle']}")
    if article_text:
        parts.append(f"Article excerpt (extract facts, rewrite in your voice):\n{article_text[:2500]}")
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


def _is_cashtag_line(ln: str) -> bool:
    tokens = ln.strip().split()
    return bool(tokens) and all(re.fullmatch(r"\$[A-Z]{1,5}", t) for t in tokens)


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
    for ln in cleaned:
        if _is_cashtag_line(ln):
            continue
        body_lines.append(ln)

    while body_lines and body_lines[-1] == "":
        body_lines.pop()

    if tickers:
        ticker_line = " ".join(f"${t.upper()}" for t in tickers)
        if not body_lines or body_lines[-1] != ticker_line:
            body_lines.append("")
            body_lines.append(ticker_line)

    return "\n".join(body_lines).strip()


def _commit_draft(
    headline: Headline,
    classification: dict,
    *,
    text: str,
    fmt: str,
    tickers: list[str],
    confidence: float,
    impact: str | None = None,
    category: str | None = None,
) -> bool:
    if not text or not _passes_style_check(text, fmt):
        return False

    with get_session() as session:
        draft = Draft(
            headline_id=headline.id,
            text=text,
            format=fmt,
            impact=impact or classification.get("impact", "med"),
            category=category or classification.get("category", "other"),
            tickers=",".join(tickers) if tickers else "",
            confidence=confidence,
            status="pending",
            created_at=datetime.utcnow(),
        )
        session.add(draft)
        row = session.get(Headline, headline.id)
        if row:
            row.status = "drafted"
            session.add(row)
        session.commit()
    return True


def _llm_draft_headline(
    headline: Headline,
    classification: dict,
    *,
    rewrite_note: str = "",
) -> tuple[str, str, list[str], float] | None:
    """Run LLM drafter; returns (text, format, tickers, confidence) or None."""
    article_text = get_article_text_for_draft(headline, classification)
    prompt = _build_draft_prompt(
        headline, classification, article_text, rewrite_note=rewrite_note
    )
    raw = _call_claude(
        DRAFT_SYSTEM_PROMPT,
        prompt,
        DRAFT_MODEL,
        max_tokens=750,
        temperature=DRAFT_TEMPERATURE,
    )
    if not raw:
        return None

    draft_data = _parse_draft_response(raw)
    if not draft_data or draft_data.get("skip"):
        return None

    tickers = _resolve_tickers(draft_data, classification, headline)
    fmt = draft_data.get("format", "CONTEXT")
    text = _normalize_post(draft_data.get("text", "").strip(), tickers)
    if not text or not _passes_style_check(text, fmt):
        return None
    if is_too_generic(text, headline.title):
        return None

    return text, fmt, tickers, float(draft_data.get("confidence", 0.5))


def draft_posts(
    filtered: list[tuple[Headline, dict]],
    budget: DraftBudget | None = None,
) -> int:
    if not filtered:
        return 0

    cap = budget.remaining if budget else MAX_DRAFTS_PER_CYCLE
    created = 0
    template_count = 0
    for headline, classification in filtered:
        if budget is not None:
            if budget.remaining <= 0:
                logger.info("Draft cap reached (%d/cycle)", budget.limit)
                break
        elif created >= cap:
            logger.info("Draft cap reached (%d/cycle)", MAX_DRAFTS_PER_CYCLE)
            break

        if not is_fresh(headline.published_at):
            _discard_headline(headline, "story too old to draft")
            continue

        if was_recently_drafted(headline.title, headline.source):
            _discard_headline(headline, "duplicate story drafted recently")
            logger.debug("Skipping duplicate story: %s", headline.title[:80])
            continue

        template = try_template_draft(headline, classification)
        if template:
            tickers = template.tickers
            text = _normalize_post(template.text, tickers)
            if _commit_draft(
                headline,
                classification,
                text=text,
                fmt=template.format,
                tickers=tickers,
                confidence=template.confidence,
                impact=template.impact,
                category=template.category,
            ):
                if budget:
                    budget.try_take(1)
                created += 1
                template_count += 1
                logger.debug("Template draft (%s): %s", template.category, headline.title[:60])
                continue
            logger.info("Template draft skipped for headline %s — trying LLM", headline.id)

        result = _llm_draft_headline(headline, classification)
        if not result:
            result = _llm_draft_headline(
                headline,
                classification,
                rewrite_note=(
                    "REWRITE REQUIRED: prior attempt was too generic or copied the headline. "
                    "Use completely different wording. Lead with the trade surprise or number."
                ),
            )

        if not result:
            _discard_headline(headline, "draft quality check failed")
            continue

        text, fmt, tickers, confidence = result
        if _commit_draft(
            headline,
            classification,
            text=text,
            fmt=fmt,
            tickers=tickers,
            confidence=confidence,
        ):
            if budget:
                budget.try_take(1)
            created += 1

    llm_count = created - template_count
    logger.info(
        "Created %d drafts (%d template, %d LLM)",
        created,
        template_count,
        llm_count,
    )
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
    if dollar_count > 3 or (dollar_count + pct_count) > 4:
        return False

    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) < 3:
        return False

    if "\n" not in text and len(text) > 120:
        return False

    for ln in lines:
        if len(ln) > 120:
            return False

    if len(lines) > 7:
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


def regenerate_draft(draft_id: int) -> Draft | None:
    """Rewrite a pending draft with a fresh LLM pass (same headline)."""
    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if not draft or draft.status not in ("pending", "scheduled"):
            return None
        headline = session.get(Headline, draft.headline_id)
        if not headline:
            return None

    classification = get_cached_classification(headline.title, headline.source) or {
        "category": draft.category,
        "impact": draft.impact,
        "tickers": draft.tickers.split(",") if draft.tickers else [],
        "relevant": True,
    }

    result = _llm_draft_headline(headline, classification)
    if not result:
        result = _llm_draft_headline(
            headline,
            classification,
            rewrite_note="Rewrite with sharper hook and original phrasing — no headline copy.",
        )
    if not result:
        return None

    text, fmt, tickers, confidence = result
    with get_session() as session:
        row = session.get(Draft, draft_id)
        if not row:
            return None
        row.text = text
        row.format = fmt
        row.tickers = ",".join(tickers) if tickers else row.tickers
        row.confidence = confidence
        row.post_error = None
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
