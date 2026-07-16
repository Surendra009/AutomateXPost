"""Extract EPS/revenue figures and commentary highlights from earnings text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import ANTHROPIC_API_KEY, FILTER_MODEL
from logging_config import setup_logging
from pipeline.draft_quality import draft_quality_reason

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
    r"\b("
    r"data[- ]?center|cloud|AI|artificial intelligence|services|iPhone|iPad|"
    r"advertising|subscriptions|automotive|gaming|infrastructure|hyperscaler|"
    r"Azure|AWS|Copilot|GPU|chips?|Mac|Windows|Search|"
    # Banks / financials
    r"net interest income|NII|investment banking|IB fees|markets|trading|"
    r"consumer(?:\s+banking)?|commercial banking|asset(?:\s+and\s+|\s+&\s+)?wealth|"
    r"wealth management|AUM|assets under management|card(?:s| volume)?|"
    r"deposits|loans|credit costs?|provision(?:s)?|CET1|capital ratios?|"
    r"fixed income|equities|advisory|underwriting"
    r")\b[^.]{0,90}?"
    r"(?:up|rose|grew|surged|jumped|climbed|increased|fell|declined|slipped|down)\s+"
    r"(?:by\s+)?(\d+\.?\d*)%",
    re.I,
)

_BANK_METRIC = re.compile(
    r"\b("
    r"net interest income|NII|investment banking(?:\s+fees)?|markets revenue|trading revenue|"
    r"wealth management|assets under management|AUM|CET1(?:\s+ratio)?|Common Equity Tier 1|"
    r"credit costs?|provision for credit|net charge[- ]offs?|deposits|loan growth|"
    r"card sales|consumer banking|commercial banking|IB fees"
    r")\b[^.]{0,120}?"
    r"(?:"
    r"\$[\d,.]+(?:\s*(?:billion|million|trillion|[bBmM]))?"
    r"|\d+\.?\d*\s*%"
    r"|up|rose|grew|fell|declined"
    r")",
    re.I,
)

_GUIDANCE_CHANGE = re.compile(
    r"\b(guidance|outlook|forecast)\b[^.]{0,100}?"
    r"\b(raised|lowered|cut|hiked|increased|reduced|tightened|maintained|reaffirmed|"
    r"below|above|surpassed|missed)\b",
    re.I,
)

_MARGIN_MOVE = re.compile(
    r"\b(gross|operating|net|net interest)\s+margins?\b[^.]{0,50}?"
    r"\b(expanded|widened|improved|contracted|compressed|fell|declined|narrowed|"
    r"rose|increased|was|of)\b",
    re.I,
)

_DEMAND_SIGNAL = re.compile(
    r"\b(demand|orders|bookings|backlog|pipeline|deal activity|client activity)\b[^.]{0,60}?"
    r"\b(strong|weak|robust|soft|record|solid|muted|elevated)\b",
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

# Skip pure EPS/consensus restatements — keep segment revenue and bank metrics.
_HIGHLIGHT_SKIP = re.compile(
    r"\b(per share|consensus|estimate|expected|vs\.?\s*(?:\$|est)|versus (?:\$|est)|"
    r"eps\s+(?:of\s+)?\$|earnings per share)\b",
    re.I,
)

_LLM_HIGHLIGHTS_PER_CYCLE = 3
_llm_highlight_calls = 0
_llm_bullet_batch_calls = 0

EARNINGS_HIGHLIGHTS_MARKER = "---\nHighlights:"
_BULLET_LINE = re.compile(r"^[\s•\-\*–—]+|^\d+[\.\)]\s+")


def reset_earnings_highlight_budget() -> None:
    global _llm_highlight_calls, _llm_bullet_batch_calls
    _llm_highlight_calls = 0
    _llm_bullet_batch_calls = 0


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
        _SEGMENT_GROWTH.search(sentence)
        or _GUIDANCE_CHANGE.search(sentence)
        or _BANK_METRIC.search(sentence)
    ):
        score -= 20
    if _SEGMENT_GROWTH.search(sentence):
        score += 40
    if _BANK_METRIC.search(sentence):
        score += 42
    if _GUIDANCE_CHANGE.search(sentence):
        score += 45
    if _MARGIN_MOVE.search(sentence):
        score += 35
    if _DEMAND_SIGNAL.search(sentence):
        score += 30
    if _QUOTE_CLAUSE.search(sentence):
        score += 25
    for word in (
        "cloud", "data center", "ai", "guidance", "margin", "outlook", "demand",
        "net interest", "nii", "investment banking", "cet1", "trading", "deposits",
        "provision", "aum", "wealth",
    ):
        if word in lower:
            score += 8
    if re.search(r"\d+\.?\d*%", sentence):
        score += 5
    if re.search(r"\$[\d,.]+", sentence):
        score += 4
    return score


def extract_earnings_highlight(text: str) -> str | None:
    """Pull a commentary line — segment, guidance, margins, demand, or management angle."""
    highlights = extract_earnings_highlights(text, max_bullets=1, allow_llm=False)
    return highlights[0] if highlights else None


def _normalize_bullet(line: str, max_len: int = 110) -> str | None:
    text = _BULLET_LINE.sub("", line.strip())
    text = re.sub(r"\s+", " ", text).strip(" \"'")
    if len(text) < 18:
        return None
    if _HIGHLIGHT_SKIP.search(text) and not (
        _SEGMENT_GROWTH.search(text)
        or _GUIDANCE_CHANGE.search(text)
        or _BANK_METRIC.search(text)
        or _MARGIN_MOVE.search(text)
    ):
        return None
    return _trim_highlight(text, max_len=max_len)


def extract_list_bullets_from_html(html: str, max_bullets: int = 12) -> list[str]:
    if not html:
        return []
    items = re.findall(r"<li[^>]*>(.*?)</li>", html, flags=re.I | re.S)
    bullets: list[str] = []
    seen: set[str] = set()
    for raw in items:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        cleaned = _normalize_bullet(text)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        bullets.append(cleaned)
        if len(bullets) >= max_bullets:
            break
    return bullets


def extract_bullets_from_plaintext(text: str, max_bullets: int = 12) -> list[str]:
    bullets: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not (_BULLET_LINE.match(stripped) or stripped.startswith("•")):
            continue
        cleaned = _normalize_bullet(stripped)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        bullets.append(cleaned)
        if len(bullets) >= max_bullets:
            break
    return bullets


def _dedupe_highlights(items: list[str]) -> list[str]:
    """Drop near-duplicates; keep the longer, more complete phrasing."""
    out: list[str] = []
    for item in items:
        key = re.sub(r"\W+", " ", item.lower()).strip()
        if not key:
            continue
        # Prefer longer text when keys substantially overlap
        replaced = False
        for i, existing in enumerate(out):
            ek = re.sub(r"\W+", " ", existing.lower()).strip()
            if key == ek or key in ek or ek in key:
                if len(item) > len(existing):
                    out[i] = item
                replaced = True
                break
        if not replaced:
            out.append(item)
    return out


def extract_earnings_highlights(
    text: str,
    *,
    ticker: str = "",
    max_bullets: int | None = None,
    allow_llm: bool = True,
    html: str = "",
) -> list[str]:
    """Up to N commentary bullets from press release HTML or article text."""
    from config import MAX_EARNINGS_HIGHLIGHTS

    max_bullets = max_bullets if max_bullets is not None else MAX_EARNINGS_HIGHLIGHTS
    if max_bullets <= 0:
        return []

    candidates: list[tuple[int, str]] = []

    for bullet in extract_list_bullets_from_html(html, max_bullets=max_bullets):
        candidates.append((95, bullet))
    for bullet in extract_bullets_from_plaintext(text, max_bullets=max_bullets):
        candidates.append((90, bullet))

    blob = re.sub(r"\s+", " ", text or "").strip()
    if blob:
        for seg in _SEGMENT_GROWTH.finditer(blob):
            segment = seg.group(1).strip()
            pct = seg.group(2)
            candidates.append((92, f"{segment} up {pct}%"))

        for bank in _BANK_METRIC.finditer(blob):
            cleaned = _normalize_bullet(bank.group(0))
            if cleaned and not draft_quality_reason(cleaned):
                candidates.append((93, cleaned))

        for pattern, score in (
            (_GUIDANCE_CHANGE, 90),
            (_MARGIN_MOVE, 85),
            (_DEMAND_SIGNAL, 80),
            (_BEAT_DRIVER, 78),
            (_QUOTE_CLAUSE, 75),
        ):
            match = pattern.search(blob)
            if match:
                phrase = match.group(0).strip()
                if pattern is _BEAT_DRIVER:
                    reason = match.group(1).strip()
                    if len(reason) > 12:
                        phrase = reason
                cleaned = _normalize_bullet(phrase)
                if cleaned and not draft_quality_reason(cleaned):
                    candidates.append((score, cleaned))

        for sentence in _sentences(blob):
            score = _sentence_highlight_score(sentence)
            if score >= 28:
                cleaned = _normalize_bullet(sentence)
                if cleaned and not draft_quality_reason(cleaned):
                    candidates.append((score, cleaned))

    candidates.sort(key=lambda item: item[0], reverse=True)
    highlights = _dedupe_highlights([item[1] for item in candidates])
    highlights = [h for h in highlights if not draft_quality_reason(h)]

    if len(highlights) < max_bullets and allow_llm and len(blob) >= 250 and ticker:
        llm_bullets = llm_earnings_highlights(blob, ticker, max_bullets=max_bullets)
        llm_bullets = [b for b in llm_bullets if not draft_quality_reason(b)]
        highlights = _dedupe_highlights(highlights + llm_bullets)

    return highlights[:max_bullets]


def llm_earnings_highlights(source_text: str, ticker: str, max_bullets: int = 10) -> list[str]:
    """Extract up to N bullets from a long press release via one cheap LLM call."""
    global _llm_bullet_batch_calls
    from config import FILTER_MODEL, FILTER_PROVIDER, LLM_EARNINGS_BULLET_BATCHES_PER_CYCLE
    from pipeline.llm_providers import call_llm, deepseek_configured

    if len(source_text) < 250:
        return []
    if not deepseek_configured() and not ANTHROPIC_API_KEY:
        return []
    if _llm_bullet_batch_calls >= LLM_EARNINGS_BULLET_BATCHES_PER_CYCLE:
        return []

    prompt = (
        f"Ticker: {ticker}\n"
        f"Press release / article excerpt:\n{source_text[:6500]}\n\n"
        f"Return a JSON array of up to {max_bullets} short bullet strings "
        "(max 100 chars each). Facts only from the text: segment growth %, "
        "NII, investment banking, markets/trading, CET1, credit costs, AUM, "
        "margins, guidance ranges, product metrics, demand. "
        "No opinions or predictions. Plain strings only, no numbering."
    )
    system = (
        "You extract factual earnings data points as a JSON string array. "
        "Prefer segment and bank metrics over repeating EPS/revenue headlines. No opinions."
    )
    raw = call_llm(
        system,
        prompt,
        model=FILTER_MODEL,
        provider=FILTER_PROVIDER,
        max_tokens=700,
        retry=False,
        role="filter",
    )
    _llm_bullet_batch_calls += 1
    if not raw:
        return []

    try:
        import json

        start = raw.find("[")
        end = raw.rfind("]")
        if start < 0 or end <= start:
            return []
        parsed = json.loads(raw[start : end + 1])
        if not isinstance(parsed, list):
            return []
    except Exception:
        return []

    bullets: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        cleaned = _normalize_bullet(item, max_len=100)
        if cleaned and not draft_quality_reason(cleaned):
            bullets.append(cleaned)
        if len(bullets) >= max_bullets:
            break
    return bullets


def llm_earnings_highlight(source_text: str, ticker: str) -> str | None:
    """One cheap LLM line when regex can't find commentary in a long article."""
    global _llm_highlight_calls
    from config import FILTER_MODEL, FILTER_PROVIDER
    from pipeline.llm_providers import call_llm, deepseek_configured

    if len(source_text) < 250:
        return None
    if not deepseek_configured() and not ANTHROPIC_API_KEY:
        return None
    if _llm_highlight_calls >= _LLM_HIGHLIGHTS_PER_CYCLE:
        return None

    prompt = (
        f"Ticker: {ticker}\n"
        f"Text:\n{source_text[:2200]}\n\n"
        "Write ONE factual earnings line (max 90 chars): segment metric, guidance "
        "range, margin, or demand figure from the text. No opinions. "
        "Do not repeat EPS/revenue headline numbers. Plain text only."
    )
    system = "You extract one factual earnings data point. No opinions or predictions."
    raw = call_llm(
        system,
        prompt,
        model=FILTER_MODEL,
        provider=FILTER_PROVIDER,
        max_tokens=80,
        retry=False,
        role="filter",
    )
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
    html: str = "",
) -> str | None:
    combined = " ".join(part for part in (source_text, article_text) if part).strip()
    highlights = extract_earnings_highlights(
        combined,
        ticker=ticker,
        max_bullets=1,
        allow_llm=allow_llm,
        html=html,
    )
    if highlights:
        return highlights[0]
    if article_text:
        return extract_earnings_highlight(article_text)
    return None


