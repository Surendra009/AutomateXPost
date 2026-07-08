"""Extract EPS/revenue figures and commentary highlights from earnings text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import ANTHROPIC_API_KEY, FILTER_MODEL
from logging_config import setup_logging

logger = setup_logging()

QUARTER = re.compile(r"\b(q[1-4])\b", re.I)

_EPS_VS_PATTERNS = (
    re.compile(
        r"eps\s+(?:of\s+)?\$?([\d.]+).*?(?:vs\.?|versus|compared to|against|est\.?|expected|consensus|estimate)\s*\$?([\d.]+)",
        re.I,
    ),
    re.compile(
        r"\$?([\d.]+)\s*(?:eps|per share).*?(?:vs\.?|versus|compared|against|est\.?|expected|consensus|estimate)\s*\$?([\d.]+)",
        re.I,
    ),
    re.compile(
        r"(?:reported|posted|earned|delivered)\s+(?:eps\s+)?(?:of\s+)?\$?([\d.]+).*?(?:vs\.?|versus|compared|against|estimate|est\.?|expected|consensus)\s*(?:of\s+)?\$?([\d.]+)",
        re.I,
    ),
    re.compile(
        r"(?:beat|topped|exceeded|surpassed).*?(?:estimate|est\.?|expected|consensus).*?\$?([\d.]+).*?(?:with|at|reporting|posted|earned|delivered)\s+\$?([\d.]+)",
        re.I,
    ),
    re.compile(
        r"(?:missed|fell short).*?(?:estimate|est\.?|expected|consensus).*?\$?([\d.]+).*?(?:with|at|reporting|posted|earned)\s+\$?([\d.]+)",
        re.I,
    ),
)

_EPS_SINGLE = re.compile(
    r"(?:earnings per share|eps)(?:\s+of)?\s+\$?([\d.]+)",
    re.I,
)

_REV_VS_PATTERNS = (
    re.compile(
        r"(?:revenue|sales)\s+(?:of\s+)?\$?([\d,.]+)\s*([bBmMkK])?.*?(?:vs\.?|versus|compared|against|est\.?|expected|consensus|estimate)\s*\$?([\d,.]+)\s*([bBmMkK])?",
        re.I,
    ),
    re.compile(
        r"\$?([\d,.]+)\s*([bBmMkK])?\s+(?:in\s+)?(?:revenue|sales).*?(?:vs\.?|versus|est\.?|expected|consensus)\s*\$?([\d,.]+)\s*([bBmMkK])?",
        re.I,
    ),
)

_REV_SINGLE = re.compile(
    r"(?:revenue|sales)(?:\s+of)?\s+\$?([\d,.]+)\s*([bBmMkK])?",
    re.I,
)

_YOY_PCT = re.compile(
    r"(?:revenue|sales|eps|earnings).*?(?:up|rose|grew|increased|jumped|climbed)\s+(\d+\.?\d*)%",
    re.I,
)

_EARNINGS_NEWS = re.compile(
    r"\b(eps|earnings|revenue|sales|quarterly results|guidance|outlook)\b",
    re.I,
)

_SEGMENT_GROWTH = re.compile(
    r"\b(data[- ]?center|cloud|AI|artificial intelligence|services|iPhone|iPad|"
    r"advertising|subscriptions|automotive|gaming|infrastructure|hyperscaler|"
    r"Azure|AWS|Copilot|GPU|chips?|iPhone|Mac|Windows|Search)\b[^.]{0,80}?"
    r"(?:up|rose|grew|surged|jumped|climbed|increased|fell|declined|slipped)\s+"
    r"(?:by\s+)?(\d+\.?\d*)%",
    re.I,
)

_GUIDANCE_CHANGE = re.compile(
    r"\b(guidance|outlook|forecast)\b[^.]{0,100}?"
    r"\b(raised|lowered|cut|hiked|increased|reduced|tightened|maintained|reaffirmed|"
    r"below|above|surpassed|missed)\b",
    re.I,
)

_MARGIN_MOVE = re.compile(
    r"\b(gross|operating|net)\s+margins?\b[^.]{0,50}?"
    r"\b(expanded|widened|improved|contracted|compressed|fell|declined|narrowed)\b",
    re.I,
)

_DEMAND_SIGNAL = re.compile(
    r"\b(demand|orders|bookings|backlog|pipeline)\b[^.]{0,60}?"
    r"\b(strong|weak|robust|soft|record|solid|muted)\b",
    re.I,
)

_BEAT_DRIVER = re.compile(
    r"\b(?:beat|topped|exceeded|surpassed|missed|fell short)\b[^.]{0,40}?"
    r"(?:on|driven by|thanks to|as|amid|due to)\s+(.+?)(?:\.|,|;|$)",
    re.I,
)

_QUOTE_CLAUSE = re.compile(
    r"\b(?:CEO|CFO|executive|management|company)\b[^.]{0,30}?"
    r"(?:said|says|expects|forecast|sees|cited|noted)\b[^.]{0,120}\.",
    re.I,
)

_HIGHLIGHT_SKIP = re.compile(
    r"\b(eps|revenue|per share|consensus|estimate|expected|vs\.?|versus)\b",
    re.I,
)

_LLM_HIGHLIGHTS_PER_CYCLE = 3
_llm_highlight_calls = 0


def reset_earnings_highlight_budget() -> None:
    global _llm_highlight_calls
    _llm_highlight_calls = 0


@dataclass
class EarningsFacts:
    quarter: str | None = None
    eps_actual: str | None = None
    eps_estimate: str | None = None
    revenue_actual: str | None = None
    revenue_estimate: str | None = None
    yoy_pct: str | None = None

    def has_numbers(self) -> bool:
        if self.eps_actual or self.revenue_actual:
            return True
        if self.eps_actual and self.eps_estimate:
            return True
        if self.revenue_actual and self.revenue_estimate:
            return True
        return bool(self.yoy_pct and (self.eps_actual or self.revenue_actual))


def _fmt_money(num: str, suffix: str | None = None) -> str:
    cleaned = num.replace(",", "")
    if suffix:
        return f"${cleaned}{suffix.upper()}"
    try:
        val = float(cleaned)
    except ValueError:
        return f"${num}"
    if val >= 1_000:
        return f"${val/1_000:.2f}B" if val < 1_000_000 else f"${val/1_000_000:.2f}M"
    return f"${val:.2f}"


def _fmt_eps(value: str) -> str:
    try:
        return f"${float(value):.2f}"
    except ValueError:
        return f"${value}"


def _eps_surprise_pct(actual: str, estimate: str) -> float | None:
    try:
        a = float(actual.replace("$", ""))
        e = float(estimate.replace("$", ""))
        if e == 0:
            return None
        return (a - e) / abs(e) * 100
    except ValueError:
        return None


def _trim_highlight(text: str, max_len: int = 95) -> str:
    text = re.sub(r"\s+", " ", text).strip(" \"'")
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rsplit(" ", 1)[0]
    return cut + "…"


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) > 25]


def _sentence_highlight_score(sentence: str) -> int:
    score = 0
    lower = sentence.lower()
    if _HIGHLIGHT_SKIP.search(sentence) and not (
        _SEGMENT_GROWTH.search(sentence) or _GUIDANCE_CHANGE.search(sentence)
    ):
        score -= 20
    if _SEGMENT_GROWTH.search(sentence):
        score += 40
    if _GUIDANCE_CHANGE.search(sentence):
        score += 45
    if _MARGIN_MOVE.search(sentence):
        score += 35
    if _DEMAND_SIGNAL.search(sentence):
        score += 30
    if _QUOTE_CLAUSE.search(sentence):
        score += 25
    for word in ("cloud", "data center", "ai", "guidance", "margin", "outlook", "demand"):
        if word in lower:
            score += 8
    if re.search(r"\d+\.?\d*%", sentence):
        score += 5
    return score


def extract_earnings_highlight(text: str) -> str | None:
    """Pull a commentary line — segment, guidance, margins, demand, or management angle."""
    if not text or len(text.strip()) < 25:
        return None

    blob = re.sub(r"\s+", " ", text).strip()
    candidates: list[tuple[int, str]] = []

    seg = _SEGMENT_GROWTH.search(blob)
    if seg:
        segment = seg.group(1).strip()
        pct = seg.group(2)
        candidates.append((92, f"{segment} revenue up {pct}% — stood out in the quarter"))

    guide = _GUIDANCE_CHANGE.search(blob)
    if guide:
        phrase = guide.group(0).strip()
        candidates.append((90, _trim_highlight(phrase.capitalize())))

    margin = _MARGIN_MOVE.search(blob)
    if margin:
        candidates.append((85, _trim_highlight(margin.group(0).capitalize())))

    demand = _DEMAND_SIGNAL.search(blob)
    if demand:
        candidates.append((80, _trim_highlight(demand.group(0).capitalize())))

    driver = _BEAT_DRIVER.search(blob)
    if driver:
        reason = driver.group(1).strip()
        if len(reason) > 12:
            candidates.append((78, _trim_highlight(f"Beat driven by {reason}")))

    quote = _QUOTE_CLAUSE.search(blob)
    if quote:
        q = quote.group(0).strip()
        q = re.sub(r"^(CEO|CFO|executive|management|company)\s+", "", q, flags=re.I)
        candidates.append((75, _trim_highlight(q)))

    for sentence in _sentences(blob):
        score = _sentence_highlight_score(sentence)
        if score >= 30:
            candidates.append((score, _trim_highlight(sentence)))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def llm_earnings_highlight(source_text: str, ticker: str) -> str | None:
    """One cheap Haiku line when regex can't find commentary in a long article."""
    global _llm_highlight_calls
    if not ANTHROPIC_API_KEY or len(source_text) < 250:
        return None
    if _llm_highlight_calls >= _LLM_HIGHLIGHTS_PER_CYCLE:
        return None

    from pipeline.filter import _call_claude

    prompt = (
        f"Ticker: {ticker}\n"
        f"Text:\n{source_text[:2200]}\n\n"
        "Write ONE earnings commentary line (max 90 chars) about segment strength, "
        "guidance, margins, demand, or management outlook. "
        "Do not repeat EPS/revenue headline numbers. Plain text only."
    )
    system = "You extract one sharp earnings highlight for a stock post."
    raw = _call_claude(system, prompt, FILTER_MODEL, max_tokens=80, retry=False)
    _llm_highlight_calls += 1
    if not raw:
        return None
    line = raw.strip().strip('"').split("\n")[0].strip()
    if len(line) < 15:
        return None
    return _trim_highlight(line)


