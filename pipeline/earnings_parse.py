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
    highlights = extract_earnings_highlights(text, max_bullets=1, allow_llm=False)
    return highlights[0] if highlights else None


def _normalize_bullet(line: str, max_len: int = 110) -> str | None:
    text = _BULLET_LINE.sub("", line.strip())
    text = re.sub(r"\s+", " ", text).strip(" \"'")
    if len(text) < 18:
        return None
    if _HIGHLIGHT_SKIP.search(text) and not (
        _SEGMENT_GROWTH.search(text) or _GUIDANCE_CHANGE.search(text)
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
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = re.sub(r"\W+", " ", item.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
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
        seg = _SEGMENT_GROWTH.search(blob)
        if seg:
            segment = seg.group(1).strip()
            pct = seg.group(2)
            candidates.append((92, f"{segment} revenue up {pct}%"))

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
                        phrase = f"Beat driven by {reason}"
                cleaned = _normalize_bullet(phrase)
                if cleaned:
                    candidates.append((score, cleaned))

        for sentence in _sentences(blob):
            score = _sentence_highlight_score(sentence)
            if score >= 30:
                cleaned = _normalize_bullet(sentence)
                if cleaned:
                    candidates.append((score, cleaned))

    candidates.sort(key=lambda item: item[0], reverse=True)
    highlights = _dedupe_highlights([item[1] for item in candidates])

    if len(highlights) < max_bullets and allow_llm and len(blob) >= 250 and ticker:
        llm_bullets = llm_earnings_highlights(blob, ticker, max_bullets=max_bullets)
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
        f"Press release excerpt:\n{source_text[:5000]}\n\n"
        f"Return a JSON array of up to {max_bullets} short bullet strings "
        "(max 100 chars each) with segment growth, margins, guidance, demand, "
        "product wins, and management outlook. "
        "Do NOT repeat the headline EPS/revenue beat/miss line. "
        "Plain strings only, no numbering."
    )
    system = "You extract earnings press-release highlights as a JSON string array."
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
        if cleaned:
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
        "Write ONE earnings commentary line (max 90 chars) about segment strength, "
        "guidance, margins, demand, or management outlook. "
        "Do not repeat EPS/revenue headline numbers. Plain text only."
    )
    system = "You extract one sharp earnings highlight for a stock post."
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
    """Postable X copy — strip reference highlights below the marker."""
    if EARNINGS_HIGHLIGHTS_MARKER in text:
        return text.split(EARNINGS_HIGHLIGHTS_MARKER, 1)[0].strip()
    return text.strip()


def format_earnings_draft(
    line1: str,
    line2: str,
    line3: str,
    *,
    highlights: list[str] | None = None,
    ticker: str | None = None,
) -> str:
    """Three-line post hook plus optional bullet reference section."""
    body = f"{line1}\n{line2}\n{line3}".strip()
    if ticker:
        body = f"{body}\n\n${ticker.upper()}"
    if highlights:
        bullet_lines = "\n".join(f"• {item}" for item in highlights[:10])
        body = f"{body}\n\n{EARNINGS_HIGHLIGHTS_MARKER}\n{bullet_lines}"
    return body.strip()


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


def build_earnings_insight(ticker: str, verb: str, facts: EarningsFacts) -> str:
    """Company-specific line 3 — derived from numbers, not a shared template."""
    eps_surp = None
    if facts.eps_actual and facts.eps_estimate:
        eps_surp = _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)

    rev_surp = None
    if facts.revenue_actual and facts.revenue_estimate:
        rev_surp = _revenue_surprise_pct(facts.revenue_actual, facts.revenue_estimate)

    if verb == "beat":
        if eps_surp is not None and rev_surp is not None:
            if eps_surp >= 25 and rev_surp < 3:
                return (
                    f"EPS crushed estimates but revenue only cleared by {rev_surp:.0f}% — "
                    "margins or one-offs likely drove the print"
                )
            if rev_surp >= 5 and eps_surp >= 5:
                return (
                    f"Revenue beat {rev_surp:.0f}% too — broad-based quarter, not a skinny top-line miss"
                )
            if rev_surp < -1:
                return "EPS beat despite revenue missing — margin story needs to hold on the call"
        if eps_surp is not None and eps_surp >= 80:
            return f"Street was {eps_surp:.0f}% too low on EPS — models need a full reset after the call"
        if eps_surp is not None and eps_surp >= 20:
            return f"{eps_surp:.0f}% EPS surprise — sets a high bar for forward guidance tonight"
        if rev_surp is not None and rev_surp >= 8:
            return f"Revenue beat {rev_surp:.0f}% vs est — top-line momentum backs the EPS upside"
        if facts.yoy_pct:
            return f"Revenue up {facts.yoy_pct}% YoY — growth vs guide is the key read from here"
        return "Segment mix and full-year outlook on the call decide if the beat sticks"

    if verb == "missed":
        if eps_surp is not None and rev_surp is not None and rev_surp < -2:
            return f"Revenue missed {abs(rev_surp):.0f}% too — demand softness may be the bigger story"
        if eps_surp is not None and eps_surp <= -15:
            return f"EPS {abs(eps_surp):.0f}% below street — watch for guide cuts and margin commentary"
        return "Miss opens downside risk — guidance and segment detail on the call matter most"

    if facts.yoy_pct:
        return f"Revenue growth ran +{facts.yoy_pct}% YoY — compare that pace to management's outlook"
    return "Print is in — segment breakdown and guide frame the trade from here"


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
    highlight = highlights[0] if highlights else None

    if facts.revenue_actual and facts.revenue_estimate and "revenue" not in line1.lower():
        line2 = f"Revenue {facts.revenue_actual} vs {facts.revenue_estimate} est"
    elif facts.yoy_pct:
        line2 = f"Revenue growth +{facts.yoy_pct}% year over year"
    elif facts.eps_actual and facts.eps_estimate:
        surprise = _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)
        if surprise is not None:
            word = "cleared" if surprise >= 0 else "missed"
            line2 = f"EPS {word} the street by {abs(surprise):.0f}%"
        else:
            line2 = "Headline numbers vs the street"
    else:
        line2 = "Full segment breakdown on the call"

    if highlight and highlight.lower() not in line2.lower():
        line3 = highlight
    else:
        line3 = build_earnings_insight(ticker, verb, facts)

    return line1, line2, line3, highlights
