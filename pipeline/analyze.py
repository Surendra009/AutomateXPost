"""Analyze headlines — distill to one simple hook for X."""

import json

from config import FILTER_MODEL
from logging_config import setup_logging
from models import Headline
from pipeline.filter import _call_claude, _parse_json_array

logger = setup_logging()

ANALYZE_SYSTEM_PROMPT = """You prepare ultra-short briefs for someone posting market news on X (Twitter).

Pick ONE thing worth saying. Simple words. No jargon dump.

Return JSON:
{
  "publish": true|false,
  "skip_reason": "if false",
  "suggested_format": "BREAKING"|"CONTEXT"|"SUMMARY",
  "tickers": ["RIVN"],
  "hook": "What happened — one plain sentence, max 15 words",
  "why_it_matters": "Why traders care — one plain sentence, max 15 words. null if obvious from hook",
  "one_number": "the single most important number to include, or null"
}

Rules:
- hook = the news in everyday language ("Rivian sold shares to raise cash")
- why_it_matters = the reaction or surprise ("stock fell on dilution fears")
- Do NOT pack in extra stats, guidance ranges, or secondary facts
- publish=false if you can't explain it simply in those two sentences
- Default publish=false"""


def analyze_headline(
    headline: Headline,
    classification: dict,
    article_text: str,
) -> dict | None:
    parts = [
        f"Headline: {headline.title}",
        f"Source: {headline.source}",
    ]
    if headline.summary:
        parts.append(f"Summary: {headline.summary[:500]}")
    if article_text:
        parts.append(f"Article:\n{article_text[:2500]}")

    raw = _call_claude(ANALYZE_SYSTEM_PROMPT, "\n\n".join(parts), FILTER_MODEL, max_tokens=500)
    if not raw:
        return None

    parsed = _parse_json_array(raw)
    if not parsed:
        try:
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            parsed = [json.loads(text)]
        except json.JSONDecodeError:
            logger.warning("Bad analyze JSON for headline %s", headline.id)
            return None

    return parsed[0]
