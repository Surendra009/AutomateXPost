"""LLM draft step — single Sonnet call (analyze + write combined)."""

import json
import re
from datetime import datetime

from config import DRAFT_ARTICLE_CHARS, DRAFT_MAX_TOKENS, DRAFT_MODEL, DRAFT_PROVIDER, MAX_DRAFTS_PER_CYCLE
from database import get_session, get_setting
from logging_config import setup_logging
from models import Draft, Headline
from pipeline.ai_news import infer_ai_tickers
from pipeline.draft_budget import DraftBudget
from pipeline.dedup import was_recently_drafted
from pipeline.earnings_dedup import (
    earnings_ticker_blocked,
    extract_ticker_from_text,
    headline_looks_like_earnings,
    primary_ticker,
)
from pipeline.classify_cache import get_cached_classification
from pipeline.enrich import get_article_text_for_draft
from pipeline.feedback import drafter_feedback_hints
from pipeline.filter import _parse_json_array
from pipeline.freshness import is_fresh
from pipeline.llm_providers import call_llm
from pipeline.templates import try_template_draft

logger = setup_logging()

MAX_CHARS = {"BREAKING": 295, "CONTEXT": 320, "SUMMARY": 420}

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
- Vague wire headlines with no specific company or data
- Rehashed news with no new information
- Can't explain what happened AND why it matters with real specifics

## Post layout (use \\n in text) — aim for 4 substantive lines before tickers
```
Hook — company + concrete surprise (number or action)

Key detail — segment, product, deal size, or guidance change with numbers

Second detail — margin, demand, YoY %, management quote, or competitive angle

Why it matters — stock impact, who wins/loses, what to watch next

$TICKER
```

For earnings: include EPS/revenue vs consensus, surprise %, AND one segment/guidance highlight from the article.

## Tone
- Informative and conversational — sharp market analyst, not a wire headline
- Use plain language; explain acronyms once if needed

## Rules
- Use ALL important numbers from the article (up to 5 figures total)
- Pull segment breakdown, guidance, margins, or demand when present — do not stop at headline EPS only
- Target 260–320 characters (BREAKING/CONTEXT); SUMMARY up to ~400
- Line 1 must stop the scroll — company + concrete action or surprise number
- Never open with "Investors", "Markets", "Traders", or "Wall Street"
- Sentence case. No emojis. Cashtags on the last line only
- Don't copy the headline verbatim

