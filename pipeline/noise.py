"""Heuristic pre-filter to drop obvious noise before LLM calls."""

import re

from models import Headline
from pipeline.ai_news import AI_SOURCES, is_ai_source, mentions_ai

# Title patterns that are almost never stock-moving
NOISE_PATTERNS = re.compile(
    r"\b("
    r"how to|review|best \d|\d+ best|top \d|podcast|newsletter|opinion:|"
    r"why you should|what to know|explainer|hands[- ]on|"
    r"deal of the day|gift guide|week in review|"
    r"announces partnership with(?!.*\$\d)|"
    r"raises awareness|wins award|appoints.*chief marketing|"
    r"celebrates|spotlight|interview with(?!.*ceo|.*cfo|.*fed)"
    r")\b",
    re.I,
)

# Must hit at least one trade signal for med-priority sources
TRADE_SIGNALS = re.compile(
    r"\b("
    r"earnings|revenue|profit|eps|guidance|beat|miss|"
    r"fed|fomc|cpi|ppi|nfp|jobs report|gdp|inflation|rate cut|rate hike|"
    r"tariff|sanction|sec filing|8-k|10-k|merger|acquire|acquisition|"
    r"ipo|offering|buyback|dividend|layoff|forecast|"
    r"\$\d|billion|million|percent|%|"
    r"nvidia|nvda|apple|aapl|microsoft|msft|google|alphabet|"
    r"amazon|amzn|meta|tesla|tsla|openai|anthropic"
    r")\b",
    re.I,
)

# Sources that need a trade signal in title or summary (noisy feeds removed from config)
SOFT_SOURCES = {"Finnhub", "TechCrunch", "The Verge AI"}


def is_obvious_noise(headline: Headline) -> str | None:
    """Return discard reason if headline is obvious noise, else None."""
    text = f"{headline.title} {headline.summary}".strip()
    if not text:
        return "empty"

    if NOISE_PATTERNS.search(headline.title):
        return "noise pattern in title"

    if is_ai_source(headline) and mentions_ai(text):
        return None

    if len(headline.title) < 20 and headline.source not in ("SEC EDGAR 8-K",):
        return "title too short/vague"

    if headline.source.startswith("Finnhub") and not TRADE_SIGNALS.search(text):
        return "no trade signal"

    return None
