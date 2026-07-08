"""Template-based drafts — zero LLM for structured earnings and macro."""

from __future__ import annotations

import re
from dataclasses import dataclass

from models import Headline
from pipeline.ai_news import (
    AI_PRODUCT_SIGNALS,
    GEOPOLITICS_CONFLICT,
    infer_ai_tickers,
    is_ai_source,
    is_material_ai_update,
)
from pipeline.draft_quality import is_too_generic
from pipeline.earnings_parse import build_earnings_lines, extract_earnings_facts
from pipeline.enrich import fetch_article_text

# ── Earnings patterns ─────────────────────────────────────────────────────

EARNINGS_CONTEXT = re.compile(
    r"\b(eps|earnings|revenue|sales|quarterly results|q[1-4])\b",
    re.I,
)
BEAT_MISS = re.compile(
    r"\b(beat|beats|beating|topped|tops|exceeded|exceeds|surpassed|"
    r"missed|misses|missing|fell short|below expectations|in.?line with)\b",
    re.I,
)
INLINE_TICKER = re.compile(r"\$([A-Z]{1,5})\b")
PAREN_TICKER = re.compile(r"\(([A-Z]{1,5})\)")

# ── Macro patterns ────────────────────────────────────────────────────────

MACRO_KIND = re.compile(
    r"\b("
    r"cpi|consumer price index|ppi|producer price|"
    r"nfp|nonfarm payrolls|jobs report|unemployment rate|"
    r"gdp|gross domestic product|"
    r"fomc|fed (?:holds|cuts|hikes|raises|lowers|keeps)|"
    r"interest rate decision"
    r")\b",
    re.I,
)
PCT_VS = re.compile(
    r"(\d+\.?\d*)%.*?(?:vs\.?|versus|expected|forecast|est\.?)\s*(\d+\.?\d*)%",
    re.I,
)
SINGLE_PCT = re.compile(r"(\d+\.?\d*)%")

MACRO_LABELS = {
    "cpi": "CPI",
    "consumer price index": "CPI",
    "ppi": "PPI",
    "producer price": "PPI",
    "nfp": "Nonfarm payrolls",
    "nonfarm payrolls": "Nonfarm payrolls",
    "jobs report": "Jobs report",
    "unemployment rate": "Unemployment",
    "gdp": "GDP",
    "gross domestic product": "GDP",
    "fomc": "Fed",
    "fed": "Fed",
    "interest rate decision": "Fed",
}

# Official AI blogs — highest confidence for templates
AI_BLOG_SOURCES = {
    "OpenAI Blog",
    "Google AI Blog",
    "The Verge AI",
    "TechCrunch AI",
    "VentureBeat AI",
}


@dataclass
class TemplateDraft:
    text: str
    format: str
    tickers: list[str]
    confidence: float
    category: str
    impact: str


def try_template_draft(headline: Headline, classification: dict) -> TemplateDraft | None:
    """Return a template draft if the story is structured enough, else None."""
    for builder in (
        try_earnings_template,
        try_macro_template,
        try_ai_launch_template,
    ):
        result = builder(headline, classification)
        if result and not is_too_generic(result.text, headline.title):
            return result
    return None


def _extract_tickers(headline: Headline, classification: dict) -> list[str]:
    tickers = [t.upper() for t in classification.get("tickers") or []]
    if tickers:
        return tickers
    text = f"{headline.title} {headline.summary}"
    for pat in (INLINE_TICKER, PAREN_TICKER):
        for m in pat.finditer(text):
            sym = m.group(1).upper()
            if sym not in tickers and len(sym) >= 1:
                tickers.append(sym)
    if not tickers:
        tickers = infer_ai_tickers(text)
    return tickers[:3]


def _beat_miss_verb(match: re.Match) -> str:
    word = match.group(0).lower()
    if word in ("missed", "misses", "missing", "fell short", "below expectations"):
        return "missed"
    if "in-line" in word or "inline" in word:
        return "matched"
    return "beat"