## Good earnings example
```
PENG beat Q3 EPS $0.84 — 53% above consensus
Revenue $479M vs $414M est; infrastructure segment drove the beat
Management raised full-year guide on strong AI server demand
Big EPS beat — stock likely gaps up unless guide disappoints on the call

$PENG
```"""


def _build_draft_prompt(headline: Headline, classification: dict, article_text: str) -> str:
    tickers = classification.get("tickers") or []
    ticker_str = ", ".join(tickers) if tickers else "infer from story"
    hints = drafter_feedback_hints()
    parts = [
        f"Headline (don't copy): {headline.title}",
        f"Source: {headline.source}",
        f"Category: {classification.get('category', 'other')}",
        f"Impact: {classification.get('impact', 'med')}",
        f"Suggested tickers: {ticker_str}",
        f"allow_hashtags: {get_setting('allow_hashtags', False)}",
    ]
    if hints:
        parts.append(f"User rejection feedback (avoid these mistakes):\n{hints}")
    if headline.summary:
        parts.append(f"Summary: {headline.summary[:500]}")
    if classification.get("angle"):
        parts.append(f"Angle: {classification['angle']}")
    if article_text:
        parts.append(f"Article excerpt (use specific details from here):\n{article_text[:DRAFT_ARTICLE_CHARS]}")
    parts.append("\nDecide skip or write the post. Return JSON.")
    return "\n\n".join(parts)


def _build_regenerate_prompt(
    headline: Headline,
    classification: dict,
    article_text: str,
    existing_text: str,
    *,
    attempt: int = 0,
) -> str:
    base = _build_draft_prompt(headline, classification, article_text)
    extra = [
        "The user tapped REWRITE on an existing draft. You MUST return skip=false.",
        "Produce a fresh multi-line post (3–4 substantive lines, then $TICKER on its own line).",
        f"Current draft to improve (do not copy verbatim):\n{existing_text}",
    ]
    if attempt > 0:
        extra.append(
            "Previous rewrite failed validation. Use clear line breaks, at least 3 content lines, "
            "and put cashtags only on the final line."
        )
    return base + "\n\n" + "\n\n".join(extra)


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

    cat = category or classification.get("category", "other")
    ticker_str = ",".join(tickers) if tickers else ""
    symbol = primary_ticker(ticker_str) or extract_ticker_from_text(ticker_str or headline.title)
    if symbol and (
        cat == "earnings"
        or (headline_looks_like_earnings(headline.title, headline.summary) and cat != "macro")
    ):
        if earnings_ticker_blocked(symbol):
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
            else:
                logger.info("Template style check failed for headline %s", headline.id)
                _discard_headline(headline, "template style check failed")
            continue

        article_text = get_article_text_for_draft(headline, classification)
        prompt = _build_draft_prompt(headline, classification, article_text)
        raw = call_llm(
            DRAFT_SYSTEM_PROMPT,
            prompt,
            model=DRAFT_MODEL,
            provider=DRAFT_PROVIDER,
            max_tokens=DRAFT_MAX_TOKENS,
        )
        if not raw:
            _discard_headline(headline, "draft LLM failed")
            continue

        draft_data = _parse_draft_response(raw)
        if not draft_data:
            logger.warning("Unparseable draft JSON for headline %s", headline.id)
            _discard_headline(headline, "unparseable draft JSON")
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

        if _commit_draft(
            headline,
            classification,
            text=text,
            fmt=fmt,
            tickers=tickers,
            confidence=float(draft_data.get("confidence", 0.5)),
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


def _passes_style_check(text: str, fmt: str, *, relaxed: bool = False) -> bool:
    limit = MAX_CHARS.get(fmt, 320)
    if len(text) > limit + 60:
        return False

    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.4:
        return False

    dollar_count = len(re.findall(r"(?:~)?\$[\d,.]+[BMK]?", text))
    pct_count = len(re.findall(r"\d+\.?\d*%", text))
    if dollar_count > 5 or (dollar_count + pct_count) > 6:
        return False

    lines = [ln for ln in text.split("\n") if ln.strip()]
    min_lines = 2 if relaxed else 3
    if len(lines) < min_lines:
        return False

    if "\n" not in text and len(text) > 140:
        return False

    for ln in lines:
        if len(ln) > 140:
            return False

    if len(lines) > 9:
        return False

    jargon = re.compile(
        r"\b(intraday|read-through|signals capital|the cushion|sequentially)\b",
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


def regenerate_draft(draft_id: int) -> tuple[Draft | None, str | None]:
    """Rewrite a pending draft with a fresh LLM pass (same headline)."""
    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if not draft or draft.status not in ("pending", "scheduled"):
            return None, "Draft not found or not editable"
        headline = session.get(Headline, draft.headline_id)
        if not headline:
            return None, "Headline missing for this draft"
        existing_text = draft.text or ""
        draft_format = draft.format

    classification = get_cached_classification(headline.title, headline.source) or {
        "category": draft.category,
        "impact": draft.impact,
        "tickers": draft.tickers.split(",") if draft.tickers else [],
        "relevant": True,
    }
    article_text = get_article_text_for_draft(headline, classification)
    last_reason = "LLM request failed"

    for attempt in range(3):
        prompt = _build_regenerate_prompt(
            headline,
            classification,
            article_text,
            existing_text,
            attempt=attempt,
        )
        raw = call_llm(
            DRAFT_SYSTEM_PROMPT,
            prompt,
            model=DRAFT_MODEL,
            provider=DRAFT_PROVIDER,
            max_tokens=DRAFT_MAX_TOKENS,
            retry=attempt == 0,
        )
        if not raw:
            last_reason = "LLM request failed — check DeepSeek key and Railway logs"
            continue

        draft_data = _parse_draft_response(raw)
        if not draft_data:
            last_reason = "Could not parse LLM response"
            continue

        if draft_data.get("skip"):
            last_reason = draft_data.get("skip_reason") or "Model declined to rewrite"
            continue

        tickers = _resolve_tickers(draft_data, classification, headline)
        fmt = draft_data.get("format", draft_format)
        text = _normalize_post(draft_data.get("text", "").strip(), tickers)
        relaxed = attempt > 0
        if not text:
            last_reason = "Model returned empty text"
            continue
        if not _passes_style_check(text, fmt, relaxed=relaxed):
            last_reason = "Rewrite did not pass style checks — try again"
            continue

        with get_session() as session:
            row = session.get(Draft, draft_id)
            if not row:
                return None, "Draft disappeared during rewrite"
            row.text = text
            row.format = fmt
            row.tickers = ",".join(tickers) if tickers else row.tickers
            row.confidence = float(draft_data.get("confidence", row.confidence))
            row.post_error = None
            session.add(row)
            session.commit()
            session.refresh(row)
            return row, None

    return None, last_reason