def earnings_tweet_text(text: str) -> str:
    """Postable X copy. Structured earnings posts are posted in full."""
    return (text or "").strip()


def _earnings_verdict(verb: str, eps_surp: float | None, rev_surp: float | None) -> str:
    eps = eps_surp if eps_surp is not None else 0.0
    rev = rev_surp if rev_surp is not None else 0.0
    if verb == "missed" or (eps < -1 and rev <= 0) or (eps <= 0 and rev < -1):
        worst = min(eps if eps_surp is not None else 0.0, rev if rev_surp is not None else 0.0)
        if worst <= -10:
            return "Wide Miss"
        return "Miss"
    if verb == "matched" or (abs(eps) < 1 and abs(rev) < 1):
        return "In Line"
    best = max(eps if eps_surp is not None else 0.0, rev if rev_surp is not None else 0.0)
    if best >= 8:
        return "Massive Beat"
    if best >= 3:
        return "Solid Beat"
    return "Beat"


def _metric_bullet(label: str, actual: str | None, estimate: str | None, surprise: float | None) -> str | None:
    if not actual or not estimate:
        return None
    if surprise is None:
        return f"• {label}: {actual} vs. {estimate} est."
    if surprise >= 0:
        return f"• {label}: {actual} vs. {estimate} est. ✅ (+{surprise:.0f}%)"
    return f"• {label}: {actual} vs. {estimate} est. ❌ ({surprise:.0f}%)"