def resolve_earnings_highlight(
    source_text: str,
    ticker: str,
    *,
    article_text: str = "",
    allow_llm: bool = True,
) -> str | None:
    combined = " ".join(part for part in (source_text, article_text) if part).strip()
    highlight = extract_earnings_highlight(combined)
    if highlight:
        return highlight
    if allow_llm and article_text:
        return llm_earnings_highlight(combined, ticker)
    return extract_earnings_highlight(article_text) if article_text else None


def fetch_earnings_news_context(symbol: str, days_back: int = 2) -> str:
    """Latest Finnhub company-news blurb for earnings commentary."""
    from pipeline.finnhub_api import finnhub_get, get_finnhub_key

    if not get_finnhub_key():
        return ""

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=days_back)).isoformat()
    to_date = today.isoformat()
    data, err = finnhub_get(
        "company-news",
        {"symbol": symbol.upper(), "from": from_date, "to": to_date},
    )
    if err or not isinstance(data, list):
        return ""

    chunks: list[str] = []
    for item in data[:15]:
        headline = (item.get("headline") or "").strip()
        summary = (item.get("summary") or "").strip()
        if not headline:
            continue
        text = f"{headline} {summary}"
        if _EARNINGS_NEWS.search(text):
            chunks.append(text)
    return " ".join(chunks)[:3000]


