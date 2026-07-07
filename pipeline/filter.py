"""LLM filter step — classify headline relevance."""

import json
from typing import Any

from sqlmodel import select

from config import ANTHROPIC_API_KEY, FILTER_MODEL
from database import get_session, get_setting
from logging_config import setup_logging
from models import Headline

logger = setup_logging()

FILTER_SYSTEM_PROMPT = (
    "You classify news for a stock/AI market X account. For each item "
    "return strict JSON: {relevant: bool, tickers: [], "
    "impact: 'high'|'med'|'low', category: 'earnings'|'macro'|'ai'|"
    "'geopolitics'|'ipo'|'regulatory'|'other', angle: '<one sentence "
    "why traders care>', key_facts: ['<specific fact with number if any>', "
    "'<second fact>']}. relevant=true only if it could move a stock, "
    "sector, or index, or is major AI industry news. Opinion pieces "
    "and PR fluff are false. Extract 2-4 concrete facts (numbers, names, "
    "comparisons to estimates) from the summary — not just the headline. "
    "JSON array only."
)


def _call_claude(system: str, user: str, model: str, retry: bool = True, max_tokens: int = 4096) -> str | None:
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set, skipping filter")
        return None

    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text
    except Exception as e:
        logger.error("Claude API error: %s", e)
        if retry:
            logger.info("Retrying Claude call once...")
            return _call_claude(system, user, model, retry=False, max_tokens=max_tokens)
        return None


def _parse_json_array(text: str) -> list[dict] | None:
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError as e:
        logger.warning("Malformed LLM JSON: %s", e)
    return None


def _build_batch_prompt(headlines: list[Headline], watchlist: list[str]) -> str:
    watchlist_str = ", ".join(watchlist) if watchlist else "none"
    lines = [f"Watchlist tickers (prioritize if mentioned): {watchlist_str}", ""]
    for i, h in enumerate(headlines):
        lines.append(f"[{i}] Source: {h.source}")
        lines.append(f"Title: {h.title}")
        if h.summary:
            lines.append(f"Summary: {h.summary[:300]}")
        lines.append("")
    lines.append(f"Return a JSON array of exactly {len(headlines)} objects, one per item in order.")
    return "\n".join(lines)


def filter_headlines(headlines: list[Headline]) -> list[tuple[Headline, dict]]:
    """Filter headlines via Claude Haiku. Returns (headline, classification) pairs."""
    if not headlines:
        return []

    watchlist = get_setting("watchlist", [])
    results: list[tuple[Headline, dict]] = []

    for batch_start in range(0, len(headlines), 10):
        batch = headlines[batch_start : batch_start + 10]
        prompt = _build_batch_prompt(batch, watchlist)
        raw = _call_claude(FILTER_SYSTEM_PROMPT, prompt, FILTER_MODEL)
        if not raw:
            continue

        parsed = _parse_json_array(raw)
        if not parsed:
            logger.warning("Skipping batch due to unparseable filter response")
            continue

        for i, h in enumerate(batch):
            if i >= len(parsed):
                break
            classification: dict[str, Any] = parsed[i]
            if not classification.get("relevant"):
                with get_session() as session:
                    row = session.get(Headline, h.id)
                    if row:
                        row.status = "discarded"
                        session.add(row)
                        session.commit()
                continue
            if classification.get("impact") == "low":
                with get_session() as session:
                    row = session.get(Headline, h.id)
                    if row:
                        row.status = "discarded"
                        session.add(row)
                        session.commit()
                continue
            results.append((h, classification))
            with get_session() as session:
                row = session.get(Headline, h.id)
                if row:
                    row.status = "filtered"
                    session.add(row)
                    session.commit()

    logger.info("Filter kept %d/%d headlines", len(results), len(headlines))
    return results
