"""AI company news detection and ticker mapping."""

import re

from models import Headline

# Dedicated AI RSS sources — use AI signals instead of trade signals in pre-filter
AI_SOURCES = {
    "CNBC AI",
    "TechCrunch AI",
    "The Verge AI",
    "Google AI Blog",
    "OpenAI Blog",
    "VentureBeat AI",
    "MIT Tech Review AI",
}

# Major AI labs / products — used for pre-filter and ticker inference
AI_NEWS_SIGNALS = re.compile(
    r"\b("
    r"openai|chatgpt|gpt-[\d\.o]|codex|sora|"
    r"anthropic|claude|"
    r"google|gemini|deepmind|alphabet|"
    r"meta|llama|facebook|"
    r"microsoft|copilot|azure ai|"
    r"nvidia|nim\b|"
    r"amazon|bedrock|"
    r"apple|intelligence|"
    r"mistral|xai|grok|deepseek|perplexity|"
    r"new model|model release|launches|unveils|announces|rolls out|"
    r"api|agents?|multimodal|reasoning|benchmark|"
    r"artificial intelligence|\bai\b"
    r")\b",
    re.I,
)

# Material AI product events (not explainers) — verbs alone are NOT enough
AI_PRODUCT_SIGNALS = re.compile(
    r"\b("
    r"launch(es|ed|ing)?|release[sd]?|unveil(s|ed|ing)?|roll(s|ed|ing)? out|"
    r"introduc(es|ed|ing)?|debut(s|ed|ing)?|expand(s|ed|ing)?|"
    r"now available|general availability|ga\b|preview|beta|"
    r"new (model|version|capabilit|feature|tool|agent)|"
    r"gpt-[\d\.o]+|claude [\d\.]|gemini [\d\.]|llama [\d\.]"
    r")\b",
    re.I,
)

AI_LAB_NAMES = re.compile(
    r"\b("
    r"openai|chatgpt|anthropic|claude|google|gemini|deepmind|meta|llama|"
    r"microsoft|copilot|nvidia|amazon|bedrock|apple intelligence|mistral|xai|grok|deepseek"
    r")\b",
    re.I,
)

# Military/geopolitics — "launches" here must not trigger AI templates
GEOPOLITICS_CONFLICT = re.compile(
    r"\b("
    r"iran|israel|ukraine|russia|strike|missile|war|tanker|oil|crude|hormuz|"
    r"military|pentagon|troops|navy|drone|sanctions|conflict|attacks"
    r")\b",
    re.I,
)

COMPANY_TICKER_MAP: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\bopenai|chatgpt|gpt-[\d\.o]|codex|sora\b", re.I), ["MSFT"]),
    (re.compile(r"\banthropic|claude\b", re.I), ["AMZN", "GOOGL"]),
    (re.compile(r"\bgoogle|gemini|deepmind|alphabet\b", re.I), ["GOOGL"]),
    (re.compile(r"\bmeta|llama|facebook\b", re.I), ["META"]),
    (re.compile(r"\bmicrosoft|copilot|azure ai\b", re.I), ["MSFT"]),
    (re.compile(r"\bnvidia\b", re.I), ["NVDA"]),
    (re.compile(r"\bamazon|bedrock\b", re.I), ["AMZN"]),
    (re.compile(r"\bapple\b", re.I), ["AAPL"]),
    (re.compile(r"\bxai|grok\b", re.I), []),
    (re.compile(r"\bdeepseek\b", re.I), []),
    (re.compile(r"\bmistral\b", re.I), []),
]


def is_ai_source(headline: Headline) -> bool:
    return headline.source in AI_SOURCES


def mentions_ai(text: str) -> bool:
    return bool(AI_NEWS_SIGNALS.search(text))


def is_material_ai_update(text: str, headline: Headline | None = None) -> bool:
    """True for real AI product news — not military 'launches' or generic verbs."""
    if GEOPOLITICS_CONFLICT.search(text) and not AI_LAB_NAMES.search(text):
        return False
    if not AI_PRODUCT_SIGNALS.search(text):
        return False
    if headline is not None and is_ai_source(headline):
        return True
    return bool(AI_LAB_NAMES.search(text))


def infer_ai_tickers(text: str) -> list[str]:
    tickers: list[str] = []
    for pattern, symbols in COMPANY_TICKER_MAP:
        if pattern.search(text):
            for sym in symbols:
                if sym not in tickers:
                    tickers.append(sym)
    return tickers


def enrich_ai_classification(classification: dict, headline: Headline) -> dict:
    """Ensure AI stories have category and tickers for the hard filter."""
    text = f"{headline.title} {headline.summary}"
    if classification.get("category") in ("geopolitics", "macro", "earnings"):
        return classification
    if classification.get("category") != "ai" and (
        is_ai_source(headline) or is_material_ai_update(text, headline)
    ):
        classification["category"] = "ai"

    if classification.get("category") == "ai":
        existing = [t.upper() for t in classification.get("tickers", [])]
        for ticker in infer_ai_tickers(text):
            if ticker not in existing:
                existing.append(ticker)
        classification["tickers"] = existing
        # AI product news is post-worthy even without a stock trade
        if classification.get("impact") in ("high", "med"):
            classification["tradeable"] = True

    return classification
