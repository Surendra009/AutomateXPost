"""Template-based drafts — zero LLM for structured earnings, macro, and AI launches."""

from __future__ import annotations

import re
from dataclasses import dataclass

from models import Headline
from pipeline.ai_news import (
    AI_PRODUCT_SIGNALS,
    infer_ai_tickers,
    is_ai_source,
    is_material_ai_update,
)

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
EPS_VS = re.compile(
    r"eps\s+(?:of\s+)?\$?([\d.]+).*?(?:vs\.?|versus|est\.?|expected)\s*\$?([\d.]+)",
    re.I,
)
REV_VS = re.compile(
    r"revenue\s+(?:of\s+)?\$?([\d.]+)\s*([bBmM])?.*?(?:vs\.?|versus|est\.?|expected)\s*\$?([\d.]+)\s*([bBmM])?",
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

MACRO_TAKEAWAY = {
    "CPI": "Inflation print shifts rate-cut expectations",
    "PPI": "Producer prices feed into the inflation outlook",
    "Nonfarm payrolls": "Labor strength affects Fed and rate path",
    "Jobs report": "Labor strength affects Fed and rate path",
    "Unemployment": "Labor market signal for the Fed",
    "GDP": "Growth read shapes recession vs soft-landing odds",
    "Fed": "Rates path repriced across stocks and bonds",
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
    for builder in (try_earnings_template, try_macro_template, try_ai_launch_template):
        result = builder(headline, classification)
        if result:
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


def _fmt_rev(num: str, suffix: str | None = None) -> str:
    try:
        val = float(num)
    except ValueError:
        return f"${num}"
    if suffix:
        return f"${num}{suffix.upper()}"
    if val >= 1:
        return f"${val:.2f}B" if val < 1000 else f"${val/1000:.2f}T"
    return f"${val:.2f}"


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
    line2_parts: list[str] = []

    eps_m = EPS_VS.search(text)
    if eps_m:
        line1 = f"{ticker} {verb} EPS ${eps_m.group(1)} vs ${eps_m.group(2)} est"
    else:
        line1 = f"{ticker} {verb} quarterly earnings expectations"

    rev_m = REV_VS.search(text)
    if rev_m:
        actual = _fmt_rev(rev_m.group(1), rev_m.group(2))
        est = _fmt_rev(rev_m.group(3), rev_m.group(4))
        line2_parts.append(f"Revenue {actual} vs {est} est")
    elif verb == "beat":
        line2_parts.append("Top-line or guidance surprised to the upside")
    elif verb == "missed":
        line2_parts.append("Stock likely repricing on the disappointment")
    else:
        line2_parts.append("Results largely in line with the street")

    line2 = line2_parts[0] if line2_parts else ""
    ticker_line = " ".join(f"${t}" for t in tickers)
    body = f"{line1}\n{line2}\n\n{ticker_line}".strip()
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


def try_macro_template(headline: Headline, classification: dict) -> TemplateDraft | None:
    text = f"{headline.title} {headline.summary}"
    label = _macro_label(text)
    if not label and classification.get("category") != "macro":
        return None
    if not label:
        return None

    pct_vs = PCT_VS.search(text)
    if pct_vs:
        line1 = f"{label} came in at {pct_vs.group(1)}% vs {pct_vs.group(2)}% expected"
    else:
        pct = SINGLE_PCT.search(headline.title)
        if pct:
            line1 = f"{label} printed at {pct.group(1)}%"
        else:
            line1 = _shorten(headline.title, 72)

    line2 = MACRO_TAKEAWAY.get(label, "Macro data moves rates and risk assets")
    body = f"{line1}\n{line2}\n\n$SPY"
    return TemplateDraft(
        text=body,
        format="BREAKING",
        tickers=["SPY"],
        confidence=0.9,
        category="macro",
        impact="high",
    )


def _shorten(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rsplit(" ", 1)[0] + "…"


def _ai_takeaway(title: str, summary: str) -> str:
    blob = f"{title} {summary}".lower()
    if re.search(r"\bapi\b|sdk|developers?", blob):
        return "Gives builders a new hook into the stack"
    if re.search(r"\b(model|gpt|claude|gemini|llama)\b", blob):
        return "Raises the bar in the model race"
    if re.search(r"\bmobile|app|ios|android\b", blob):
        return "Puts AI in more users' hands"
    if re.search(r"\bagent|copilot|automation\b", blob):
        return "Pushes agents closer to daily workflows"
    return "Another step in the AI product war"


def try_ai_launch_template(headline: Headline, classification: dict) -> TemplateDraft | None:
    text = f"{headline.title} {headline.summary}"
    is_ai = classification.get("category") == "ai" or is_ai_source(headline)
    if not is_ai or not is_material_ai_update(text):
        return None

    # Prefer official blogs; others need product signal in title
    if headline.source not in AI_BLOG_SOURCES and not AI_PRODUCT_SIGNALS.search(headline.title):
        return None

    tickers = _extract_tickers(headline, classification)
    line1 = _shorten(headline.title, 72)
    line2 = _ai_takeaway(headline.title, headline.summary)

    if tickers:
        body = f"{line1}\n{line2}\n\n" + " ".join(f"${t}" for t in tickers)
    else:
        body = f"{line1}\n{line2}"

    confidence = 0.91 if headline.source in AI_BLOG_SOURCES else 0.85
    return TemplateDraft(
        text=body,
        format="BREAKING" if headline.source in {"OpenAI Blog", "Google AI Blog"} else "CONTEXT",
        tickers=tickers,
        confidence=confidence,
        category="ai",
        impact="high" if headline.source in AI_BLOG_SOURCES else "med",
    )
