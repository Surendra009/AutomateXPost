"""Finnhub economic calendar — macro prints and previews with zero LLM."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from config import MAX_MACRO_DRAFTS_PER_CYCLE
from logging_config import setup_logging
from pipeline.finnhub_api import finnhub_get, get_finnhub_key
from pipeline.structured_common import content_hash, save_structured_draft

logger = setup_logging()

MACRO_SOURCE = "Finnhub Macro"

MACRO_EVENT_LABELS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"consumer price index|\bcpi\b", re.I), "CPI"),
    (re.compile(r"producer price index|\bppi\b", re.I), "PPI"),
    (re.compile(r"non[- ]?farm payroll|nonfarm payroll|\bnfp\b", re.I), "Nonfarm payrolls"),
    (re.compile(r"unemployment rate", re.I), "Unemployment"),
    (re.compile(r"\bgdp\b|gross domestic product", re.I), "GDP"),
    (re.compile(r"\bfomc\b|fed interest rate|interest rate decision", re.I), "Fed"),
    (re.compile(r"retail sales", re.I), "Retail sales"),
    (re.compile(r"initial jobless claims|jobless claims", re.I), "Jobless claims"),
]

MACRO_TAKEAWAY = {
    "CPI": "Inflation print shifts rate-cut expectations",
    "PPI": "Producer prices feed into the inflation outlook",
    "Nonfarm payrolls": "Labor strength affects Fed and rate path",
    "Unemployment": "Labor market signal for the Fed",
    "GDP": "Growth read shapes recession vs soft-landing odds",
    "Fed": "Rates path repriced across stocks and bonds",
    "Retail sales": "Consumer demand signal for growth and rates",
    "Jobless claims": "Weekly labor pulse for the Fed",
}

_US_COUNTRIES = {"US", "USA", "UNITED STATES"}


def _macro_label(event_name: str) -> str | None:
    for pattern, label in MACRO_EVENT_LABELS:
        if pattern.search(event_name):
            return label
    return None


def _is_us_event(event: dict[str, Any]) -> bool:
    country = (event.get("country") or "").strip().upper()
    return not country or country in _US_COUNTRIES


def _impact_ok(event: dict[str, Any]) -> bool:
    impact = (event.get("impact") or "").lower()
    return impact in ("high", "medium", "med")


def _fmt_value(value: Any, unit: str | None) -> str | None:
    if value is None:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return str(value)
    unit_s = (unit or "").strip()
    if unit_s == "%":
        return f"{val:g}%"
    if unit_s.lower() in ("k", "thousands"):
        return f"{val:,.0f}K"
    if unit_s.lower() in ("m", "million"):
        return f"{val:,.1f}M"
    if unit_s:
        return f"{val:g} {unit_s}"
    return f"{val:g}"


def _surprise_word(actual: float, estimate: float, tol: float = 0.02) -> str:
    diff = actual - estimate
    if abs(diff) <= tol:
        return "in-line"
    return "beat" if diff > 0 else "miss"


def fetch_economic_calendar(from_date: str, to_date: str) -> list[dict[str, Any]]:
    if not get_finnhub_key():
        return []
    data, err = finnhub_get("calendar/economic", {"from": from_date, "to": to_date})
    if err:
        logger.warning("Finnhub economic calendar: %s", err)
        return []
    return (data or {}).get("economicCalendar") or []


def _build_result(event: dict[str, Any], label: str) -> tuple[str, str, str, str] | None:
    actual = event.get("actual")
    estimate = event.get("estimate")
    if actual is None or estimate is None:
        return None

    try:
        actual_f = float(actual)
        estimate_f = float(estimate)
    except (TypeError, ValueError):
        return None

    actual_s = _fmt_value(actual, event.get("unit"))
    est_s = _fmt_value(estimate, event.get("unit"))
    if not actual_s or not est_s:
        return None

    word = _surprise_word(actual_f, estimate_f)
    title = f"{label} {actual_s} vs {est_s} est ({word})"
    summary = title

    line1 = f"{label} came in at {actual_s} vs {est_s} expected"
    line2 = MACRO_TAKEAWAY.get(label, "Macro data moves rates and risk assets")
    draft = f"{line1}\n{line2}\n\n$SPY"
    impact = "high" if word in ("beat", "miss") else "med"
    return title, summary, draft, impact


def _build_preview(event: dict[str, Any], label: str) -> tuple[str, str, str] | None:
    estimate = event.get("estimate")
    est_s = _fmt_value(estimate, event.get("unit")) if estimate is not None else None
    time_s = (event.get("time") or "").strip()

    if time_s:
        line1 = f"{label} due at {time_s} ET"
    else:
        line1 = f"{label} release due today"

    if est_s:
        line2 = f"Consensus est {est_s}"
    else:
        line2 = MACRO_TAKEAWAY.get(label, "Macro data moves rates and risk assets")

    title = f"{line1} — {line2}" if est_s else line1
    summary = title
    draft = f"{line1}\n{line2}\n\n$SPY"
    return title, summary, draft


def process_macro_calendar() -> tuple[int, int]:
    """Create macro drafts from Finnhub economic calendar. Returns (ingested, drafts)."""
    if not get_finnhub_key():
        return 0, 0

    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    events = fetch_economic_calendar(yesterday.isoformat(), today.isoformat())

    ingested = 0
    drafts_created = 0

    for event in events:
        if drafts_created >= MAX_MACRO_DRAFTS_PER_CYCLE:
            break
        if not _is_us_event(event) or not _impact_ok(event):
            continue

        event_name = event.get("event") or ""
        label = _macro_label(event_name)
        if not label:
            continue

        event_time = (event.get("time") or "").strip()
        date_key = today.isoformat()
        chash = content_hash(MACRO_SOURCE, label, date_key, event_name, str(event.get("actual")))

        if event.get("actual") is not None and event.get("estimate") is not None:
            built = _build_result(event, label)
            if not built:
                continue
            title, summary, draft_text, impact = built
            fmt = "BREAKING"
            confidence = 0.94
            kind = "result"
        else:
            # Preview only for today's not-yet-released prints
            if event.get("actual") is not None:
                continue
            built = _build_preview(event, label)
            if not built:
                continue
            title, summary, draft_text = built
            impact = "med"
            fmt = "CONTEXT"
            confidence = 0.86
            kind = "preview"

        chash = content_hash(MACRO_SOURCE, label, date_key, kind, event_name, event_time)
        url = f"https://finnhub.io/macro/{label.lower().replace(' ', '-')}/{date_key}"

        if save_structured_draft(
            source=MACRO_SOURCE,
            url=url,
            title=title,
            summary=summary,
            draft_text=draft_text,
            tickers="SPY",
            category="macro",
            impact=impact,
            fmt=fmt,
            confidence=confidence,
            chash=chash,
        ):
            ingested += 1
            drafts_created += 1

    if drafts_created:
        logger.info("Macro calendar: created %d drafts", drafts_created)
    return ingested, drafts_created
