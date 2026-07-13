"""Reject vague, filler-heavy draft copy — require concrete facts."""

from __future__ import annotations

import re

# Phrases that sound like wire filler, not informed commentary
GENERIC_DRAFT_PHRASES = re.compile(
    r"\b("
    r"investors?\s+(?:are\s+)?(?:worried|watching|awaiting|eyeing|nervous|on edge)|"
    r"is this going to be big|could this be big|going to be big|"
    r"big question|key question|remains to be seen|all eyes on|"
    r"what to watch|here'?s what(?:\s+to|\s+you)|"
    r"matter(?:s)? most|frame the trade|opens downside risk|"
    r"sets the next move|sets a high bar for forward guidance tonight|"
    r"on the call decide|decide if the beat sticks|"
    r"full segment breakdown on the call|segment detail on the call|"
    r"guidance and margins set the next move|"
    r"watch segment commentary|watch for guide|watch oil majors|"
    r"forward outlook reset for the stock|m&a headline|"
    r"print is in|headline numbers vs the street|"
    r"traders?\s+(?:watch|await)|markets?\s+(?:watch|await|digest)|"
    r"wall street (?:watches|waits|is watching)|"
    r"sparks?\s+concerns?|raises?\s+questions?|amid concerns?|"
    r"under pressure|in focus|sentiment shift|"
    r"moves rates and risk assets|repriced quickly|"
    r"another step in the (?:ai )?product war|raises the bar in the model race|"
    r"geopolitical risk shifts|watch .+ for follow-through|"
    r"macro data moves|segment mix and full-year outlook"
    r")\b",
    re.I,
)

_OPENER_BAN = re.compile(
    r"^(?:investors?|markets?|traders?|wall street|stocks?)\b",
    re.I,
)

_HAS_CONCRETE = re.compile(
    r"(?:"
    r"\$[\d,.]+[BMK]?|"
    r"\d+\.?\d*\s*%|"
    r"\b\d+(?:\.\d+)?\s*(?:billion|million|bn|mn|bps|basis points)\b|"
    r"\bQ[1-4]\b|"
    r"\bFY\s?20\d{2}\b"
    r")",
    re.I,
)

# Proper product / segment names count as concrete when paired with a verb
_HAS_NAMED_DETAIL = re.compile(
    r"\b("
    r"Azure|AWS|Copilot|ChatGPT|iPhone|iPad|Mac|Windows|"
    r"data[- ]?center|cloud|GPU|hyperscaler|"
    r"FOMC|CPI|PPI|NFP|GDP"
    r")\b[^.]{0,40}\b(up|down|rose|fell|grew|launched|cut|hiked|beat|missed)\b",
    re.I,
)


def generic_draft_reason(text: str) -> str | None:
    """Return a short reason when copy is too vague, else None."""
    if not text or not text.strip():
        return "empty"

    match = GENERIC_DRAFT_PHRASES.search(text)
    if match:
        return f"generic filler: {match.group(0)[:40]}"

    lines = [ln.strip() for ln in text.split("\n") if ln.strip() and not _is_ticker_line(ln)]
    if not lines:
        return "no content lines"

    if _OPENER_BAN.match(lines[0]):
        return "weak opener"

    body = " ".join(lines[:-1] if _is_ticker_line(lines[-1]) else lines)
    if not has_concrete_detail(body):
        return "no concrete numbers or specifics"

    return None


def has_concrete_detail(text: str) -> bool:
    if _HAS_CONCRETE.search(text):
        return True
    if _HAS_NAMED_DETAIL.search(text):
        return True
    return False


def _is_ticker_line(line: str) -> bool:
    tokens = line.strip().split()
    return bool(tokens) and all(re.fullmatch(r"\$[A-Z]{1,5}", t) for t in tokens)


def passes_draft_quality(text: str, *, require_concrete: bool = True) -> bool:
    if generic_draft_reason(text):
        return False
    if require_concrete and not has_concrete_detail(text):
        return False
    return True