def _highlight_emoji(text: str) -> str:
    lower = text.lower()
    pairs = (
        (r"net income|gaap|profit", "📈"),
        (r"underlying eps|eps|per share", "💰"),
        (r"investment banking|trading|markets|ib fees|advisory|underwriting", "🏦"),
        (r"membership|subscriber|streaming|advertising|content", "📺"),
        (r"guidance|outlook|management|businesses|business line", "📊"),
        (r"consumer|credit|card|deposit|loan|charge[- ]?off", "💳"),
        (r"capital|cet1|shareholder|buyback|dividend|return", "💵"),
        (r"cloud|ai|gpu|data[- ]?center|azure|aws", "🤖"),
        (r"nii|net interest|revenue|sales|aum|wealth", "📈"),
    )
    for pattern, emoji in pairs:
        if re.search(pattern, lower):
            return emoji
    return "📌"


def _clean_highlight_for_post(text: str) -> str | None:
    """Prep a highlight bullet for the structured post (allow press-release phrasing)."""
    from pipeline.draft_quality import generic_draft_reason

    text = _BULLET_LINE.sub("", (text or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" \"'")
    if len(text) < 12:
        return None
    # Drop pure EPS/revenue restatements — those live in the Expectations section
    if re.search(r"^(?:eps|revenue|sales)\b", text, re.I) and re.search(
        r"\b(?:vs\.?|versus|est\.?|consensus|estimate)\b", text, re.I
    ):
        return None
    gf = generic_draft_reason(text)
    if gf and ("generic filler" in gf or gf in ("empty", "weak opener")):
        return None
    return _trim_highlight(text, max_len=120)


def _company_hashtag(company_name: str | None, ticker: str) -> str | None:
    if not company_name:
        return None
    short = re.sub(
        r"\s+(Inc\.?|Corp\.?|Co\.?|& Co\.?|Corporation|Company|plc|Ltd\.?|N\.?A\.?)\.?.*$",
        "",
        company_name,
        flags=re.I,
    ).strip()
    # JPMorgan Chase -> JPMorgan
    short = short.split()[0] if short else ""
    short = re.sub(r"[^A-Za-z0-9]", "", short)
    if not short or short.upper() == ticker.upper():
        return None
    return f"#{short}"


_BANK_TICKERS = frozenset({
    "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC", "SCHW", "BK", "STT", "COF",
})


def format_earnings_draft(
    ticker: str,
    verb: str,
    facts: EarningsFacts,
    *,
    highlights: list[str] | None = None,
    year: int | None = None,
    company_name: str | None = None,
) -> str:
    """Structured earnings post matching the queue / X layout users expect."""
    from pipeline.draft_quality import generic_draft_reason as _generic

    ticker = (ticker or "").upper()
    name = (company_name or "").strip() or ticker
    short_name = re.sub(
        r"\s+(Inc\.?|Corp\.?|Co\.?|& Co\.?|Corporation|Company|plc|Ltd\.?).*$",
        "",
        name,
        flags=re.I,
    ).strip() or name

    q_raw = (facts.quarter or "").upper().replace("QUARTER", "").strip()
    if q_raw and not q_raw.startswith("Q"):
        q_raw = f"Q{q_raw}" if q_raw.isdigit() else q_raw
    period = q_raw
    if year:
        period = f"{q_raw} {year}".strip() if q_raw else str(year)

    eps_surp = (
        _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)
        if facts.eps_actual and facts.eps_estimate
        else None
    )
    rev_surp = (
        _revenue_surprise_pct(facts.revenue_actual, facts.revenue_estimate)
        if facts.revenue_actual and facts.revenue_estimate
        else None
    )
    verdict = _earnings_verdict(verb, eps_surp, rev_surp)

    title = f"${ticker}"
    if period:
        title += f" {period}"
    title += f" Earnings: {verdict}"

    sections: list[str] = [title, "", "Analyst Expectations vs. Actual"]
    eps_line = _metric_bullet("EPS", facts.eps_actual, facts.eps_estimate, eps_surp)
    rev_line = _metric_bullet("Revenue", facts.revenue_actual, facts.revenue_estimate, rev_surp)
    if eps_line:
        sections.append(eps_line)
    if rev_line:
        sections.append(rev_line)
    if not eps_line and not rev_line:
        if facts.eps_actual:
            sections.append(f"• EPS: {facts.eps_actual}")
        if facts.revenue_actual:
            sections.append(f"• Revenue: {facts.revenue_actual}")

    cleaned_highlights: list[str] = []
    seen: set[str] = set()
    for raw in highlights or []:
        cleaned = _clean_highlight_for_post(raw)
        if not cleaned:
            continue
        # Extra pass: skip only hard generic filler
        gf = _generic(cleaned)
        if gf and ("generic filler" in gf or gf == "empty"):
            continue
        key = re.sub(r"\W+", " ", cleaned.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        cleaned_highlights.append(cleaned)
        if len(cleaned_highlights) >= 8:
            break

    if cleaned_highlights:
        sections.append("")
        sections.append("Key Highlights")
        for item in cleaned_highlights:
            sections.append(f"• {_highlight_emoji(item)} {item}")

    sections.append("")
    # Closing — match requested tone for beats; keep misses factual
    if verb == "missed" or "Miss" in verdict:
        closing = f"{ticker} missed estimates this quarter as results came in below consensus."
    elif cleaned_highlights:
        closing = (
            f"Another quarter showing why {ticker} remains the industry's benchmark, "
            f"with strong performance across nearly every business line."
        )
    else:
        closing = f"Another quarter of strength for {ticker} versus Wall Street estimates."
    sections.append(closing)

    tags = [f"#{ticker}"]
    company_tag = _company_hashtag(company_name, ticker)
    if company_tag:
        tags.append(company_tag)
    tags.append("#Earnings")
    if ticker in _BANK_TICKERS:
        tags.extend(["#BankStocks", "#Investing"])
    else:
        tags.extend(["#Stocks", "#Investing"])
    sections.append("")
    sections.append(" ".join(tags))

    return "\n".join(sections).strip()


def fetch_earnings_news_context(symbol: str, days_back: int = 2) -> str:
    """Latest Finnhub company-news blurbs for earnings commentary."""
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


def fetch_earnings_article_text(symbol: str, days_back: int = 2) -> str:
    """Fetch full text from the latest earnings-related Finnhub company news article."""
    from pipeline.enrich import fetch_article_text
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

    for item in data[:12]:
        headline = (item.get("headline") or "").strip()
        summary = (item.get("summary") or "").strip()
        url = (item.get("url") or "").strip()
        if not url or not headline:
            continue
        if not _EARNINGS_NEWS.search(f"{headline} {summary}"):
            continue
        article = fetch_article_text(url)
        if article and len(article) > 200:
            return article[:4000]
    return ""


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


def _revenue_surprise_pct(actual: str, estimate: str) -> float | None:
    try:
        def _parse_money(s: str) -> float:
            s = s.replace("$", "").replace(",", "").strip()
            mult = 1.0
            if s.endswith(("B", "b")):
                mult = 1_000_000_000
                s = s[:-1]
            elif s.endswith(("M", "m")):
                mult = 1_000_000
                s = s[:-1]
            elif s.endswith(("K", "k")):
                mult = 1_000
                s = s[:-1]
            return float(s) * mult

        a = _parse_money(actual)
        e = _parse_money(estimate)
        if e == 0:
            return None
        return (a - e) / abs(e) * 100
    except (ValueError, AttributeError):
        return None


def build_earnings_insight(ticker: str, verb: str, facts: EarningsFacts) -> str | None:
    """Company-specific line — surprise % when available (works for modest bank beats)."""
    eps_surp = None
    if facts.eps_actual and facts.eps_estimate:
        eps_surp = _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)

    rev_surp = None
    if facts.revenue_actual and facts.revenue_estimate:
        rev_surp = _revenue_surprise_pct(facts.revenue_actual, facts.revenue_estimate)

    if verb == "beat":
        if eps_surp is not None and rev_surp is not None:
            if eps_surp >= 25 and rev_surp < 3:
                return f"EPS beat consensus by {eps_surp:.0f}%; revenue beat by {rev_surp:.0f}%"
            if rev_surp >= 5 and eps_surp >= 5:
                return f"Revenue beat {rev_surp:.0f}% and EPS beat {eps_surp:.0f}%"
            if rev_surp < -1:
                return f"EPS beat {eps_surp:.0f}% but revenue missed by {abs(rev_surp):.0f}%"
            if abs(eps_surp) >= 1 or abs(rev_surp) >= 1:
                return f"EPS {eps_surp:+.0f}% and revenue {rev_surp:+.0f}% vs est"
        if eps_surp is not None and abs(eps_surp) >= 1:
            return f"EPS beat consensus by {eps_surp:.0f}%"
        if rev_surp is not None and rev_surp >= 1:
            return f"Revenue beat consensus by {rev_surp:.0f}%"
        if facts.yoy_pct:
            return f"Revenue up {facts.yoy_pct}% YoY"
        return None

    if verb == "missed":
        if eps_surp is not None and rev_surp is not None:
            return f"EPS {eps_surp:+.0f}% and revenue {rev_surp:+.0f}% vs est"
        if eps_surp is not None:
            return f"EPS {abs(eps_surp):.0f}% below consensus"
        if rev_surp is not None and rev_surp < 0:
            return f"Revenue missed by {abs(rev_surp):.0f}% vs est"
        return None

    if eps_surp is not None and rev_surp is not None:
        return f"EPS {eps_surp:+.0f}% and revenue {rev_surp:+.0f}% vs est"
    if facts.yoy_pct:
        return f"Revenue growth +{facts.yoy_pct}% YoY"
    return None


