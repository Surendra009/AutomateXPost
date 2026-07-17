"""Template-based drafts — zero LLM for structured earnings, macro, and AI launches."""

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
from pipeline.earnings_dedup import earnings_ticker_blocked
from pipeline.earnings_enrich import enrich_earnings_context
from pipeline.draft_quality import draft_quality_reason
from pipeline.earnings_freshness import earnings_draft_period_allowed
from pipeline.earnings_parse import (
    extract_earnings_facts,
    format_earnings_draft,
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

MACRO_TAKEAWAY: dict[str, str] = {}  # factual line2 comes from headline text

GEOPOLITICS_SIGNAL = re.compile(
    r"\b("
    r"iran|israel|ukraine|russia|strike|missile|war|tanker|oil|crude|hormuz|"
    r"military|pentagon|troops|navy|drone|sanctions|conflict|attacks"
    r")\b",
    re.I,
)

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
        try_geopolitics_template,
        try_ai_launch_template,
    ):
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
    from pipeline.earnings_freshness import parse_quarter_year_from_text

    # Lead period must be in-season (blocks "fourth quarter 2025" / "Q4 last year")
    if not earnings_draft_period_allowed(text):
        return None
    bm = BEAT_MISS.search(headline.title)
    if not bm:
        return None

    tickers = _extract_tickers(headline, classification)
    if not tickers:
        return None

    verb = _beat_miss_verb(bm)
    ticker = tickers[0]
    if earnings_ticker_blocked(ticker):
        return None
    facts = extract_earnings_facts(text)
    parsed_q, parsed_y = parse_quarter_year_from_text(text)
    enrichment = enrich_earnings_context(
        ticker,
        quarter=parsed_q,
        year=parsed_y,
        finnhub_facts=facts,
        finnhub_summary=text,
        headline_url=headline.url or "",
        skip_web_search=False,
    )
    combined = f"{text} {enrichment.news_context} {enrichment.article_text[:3000]}"
    if not earnings_draft_period_allowed(combined, quarter=parsed_q, year=parsed_y):
        return None
    if enrichment.news_context or enrichment.article_text:
        facts = enrichment.facts or extract_earnings_facts(combined)
    else:
        facts = enrichment.facts or facts

    if not facts.has_numbers():
        return None
    if parsed_q and not facts.quarter:
        facts.quarter = f"Q{parsed_q}"

    highlights = list(enrichment.highlights or [])
    if not highlights:
        from pipeline.earnings_parse import extract_earnings_highlights

        highlights = extract_earnings_highlights(
            " ".join(
                part
                for part in (enrichment.news_context, enrichment.article_text, text)
                if part
            ),
            ticker=ticker,
            allow_llm=True,
            html=enrichment.press_html,
        )

    company_name = None
    try:
        from pipeline.earnings_press import get_company_profile

        company_name = (get_company_profile(ticker).get("name") or "").strip() or None
    except Exception:
        company_name = None

    body = format_earnings_draft(
        ticker,
        verb,
        facts,
        highlights=highlights,
        year=parsed_y,
        company_name=company_name,
    )
    if len(tickers) > 1:
        body = f"{body}\n\n" + " ".join(f"${t}" for t in tickers)
    impact = "high" if verb in ("beat", "missed") else "med"

    return TemplateDraft(
        text=body,
        format="SUMMARY",
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

    line2 = MACRO_TAKEAWAY.get(label) or _second_fact_from_text(headline.title, headline.summary, label)
    if draft_quality_reason(line2):
        line2 = _second_fact_from_text(headline.title, headline.summary, label)
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


def _fact_line_from_text(title: str, summary: str, fallback: str) -> str:
    text = f"{title} {summary}"
    pct = re.search(r"(\d+\.?\d*)%", text)
    money = re.search(r"\$[\d,.]+[BMK]?", text)
    if pct and money:
        return f"{money.group(0)} print ({pct.group(1)}% move) in the release"
    if money:
        return f"Deal/value cited at {money.group(0)}"
    if pct:
        return f"Key metric moved {pct.group(1)}%"
    line = _shorten(summary or title, 95)
    return line if len(line) > 20 and not draft_quality_reason(line) else fallback


def _second_fact_from_text(title: str, summary: str, label: str) -> str:
    """Second factual line for macro/templates — no interpretation."""
    text = f"{title} {summary}"
    prior = re.search(
        r"(?:prior|previous|last)\s+(?:month|quarter|year|reading)\s+(?:was\s+)?(\d+\.?\d*)%?",
        text,
        re.I,
    )
    if prior:
        return f"Prior {label} reading was {prior.group(1)}%"
    jobs = re.search(r"(\d[\d,]*)\s+jobs", text, re.I)
    if jobs:
        return f"Reported {jobs.group(1)} jobs"
    return _fact_line_from_text(title, summary, f"{label} report vs expectations")


def _ai_takeaway(title: str, summary: str) -> str:
    blob = f"{title} {summary}"
    named = re.search(
        r"\b(GPT-\d|Claude|Gemini|Llama|Copilot|API|SDK|iOS|Android)\b[^.]{0,60}",
        blob,
        re.I,
    )
    if named:
        return _shorten(named.group(0).strip(), 95)
    return _fact_line_from_text(title, summary, _shorten(summary or title, 95))


def _geopolitics_takeaway(title: str, summary: str) -> str:
    blob = f"{title} {summary}"
    oil = re.search(r"\b(\d+\.?\d*)%?\s*(?:jump|rise|surge|fall|drop)?[^.]{0,30}\b(oil|crude|brent|wti)\b", blob, re.I)
    if oil:
        return _shorten(oil.group(0).strip(), 95)
    return _fact_line_from_text(title, summary, _shorten(summary or title, 95))


def try_geopolitics_template(headline: Headline, classification: dict) -> TemplateDraft | None:
    text = f"{headline.title} {headline.summary}"
    cat = classification.get("category", "other")
    if cat == "ai":
        return None
    if cat not in ("geopolitics", "macro", "other") and not GEOPOLITICS_SIGNAL.search(text):
        return None
    if not GEOPOLITICS_SIGNAL.search(text):
        return None

    tickers = _extract_tickers(headline, classification)
    if not tickers:
        return None

    line1 = _shorten(headline.title, 95)
    line2 = _geopolitics_takeaway(headline.title, headline.summary)
    line3 = _fact_line_from_text(headline.title, headline.summary, _shorten(headline.summary or headline.title, 95))
    body = f"{line1}\n{line2}\n{line3}\n\n" + " ".join(f"${t}" for t in tickers)

    return TemplateDraft(
        text=body,
        format="BREAKING",
        tickers=tickers,
        confidence=0.88,
        category="geopolitics",
        impact=classification.get("impact", "high"),
    )


def try_ai_launch_template(headline: Headline, classification: dict) -> TemplateDraft | None:
    text = f"{headline.title} {headline.summary}"
    if GEOPOLITICS_CONFLICT.search(text) and not is_ai_source(headline):
        return None
    is_ai = classification.get("category") == "ai" or is_ai_source(headline)
    if not is_ai or not is_material_ai_update(text, headline):
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
