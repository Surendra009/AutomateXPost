"""Analyze headlines — distill to one simple hook for X."""

import json

from config import FILTER_MODEL
from logging_config import setup_logging
from models import Headline
from pipeline.ai_news import infer_ai_tickers, is_ai_source
from pipeline.filter import _call_claude, _parse_json_array

logger = setup_logging()

ANALYZE_SYSTEM_PROMPT = """You prepare ultra-short briefs for someone posting market and AI news on X (Twitter).

Pick ONE thing worth saying. Simple words. No jargon dump.

Return JSON:
{
  "publish": true|false,
  "skip_reason": "if false",
  "suggested_format": "BREAKING"|"CONTEXT"|"SUMMARY",
  "tickers": ["GOOGL"],
  "hook": "What happened — one plain sentence, max 15 words",
  "why_it_matters": "Why people care — one plain sentence, max 15 words. null if obvious from hook",
  "one_number": "the single most important number to include, or null"
}

Rules:
- hook = the news in everyday language ("Google shipped Gemini 2.5 with better reasoning")
- why_it_matters = the reaction or surprise ("puts pressure on OpenAI's API pricing")
- For AI product news: focus on what launched or changed, not stock price unless obvious
- Do NOT pack in extra stats, guidance ranges, or secondary facts
- publish=false if you can't explain it simply in those two sentences
- Default publish=false"""

ANALYZE_AI_PROMPT = """You prepare ultra-short briefs for AI product news on X.

Focus on: what launched, what's new, who it's from (OpenAI, Anthropic, Google, Meta, etc.)

Return JSON:
{
  "publish": true|false,
  "skip_reason": "if false",
  "suggested_format": "BREAKING"|"CONTEXT",
  "tickers": ["MSFT"],
  "hook": "What shipped — plain language, max 15 words",
  "why_it_matters": "Why builders or investors care — max 15 words",
  "one_number": "version number or price if relevant, else null"
}

publish=true for real model releases, new capabilities, API/agent features.
publish=false for vague teasers, rehashed news, or minor UI tweaks."""


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

    system = ANALYZE_AI_PROMPT if is_ai_source(headline) or classification.get("category") == "ai" else ANALYZE_SYSTEM_PROMPT
    raw = _call_claude(system, "\n\n".join(parts), FILTER_MODEL, max_tokens=500)
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

    result = parsed[0]
    if not result.get("tickers"):
        inferred = infer_ai_tickers(f"{headline.title} {headline.summary}")
        if inferred:
            result["tickers"] = inferred
    return result