def try_earnings_template(headline: Headline, classification: dict) -> TemplateDraft | None:
    text = f"{headline.title} {headline.summary}"
    if classification.get("category") != "earnings" and not EARNINGS_CONTEXT.search(text):
        return None
    bm = BEAT_MISS.search(headline.title)
    if not bm:
        return None

    tickers = _extract_tickers(headline, classification)
    if not tickers:
        return None

    verb = _beat_miss_verb(bm)
    ticker = tickers[0]
    facts = extract_earnings_facts(text)
    lines = build_earnings_lines(ticker, verb, facts)
    if not lines and headline.url:
        article = fetch_article_text(headline.url)
        if article:
            facts = extract_earnings_facts(f"{text} {article[:2500]}")
            lines = build_earnings_lines(ticker, verb, facts)
    if not lines:
        return None

    line1, line2, line3 = lines
    ticker_line = " ".join(f"${t}" for t in tickers)
    body = f"{line1}\n{line2}\n{line3}\n\n{ticker_line}".strip()
    impact = "high" if verb in ("beat", "missed") else "med"

    return TemplateDraft(
        text=body,
        format="BREAKING",
        tickers=tickers,
        confidence=0.92,
        category="earnings",
        impact=impact,
    )


def _macro_label(text: str) -> str | None:
    m = MACRO_KIND.search(text)
    if not m:
        return None
    key = m.group(1).lower()
    for pattern, label in MACRO_LABELS.items():
        if pattern in key or key in pattern:
            return label
    return m.group(1).upper()


def _macro_direction(label: str, actual: float, expected: float) -> tuple[str, str]:
    """Return line2 (surprise frame) and line3 (market implication)."""
    diff = actual - expected
    hot = diff > 0.02
    cool = diff < -0.02

    if label in ("CPI", "PPI"):
        if hot:
            return (
                f"Hotter than expected — bond yields and the dollar tend to firm",
                "Rate-cut bets get pushed out; growth vs duration trade tilts defensive",
            )
        if cool:
            return (
                f"Softer than expected — yields often ease on the print",
                "Bonds catch a bid; rate-sensitive growth can outperform",
            )
        return (
            f"In-line with consensus — the reaction is all about the details",
            "Headline matched; markets trade the core vs shelter split",
        )

    if label in ("Nonfarm payrolls", "Jobs report"):
        if hot:
            return (
                f"Stronger labor print — Fed has less room to ease",
                "Wages and hours matter for the next leg in rates",
            )
        if cool:
            return (
                f"Weaker jobs data — easing odds tick higher",
                "Soft landing narrative gets another datapoint",
            )
        return (
            f"Jobs landed near consensus",
            "Unemployment and wage growth set the Fed read-through",
        )

    if label == "Unemployment":
        if hot:
            return (
                f"Unemployment rose more than expected",
                "Labor slack builds — dovish for rates and duration",
            )
        if cool:
            return (
                f"Tighter labor market than expected",
                "Sticky jobs keep the Fed cautious on cuts",
            )
        return (
            f"Jobless rate matched expectations",
            "Participation and wage data drive the bond move",
        )

    if label == "GDP":
        if hot:
            return (
                f"Growth beat — recession fears ease",
                "Cyclicals and small caps often lead on a strong GDP surprise",
            )
        if cool:
            return (
                f"Growth missed — soft-landing trade gets tested",
                "Defensives and quality factor tend to hold up",
            )
        return (
            f"GDP matched the street",
            "Composition (consumer vs capex) moves sector leadership",
        )

    if label == "Fed":
        return (
            f"Policy path repriced across front-end yields",
            "Equities trade the dot plot and Powell tone, not just the headline",
        )

    return (
        f"Macro surprise shifts the rates backdrop",
        "Risk assets reprice the path for cuts and growth",
    )


