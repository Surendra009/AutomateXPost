"""Normalize post text — one cashtag footer, no duplicate tickers."""

from __future__ import annotations

import re

CASHTAG_SYM = re.compile(r"\$([A-Z]{1,5})\b")


def dedupe_tickers(tickers: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in tickers:
        sym = raw.upper().strip().lstrip("$")
        if not sym or not re.fullmatch(r"[A-Z]{1,5}", sym):
            continue
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def parse_ticker_field(tickers: str | None) -> list[str]:
    if not tickers:
        return []
    return dedupe_tickers(tickers.split(","))


def extract_cashtags(text: str) -> list[str]:
    return dedupe_tickers(m.group(1) for m in CASHTAG_SYM.finditer(text))


def is_cashtag_line(line: str) -> bool:
    tokens = line.strip().split()
    return bool(tokens) and all(re.fullmatch(r"\$[A-Z]{1,5}", t) for t in tokens)


def strip_cashtags_from_body(line: str) -> str:
    """Move cashtags out of body lines — keep bare symbol so the hook still reads."""

    def repl(match: re.Match[str]) -> str:
        return match.group(1)

    cleaned = CASHTAG_SYM.sub(repl, line)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"^[\s\-–—,:;]+", "", cleaned)
    cleaned = re.sub(r"[\s\-–—,:;]+$", "", cleaned)
    return cleaned


def normalize_post_text(text: str, tickers: list[str] | str | None = None) -> str:
    """Ensure a single deduped cashtag line at the end; no inline $TICKER duplicates."""
    if isinstance(tickers, str):
        ticker_list = parse_ticker_field(tickers)
    else:
        ticker_list = dedupe_tickers(tickers or [])

    text = text.replace("\\n", "\n").strip()
    merged = dedupe_tickers(ticker_list + extract_cashtags(text))

    body_lines: list[str] = []
    for ln in (ln.strip() for ln in text.split("\n")):
        if not ln:
            if body_lines and body_lines[-1] != "":
                body_lines.append("")
            continue
        if is_cashtag_line(ln):
            continue
        stripped = strip_cashtags_from_body(ln)
        if stripped:
            body_lines.append(stripped)

    while body_lines and body_lines[-1] == "":
        body_lines.pop()

    if merged:
        ticker_line = " ".join(f"${sym}" for sym in merged)
        if not body_lines or body_lines[-1] != ticker_line:
            body_lines.append("")
            body_lines.append(ticker_line)

    return "\n".join(body_lines).strip()