def extract_earnings_facts(text: str) -> EarningsFacts:
    facts = EarningsFacts()
    q = QUARTER.search(text)
    if q:
        facts.quarter = q.group(1).upper()

    for pattern in _EPS_VS_PATTERNS:
        match = pattern.search(text)
        if match:
            facts.eps_actual = _fmt_eps(match.group(1))
            facts.eps_estimate = _fmt_eps(match.group(2))
            break

    if not facts.eps_actual:
        single = _EPS_SINGLE.search(text)
        if single:
            facts.eps_actual = _fmt_eps(single.group(1))

    for pattern in _REV_VS_PATTERNS:
        match = pattern.search(text)
        if match:
            facts.revenue_actual = _fmt_money(match.group(1), match.group(2))
            facts.revenue_estimate = _fmt_money(match.group(3), match.group(4))
            break

    if not facts.revenue_actual:
        single = _REV_SINGLE.search(text)
        if single:
            facts.revenue_actual = _fmt_money(single.group(1), single.group(2))

    pct = _YOY_PCT.search(text)
    if pct:
        facts.yoy_pct = pct.group(1)

    return facts


def build_earnings_line3(ticker: str, verb: str, facts: EarningsFacts) -> str:
    if verb == "beat":
        if facts.eps_actual and facts.eps_estimate:
            surprise = _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)
            if surprise is not None and surprise >= 8:
                return f"Big EPS beat — {ticker} likely gaps up unless guidance disappoints"
        return f"Beat leans bullish into the call; guidance sets the real move"
    if verb == "missed":
        return f"Miss opens a gap-down risk — watch for guide cuts on the call"
    return f"Print is in — segment mix and outlook drive the next leg"