def try_macro_template(headline: Headline, classification: dict) -> TemplateDraft | None:
    text = f"{headline.title} {headline.summary}"
    label = _macro_label(text)
    if not label and classification.get("category") != "macro":
        return None
    if not label:
        return None

    pct_vs = PCT_VS.search(text)
    if pct_vs:
        actual = float(pct_vs.group(1))
        expected = float(pct_vs.group(2))
        line1 = f"{label} {actual:g}% vs {expected:g}% est"
        line2, line3 = _macro_direction(label, actual, expected)
    else:
        pct = SINGLE_PCT.search(headline.title)
        if not pct:
            return None
        line1 = f"{label} printed {pct.group(1)}%"
        line2 = f"Street will benchmark the reaction in rates and the dollar"
        line3 = "Watch front-end yields for the first tell"

    body = f"{line1}\n{line2}\n{line3}\n\n$SPY"
    return TemplateDraft(
        text=body,
        format="BREAKING",
        tickers=["SPY"],
        confidence=0.9,
        category="macro",
        impact="high",
    )


_MODEL_HOOK = re.compile(
    r"\b(GPT[- ]?\d[\w.-]*|Claude [\d.]+|Gemini [\w.]+|Llama [\d.]+|"
    r"Copilot [\w.]+|Sora|o\d(?:-mini|-pro)?)\b",
    re.I,
)
_SHIPS = re.compile(r"\b(launches?|ships?|releases?|unveils?|introduces?|debuts?)\b", re.I)


def _ai_hook(title: str, summary: str) -> str | None:
    """Build an original hook — never return a truncated headline."""
    blob = f"{title} {summary}"
    model = _MODEL_HOOK.search(blob)
    ships = _SHIPS.search(title)
    if model and ships:
        name = model.group(1)
        return f"New {name} is live — capability jump, not a press-release tweak"
    if model:
        return f"{model.group(1)} update drops — compare latency, price, and context window"
    if re.search(r"\bapi\b|sdk", blob, re.I):
        return "Fresh API surface for builders — integration speed is the moat"
    if re.search(r"\bagent|copilot|automation\b", blob, re.I):
        return "Agents get another production hook — workflow lock-in matters"
    return None


def _ai_takeaway(title: str, summary: str) -> str:
    blob = f"{title} {summary}".lower()
    if re.search(r"\bopenai\b", blob):
        return "MSFT/OpenAI ecosystem gets another reason to keep capex flowing"
    if re.search(r"\bgoogle\b|gemini", blob):
        return "GOOGL can bundle this into Search, Cloud, and Android"
    if re.search(r"\banthropic\b|claude", blob):
        return "Enterprise AI spend shifts toward reliability and safety"
    if re.search(r"\bmeta\b|llama", blob):
        return "Open weights pressure closed-model pricing"
    if re.search(r"\bnvidia\b|cuda", blob):
        return "More demand for inference and training silicon"
    return "Sets the bar for what rivals must match this quarter"


def try_ai_launch_template(headline: Headline, classification: dict) -> TemplateDraft | None:
    text = f"{headline.title} {headline.summary}"
    if GEOPOLITICS_CONFLICT.search(text) and not is_ai_source(headline):
        return None
    is_ai = classification.get("category") == "ai" or is_ai_source(headline)
    if not is_ai or not is_material_ai_update(text, headline):
        return None

    if headline.source not in AI_BLOG_SOURCES and not AI_PRODUCT_SIGNALS.search(headline.title):
        return None

    line1 = _ai_hook(headline.title, headline.summary or "")
    if not line1:
        return None

    tickers = _extract_tickers(headline, classification)
    line2 = _ai_takeaway(headline.title, headline.summary or "")
    line3 = "Competitors have to respond or concede the narrative"

    if tickers:
        body = f"{line1}\n{line2}\n{line3}\n\n" + " ".join(f"${t}" for t in tickers)
    else:
        body = f"{line1}\n{line2}\n{line3}"

    confidence = 0.91 if headline.source in AI_BLOG_SOURCES else 0.85
    return TemplateDraft(
        text=body,
        format="BREAKING" if headline.source in {"OpenAI Blog", "Google AI Blog"} else "CONTEXT",
        tickers=tickers,
        confidence=confidence,
        category="ai",
        impact="high" if headline.source in AI_BLOG_SOURCES else "med",
    )