def build_earnings_lines(
    ticker: str,
    verb: str,
    facts: EarningsFacts,
    *,
    source_text: str = "",
    article_text: str = "",
    allow_llm: bool = True,
    html: str = "",
    max_highlights: int | None = None,
) -> tuple[str, str, str, list[str]] | None:
    from config import MAX_EARNINGS_HIGHLIGHTS

    max_highlights = max_highlights if max_highlights is not None else MAX_EARNINGS_HIGHLIGHTS
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

    highlights = extract_earnings_highlights(
        " ".join(part for part in (source_text, article_text) if part),
        ticker=ticker,
        max_bullets=max_highlights,
        allow_llm=allow_llm,
        html=html,
    )

    if facts.revenue_actual and facts.revenue_estimate and "revenue" not in line1.lower():
        line2 = f"Revenue {facts.revenue_actual} vs {facts.revenue_estimate} est"
    elif facts.yoy_pct:
        line2 = f"Revenue growth +{facts.yoy_pct}% year over year"
    elif facts.eps_actual and facts.eps_estimate:
        surprise = _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)
        if surprise is not None:
            word = "cleared" if surprise >= 0 else "missed"
            line2 = f"EPS {word} consensus by {abs(surprise):.0f}%"
        else:
            line2 = f"EPS {facts.eps_actual} vs {facts.eps_estimate} est"
    elif facts.revenue_actual:
        line2 = f"Revenue {facts.revenue_actual}"
    else:
        line2 = f"{ticker} reported {q}results"

    insight = build_earnings_insight(ticker, verb, facts)
    line3 = None
    for candidate in (
        highlights[0] if highlights else None,
        highlights[1] if len(highlights) > 1 else None,
        insight,
    ):
        if not candidate:
            continue
        low = candidate.lower()
        if low in line1.lower() or low in line2.lower():
            continue
        if draft_quality_reason(candidate):
            continue
        line3 = candidate
        break

    if not line3:
        if insight and insight.lower() not in line1.lower() and insight.lower() not in line2.lower():
            line3 = insight
        elif facts.yoy_pct and f"{facts.yoy_pct}%" not in line2:
            line3 = f"Revenue up {facts.yoy_pct}% YoY"
        else:
            # Never duplicate line2 revenue/EPS — always return a third distinct line
            line3 = f"{ticker} {verb} {q}results".strip()

    return line1, line2, line3, highlights