def build_earnings_lines(
    ticker: str,
    verb: str,
    facts: EarningsFacts,
    *,
    source_text: str = "",
    article_text: str = "",
    allow_llm: bool = True,
) -> tuple[str, str, str] | None:
    """Hook (numbers) + commentary highlight + trade implication."""
    if not facts.has_numbers():
        return None

    q = f"{facts.quarter} " if facts.quarter else ""

    if facts.eps_actual and facts.eps_estimate:
        surprise = _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)
        if surprise is not None and abs(surprise) >= 1:
            direction = "above" if surprise >= 0 else "below"
            line1 = (
                f"{ticker} {verb} {q}EPS {facts.eps_actual} — "
                f"{abs(surprise):.0f}% {direction} consensus"
            )
        else:
            line1 = f"{ticker} {verb} {q}EPS {facts.eps_actual} vs {facts.eps_estimate} est"
    elif facts.eps_actual:
        line1 = f"{ticker} posted {q}EPS {facts.eps_actual}"
    elif facts.revenue_actual and facts.revenue_estimate:
        line1 = f"{ticker} {verb} {q}revenue {facts.revenue_actual} vs {facts.revenue_estimate} est"
    elif facts.revenue_actual:
        line1 = f"{ticker} posted {q}revenue {facts.revenue_actual}"
    else:
        return None

    highlight = resolve_earnings_highlight(
        source_text,
        ticker,
        article_text=article_text,
        allow_llm=allow_llm,
    )

    if highlight:
        line2 = highlight
    elif facts.revenue_actual and facts.revenue_estimate:
        line2 = f"Revenue {facts.revenue_actual} vs {facts.revenue_estimate} est"
    elif facts.yoy_pct:
        line2 = f"Revenue growth ran +{facts.yoy_pct}% year over year"
    elif facts.eps_actual and facts.eps_estimate:
        surprise = _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)
        if surprise is not None:
            word = "cleared" if surprise >= 0 else "missed"
            line2 = f"EPS {word} the street by {abs(surprise):.0f}%"
        else:
            line2 = f"Headline numbers vs the street — details on the call"
    else:
        line2 = f"Segment commentary and guide will frame the trade"

    line3 = build_earnings_line3(ticker, verb, facts)
    return line1, line2, line3
