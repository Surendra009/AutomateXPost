"""Prioritize tech, stock, and AI headlines over generic wire stories."""

from __future__ import annotations

import re

from models import Headline
from pipeline.ai_news import AI_SOURCES, is_ai_source, mentions_ai

# Higher score = preferred for drafting
SOURCE_PRIORITY: dict[str, int] = {
    "Finnhub Earnings": 100,
    "Finnhub watchlist": 90,
    "Finnhub": 80,
    "SEC EDGAR 8-K": 75,
    "Seeking Alpha": 70,
    "CNBC AI": 68,
    "TechCrunch AI": 66,
    "The Verge AI": 66,
    "Google AI Blog": 65,
    "OpenAI Blog": 65,
    "VentureBeat AI": 62,
    "MIT Tech Review AI": 60,
    "MarketWatch": 55,
    "Yahoo Finance": 50,
    "WSJ Markets": 45,
    "CNBC Markets": 30,
    "Bloomberg Markets": 28,
    "Financial Times": 25,
}

# Generic market wires — cap how many enter each filter cycle
WIRE_SOURCES = {"Bloomberg Markets", "CNBC Markets", "Financial Times", "WSJ Markets"}
MAX_WIRE_HEADLINES_PER_CYCLE = 2

CATEGORY_BOOST = {
    "earnings": 0.12,
    "ai": 0.10,
    "ipo": 0.08,
    "regulatory": 0.06,
    "macro": 0.04,
}

GENERIC_HEADLINE_NOISE = re.compile(
    r"\b("
    r"market wrap|stocks (rise|fall|slip|gain|drop|sink|rally|climb)|"
    r"investors (await|watch|eye)|wall street|"
    r"here'?s what'?s (moving|happening)|what to watch|"
    r"check out|things to know|need to know|"
    r"premarket|after hours wrap|market snapshot"
    r")\b",
    re.I,
)

TICKER_SIGNAL = re.compile(r"\b[A-Z]{1,5}\b|\$[A-Z]{1,5}\b|nasdaq|s&p|dow\b", re.I)
STOCK_SIGNAL = re.compile(
    r"\b(earnings|revenue|eps|guidance|beat|miss|ipo|merger|acquire|"
    r"layoff|buyback|dividend|forecast|shares|stock|chip|semiconductor|"
    r"nvidia|nvda|apple|aapl|microsoft|msft|google|meta|amazon|tesla|"
    r"openai|anthropic|claude|gemini)\b",
    re.I,
)


def source_priority(source: str) -> int:
    if source in SOURCE_PRIORITY:
        return SOURCE_PRIORITY[source]
    if source.startswith("Finnhub"):
        return 85
    if source in AI_SOURCES:
        return 64
    return 40


def is_generic_wire_noise(headline: Headline) -> bool:
    """Vague market-wrap headlines from wire sources without a specific stock angle."""
    if headline.source not in WIRE_SOURCES:
        return False
    text = f"{headline.title} {headline.summary}"
    if GENERIC_HEADLINE_NOISE.search(headline.title):
        if not STOCK_SIGNAL.search(text) and not mentions_ai(text):
            return True
    # Very broad titles with no company/ticker hook
    if len(headline.title) < 45 and not TICKER_SIGNAL.search(headline.title):
        if not STOCK_SIGNAL.search(headline.title):
            return True
    return False


def heuristic_priority(headline: Headline) -> float:
    """Pre-LLM priority score for headline selection."""
    score = source_priority(headline.source) / 100.0
    text = f"{headline.title} {headline.summary}"

    if is_ai_source(headline) or mentions_ai(text):
        score += 0.25
    if STOCK_SIGNAL.search(text):
        score += 0.15
    if TICKER_SIGNAL.search(headline.title):
        score += 0.10
    if headline.source in WIRE_SOURCES:
        score -= 0.20
    if is_generic_wire_noise(headline):
        score -= 0.50
    return score


def composite_score(headline: Headline, classification: dict) -> float:
    """Final ranking after LLM filter — blend relevance with source/category priority."""
    base = float(classification.get("relevance_score", 0))
    category = classification.get("category", "other")
    boost = CATEGORY_BOOST.get(category, 0)
    src = source_priority(headline.source) / 200.0  # 0–0.5

    tickers = classification.get("tickers") or []
    if tickers:
        boost += 0.05
    if is_ai_source(headline) or category == "ai":
        boost += 0.08
    if headline.source in WIRE_SOURCES:
        boost -= 0.10
    if is_generic_wire_noise(headline):
        boost -= 0.30

    return base + boost + src


def select_headlines_for_filter(headlines: list[Headline], limit: int) -> list[Headline]:
    """Pick a diverse, priority-weighted subset before sending to the LLM filter."""
    ranked = sorted(headlines, key=heuristic_priority, reverse=True)

    selected: list[Headline] = []
    wire_count = 0
    source_counts: dict[str, int] = {}

    for h in ranked:
        if len(selected) >= limit:
            break
        if is_generic_wire_noise(h):
            continue

        src = h.source
        if src in WIRE_SOURCES:
            if wire_count >= MAX_WIRE_HEADLINES_PER_CYCLE:
                continue
            wire_count += 1

        # Max 3 per any single source in one cycle
        if source_counts.get(src, 0) >= 3:
            continue

        selected.append(h)
        source_counts[src] = source_counts.get(src, 0) + 1

    return selected


def select_diverse_for_drafting(
    filtered: list[tuple[Headline, dict]],
    limit: int,
) -> list[tuple[Headline, dict]]:
    """Pick top stories with diversity — don't let one wire source dominate drafts."""
    ranked = sorted(
        filtered,
        key=lambda x: composite_score(x[0], x[1]),
        reverse=True,
    )

    chosen: list[tuple[Headline, dict]] = []
    wire_count = 0
    categories_seen: set[str] = set()

    for headline, classification in ranked:
        if len(chosen) >= limit:
            break

        src = headline.source
        cat = classification.get("category", "other")

        if src in WIRE_SOURCES:
            if wire_count >= 1 and cat not in {"earnings", "macro"}:
                continue
            wire_count += 1

        # Prefer mix: don't pick 3 identical categories if alternatives exist
        if len(chosen) >= 2 and cat in categories_seen and cat == "other":
            continue

        chosen.append((headline, classification))
        categories_seen.add(cat)

    # Backfill if we were too strict
    if len(chosen) < limit:
        for item in ranked:
            if item not in chosen:
                chosen.append(item)
            if len(chosen) >= limit:
                break

    return chosen
