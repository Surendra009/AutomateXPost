"""Detect generic, wire-copy, or boilerplate draft text."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

# Stock phrases reused across template paths — reject or fall through to LLM.
BOILERPLATE_PHRASES = re.compile(
    r"\b("
    r"another step in the ai product war|"
    r"forward outlook reset for the stock|"
    r"updated guidance|"
    r"m&a headline|"
    r"filed q results with the sec|"
    r"beat on the headline number|"
    r"watch guidance on the call|"
    r"results roughly in line with the street|"
    r"macro data moves rates and risk assets|"
    r"inflation print shifts rate-cut expectations|"
    r"producer prices feed into the inflation outlook|"
    r"labor strength affects fed and rate path|"
    r"labor market signal for the fed|"
    r"growth read shapes recession vs soft-landing odds|"
    r"rates path repriced across stocks and bonds|"
    r"geopolitical risk shifts defensive and energy trades|"
    r"watch oil majors and energy etfs for follow-through|"
    r"miss likely pressures the stock near term|"
    r"revenue came in at|"
    r"came in at \d|"
    r"printed at \d"
    r")\b",
    re.I,
)

WIRE_OPENERS = re.compile(
    r"^(?:"
    r".+\s+(?:said|says|reported|reports|announced|announces)\s+that|"
    r"(?:breaking|update):\s*"
    r")",
    re.I,
)

PASSIVE_WIRE = re.compile(
    r"\b("
    r"according to|in a statement|sources said|people familiar|"
    r"the company said|executives said|analysts said"
    r")\b",
    re.I,
)


def _body_lines(text: str) -> list[str]:
    lines: list[str] = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        tokens = ln.split()
        if tokens and all(re.fullmatch(r"\$[A-Z]{1,5}", t) for t in tokens):
            continue
        lines.append(ln)
    return lines


def is_headline_echo(text: str, title: str, threshold: float = 75) -> bool:
    flat = " ".join(text.lower().split())[:120]
    normalized_title = " ".join(title.lower().split())
    return fuzz.ratio(flat, normalized_title) > threshold


def line_echoes_title(line: str, title: str, threshold: float = 72) -> bool:
    a = " ".join(line.lower().split())[:100]
    b = " ".join(title.lower().split())[:100]
    if fuzz.ratio(a, b) > threshold:
        return True
    title_start = " ".join(title.lower().split()[:7])
    line_start = " ".join(line.lower().split()[:7])
    return bool(title_start) and fuzz.ratio(title_start, line_start) > 82


def is_too_generic(text: str, title: str) -> bool:
    """True when a draft reads like recycled wire copy or template filler."""
    if not text or not title:
        return True

    if is_headline_echo(text, title):
        return True

    if BOILERPLATE_PHRASES.search(text):
        return True

    body = _body_lines(text)
    if not body:
        return True

    line1 = body[0]
    if line_echoes_title(line1, title):
        return True

    if WIRE_OPENERS.match(line1) or PASSIVE_WIRE.search(text):
        return True

    # Long first line with no numbers often means truncated headline
    if len(line1) > 55 and len(re.findall(r"\d", line1)) == 0:
        if fuzz.partial_ratio(line1.lower(), title.lower()) > 85:
            return True

    return False
