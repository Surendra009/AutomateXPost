"""Analyze headlines for non-obvious trading insights before drafting."""

import json

from config import FILTER_MODEL
from logging_config import setup_logging
from models import Headline
from pipeline.filter import _call_claude, _parse_json_array

logger = setup_logging()

ANALYZE_SYSTEM_PROMPT = """You are a senior markets editor preparing briefs for an X account read by professional traders.

Your job: find what adds value BEYOND the headline. Traders already saw the headline — they need the "so what."

Extract only from the provided source material. Never invent numbers or quotes.

Return JSON:
{
  "publish": true|false,
  "skip_reason": "why not publishable if publish is false",
  "suggested_format": "BREAKING"|"CONTEXT"|"SUMMARY",
  "tickers": ["NVDA"],
  "lead_fact": "the single most important concrete fact (preferably with a number)",
  "vs_expectations": "how this compares to consensus/forecast/prior — null if unknown",
  "significance": "why this size/timing/detail matters (historical, sector, macro link)",
  "read_through": "which other tickers/sectors affected and how — null if none",
  "caveat": "uncertainty, 'reportedly', pending confirmation — null if none",
  "what_to_watch": "next data point, vote, earnings, deadline — null if none"
}

Set publish=false when:
- The source is too thin to say anything beyond restating the headline
- It's pure opinion/PR with no tradeable fact
- You cannot identify at least ONE of: vs_expectations, significance, read_through
- A busy trader would scroll past this — not worth posting
- No specific numbers, tickers, or concrete market mechanism

Set publish=true only when a professional trader would stop scrolling. Default publish=false."""


def analyze_headline(
    headline: Headline,
    classification: dict,
    article_text: str,
) -> dict | None:
    """Return analysis dict or None if unparseable."""
    parts = [
        f"Headline: {headline.title}",
        f"Source: {headline.source}",
        f"URL: {headline.url}",
    ]
    if headline.summary:
        parts.append(f"RSS summary: {headline.summary[:500]}")
    if article_text:
        parts.append(f"Article excerpt:\n{article_text[:3500]}")
    else:
        parts.append("(No article body fetched — rely on RSS summary only, be conservative)")

    parts.append(f"Filter angle: {classification.get('angle', '')}")
    key_facts = classification.get("key_facts", [])
    if key_facts:
        parts.append("Key facts from filter: " + "; ".join(key_facts))

    raw = _call_claude(ANALYZE_SYSTEM_PROMPT, "\n\n".join(parts), FILTER_MODEL, max_tokens=800)
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
