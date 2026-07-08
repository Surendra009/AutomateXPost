"""Extract EPS/revenue figures from earnings headlines and summaries."""

from __future__ import annotations

import re
from dataclasses import dataclass

QUARTER = re.compile(r"\b(q[1-4])\b", re.I)

# actual vs estimate — many wire phrasings
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

    def has_comparison(self) -> bool:
        return bool(
            (self.eps_actual and self.eps_estimate)
            or (self.revenue_actual and self.revenue_estimate)
        )


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


def build_earnings_lines(
    ticker: str,
    verb: str,
    facts: EarningsFacts,
) -> tuple[str, str] | None:
    """Build hook + detail lines. None when there are no concrete figures."""
    if not facts.has_numbers():
        return None

    q = f"{facts.quarter} " if facts.quarter else ""

    if facts.eps_actual and facts.eps_estimate:
        line1 = f"{ticker} {verb} {q}EPS {facts.eps_actual} vs {facts.eps_estimate} est"
    elif facts.eps_actual:
        line1 = f"{ticker} reported {q}EPS {facts.eps_actual}"
    elif facts.revenue_actual and facts.revenue_estimate:
        line1 = f"{ticker} {verb} {q}revenue — {facts.revenue_actual} vs {facts.revenue_estimate} est"
    elif facts.revenue_actual:
        line1 = f"{ticker} reported {q}revenue {facts.revenue_actual}"
    else:
        return None

    line2_parts: list[str] = []
    if facts.revenue_actual and facts.revenue_estimate and facts.eps_actual:
        line2_parts.append(f"Revenue {facts.revenue_actual} vs {facts.revenue_estimate} est")
    elif facts.revenue_actual and facts.revenue_estimate:
        line2_parts.append(f"Sales {facts.revenue_actual} vs {facts.revenue_estimate} est")
    elif facts.revenue_actual and not facts.eps_estimate:
        line2_parts.append(f"Revenue came in at {facts.revenue_actual}")
    elif facts.yoy_pct and facts.revenue_actual:
        line2_parts.append(f"Revenue {facts.revenue_actual}, up {facts.yoy_pct}% year over year")
    elif facts.yoy_pct:
        line2_parts.append(f"Up {facts.yoy_pct}% year over year")
    elif facts.eps_actual and facts.eps_estimate:
        surprise = _eps_surprise_pct(facts.eps_actual, facts.eps_estimate)
        if surprise is not None:
            word = "above" if surprise >= 0 else "below"
            line2_parts.append(f"{abs(surprise):.0f}% {word} the EPS estimate")
    elif verb == "beat":
        line2_parts.append("Beat on the headline number — watch guidance on the call")
    elif verb == "missed":
        line2_parts.append("Miss likely pressures the stock near term")
    else:
        line2_parts.append("Results roughly in line with the street")

    line2 = line2_parts[0] if line2_parts else ""
    if not line2:
        return None
    return line1, line2
