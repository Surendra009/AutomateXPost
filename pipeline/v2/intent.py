"""Build the Intent Board — what this cycle must try to cover.

Seeds: Finnhub earnings + macro/Fed calendars, standing Fed speak,
watchlist company material, AI catalyst, and politics/policy themes.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from config import EARNINGS_PREVIEW_DAYS_FORWARD, MAX_WEB_TICKERS_PER_CYCLE, MAX_WEB_TOPICS_PER_CYCLE
from database import get_setting
from logging_config import setup_logging
from pipeline.earnings import _has_actual, fetch_earnings_calendar
from pipeline.earnings_calendar import dedupe_calendar_events
from pipeline.earnings_freshness import coerce_quarter_year, is_current_reporting_period
from pipeline.finnhub_api import get_finnhub_key
from pipeline.macro_calendar import (
    _impact_ok,
    _is_us_event,
    _macro_label,
    fetch_economic_calendar,
)
from pipeline.v2.types import Intent, IntentKind
from pipeline.watchlist_scope import in_watchlist, normalized_watchlist

logger = setup_logging()

_MAX_EARNINGS_INTENTS = 12
_MAX_MACRO_INTENTS = 8
_MAX_COMPANY_INTENTS = 6

_AI_TOPIC = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|openai|anthropic|llm|chatgpt|gemini|gpu|semiconductor)\b",
    re.I,
)
_POLITICS_TOPIC = re.compile(
    r"\b(politic|election|tariff|sanction|congress|senate|white house|geopolit|war|nato|trade war)\b",
    re.I,
)


def build_intent_board() -> list[Intent]:
    """Return must-cover intents for this cycle."""
    today = datetime.utcnow().date()
    intents: list[Intent] = []

    if get_finnhub_key():
        intents.extend(_seed_earnings_intents(today))
        intents.extend(_seed_macro_intents(today))
    else:
        logger.info("v2 intent board: Finnhub not configured — skipping calendar seeds")

    intents.extend(_seed_standing_fed_intent(today, intents))
    intents.extend(_seed_company_intents(today))
    intents.extend(_seed_theme_intents(today))

    logger.info(
        "v2 intent board: %d intents (earnings=%d macro=%d fed=%d company=%d ai=%d politics=%d)",
        len(intents),
        sum(1 for i in intents if i.kind == "earnings_print"),
        sum(1 for i in intents if i.kind == "macro_print"),
        sum(1 for i in intents if i.kind in ("fed_decision", "fed_speak")),
        sum(1 for i in intents if i.kind == "company_material"),
        sum(1 for i in intents if i.kind == "ai_catalyst"),
        sum(1 for i in intents if i.kind == "politics_policy"),
    )
    return intents


def _seed_earnings_intents(today) -> list[Intent]:
    watchlist = normalized_watchlist(get_setting("watchlist", []))
    has_watchlist = bool(watchlist)
    yesterday = today - timedelta(days=1)
    preview_end = today + timedelta(days=EARNINGS_PREVIEW_DAYS_FORWARD)

    events = dedupe_calendar_events(
        fetch_earnings_calendar(yesterday.isoformat(), preview_end.isoformat())
    )
    out: list[Intent] = []
    seen: set[str] = set()

    for event in events:
        if len(out) >= _MAX_EARNINGS_INTENTS:
            break

        symbol = (event.get("symbol") or "").upper().strip()
        if not symbol:
            continue

        on_watchlist = has_watchlist and in_watchlist(symbol, watchlist)
        if has_watchlist and not on_watchlist:
            continue

        date_str = (event.get("date") or "")[:10]
        quarter, year = coerce_quarter_year(event.get("quarter"), event.get("year"))
        has_print = _has_actual(event)

        if not has_watchlist and not has_print:
            continue

        if quarter and year:
            if not is_current_reporting_period(quarter, year, as_of=today, require_period=True):
                continue
        elif not has_print:
            continue

        period = f"Q{quarter}-{year}" if quarter and year else None
        intent_id = f"earnings:{symbol}:{date_str}:{period or 'unknown'}"
        if intent_id in seen:
            continue
        seen.add(intent_id)

        status = "reported" if has_print else "preview"
        out.append(
            Intent(
                kind="earnings_print",
                id=intent_id,
                tickers=[symbol],
                period=period,
                label=symbol,
                window_hours=8.0 if has_print else 24.0,
                queries=_earnings_queries(symbol, quarter, year),
                metadata={
                    "date": date_str,
                    "hour": event.get("hour"),
                    "status": status,
                    "on_watchlist": on_watchlist,
                    "quarter": quarter,
                    "year": year,
                    "eps_actual": event.get("epsActual"),
                    "eps_estimate": event.get("epsEstimate"),
                    "revenue_actual": event.get("revenueActual"),
                    "revenue_estimate": event.get("revenueEstimate"),
                },
            )
        )

    return out


def _earnings_queries(symbol: str, quarter: int | None, year: int | None) -> list[str]:
    period_bits: list[str] = []
    if quarter and year:
        period_bits.append(f"Q{quarter} {year}")
        period_bits.append(f"Q{quarter}")
        word = {1: "first", 2: "second", 3: "third", 4: "fourth"}.get(quarter)
        if word:
            period_bits.append(f'"{word} quarter" {year}')
    period_clause = " OR ".join(period_bits) if period_bits else "earnings"
    return [
        f'"{symbol}" ({period_clause}) (earnings OR results) (EPS OR revenue)',
        f'"{symbol}" earnings (beat OR miss OR results) -preview -scheduled',
    ]


def _seed_macro_intents(today) -> list[Intent]:
    yesterday = today - timedelta(days=1)
    events = fetch_economic_calendar(yesterday.isoformat(), today.isoformat())
    out: list[Intent] = []
    seen: set[str] = set()

    for event in events:
        if len(out) >= _MAX_MACRO_INTENTS:
            break
        if not _is_us_event(event) or not _impact_ok(event):
            continue

        event_name = event.get("event") or ""
        label = _macro_label(event_name)
        if not label:
            continue

        date_str = (event.get("date") or today.isoformat())[:10]
        kind: IntentKind = "fed_decision" if label == "Fed" else "macro_print"
        has_actual = event.get("actual") is not None
        status = "reported" if has_actual else "preview"

        intent_id = f"macro:{label}:{date_str}:{status}"
        if intent_id in seen:
            continue
        seen.add(intent_id)

        out.append(
            Intent(
                kind=kind,
                id=intent_id,
                tickers=["SPY", "TLT", "QQQ"],
                period=None,
                label=label,
                window_hours=12.0,
                queries=_macro_queries(label, kind),
                metadata={
                    "date": date_str,
                    "time": event.get("time"),
                    "status": status,
                    "event_name": event_name,
                    "actual": event.get("actual"),
                    "estimate": event.get("estimate"),
                    "unit": event.get("unit"),
                    "impact": event.get("impact"),
                },
            )
        )

    return out


def _macro_queries(label: str, kind: IntentKind) -> list[str]:
    if kind == "fed_decision" or label == "Fed":
        return [
            'FOMC OR "Federal Reserve" (decision OR statement OR rate OR rates) when:1d',
            'Powell OR FOMC (hold OR cut OR hike OR "basis points") when:1d',
        ]
    mapping = {
        "CPI": 'CPI OR "consumer price index" (inflation OR beats OR misses OR rises) when:1d',
        "PPI": 'PPI OR "producer price index" (inflation OR rises OR falls) when:1d',
        "Nonfarm payrolls": '("nonfarm payrolls" OR NFP OR payrolls) (jobs OR employment) when:1d',
        "Unemployment": '("unemployment rate") (jobs OR labor) when:1d',
        "GDP": 'GDP OR "gross domestic product" (growth OR economy) when:1d',
        "Retail sales": '("retail sales") (consumer OR spending) when:1d',
        "Jobless claims": '("jobless claims" OR "initial claims") when:1d',
    }
    primary = mapping.get(label, f'"{label}" economy OR markets when:1d')
    return [primary, f'"{label}" (beats OR misses OR vs OR estimate) when:1d']


def _seed_standing_fed_intent(today, existing: list[Intent]) -> list[Intent]:
    """Ensure Fed coverage appears on the board even without a calendar FOMC row."""
    if any(i.kind in ("fed_decision", "fed_speak") for i in existing):
        return []
    date_str = today.isoformat()
    return [
        Intent(
            kind="fed_speak",
            id=f"fed:standing:{date_str}",
            tickers=["SPY", "TLT", "QQQ"],
            label="Fed",
            window_hours=12.0,
            queries=[
                'Powell OR "Federal Reserve" OR FOMC (speech OR speaks OR testimony OR minutes) when:1d',
                '"Federal Reserve" (rates OR inflation OR "policy") when:1d',
            ],
            metadata={"date": date_str, "status": "standing", "standing": True},
        )
    ]


def _seed_company_intents(today) -> list[Intent]:
    """One material-news intent per watchlist ticker (capped)."""
    watchlist = normalized_watchlist(get_setting("watchlist", []))[:MAX_WEB_TICKERS_PER_CYCLE]
    date_str = today.isoformat()
    out: list[Intent] = []
    for symbol in watchlist[:_MAX_COMPANY_INTENTS]:
        out.append(
            Intent(
                kind="company_material",
                id=f"company:{symbol}:{date_str}",
                tickers=[symbol],
                label=symbol,
                window_hours=12.0,
                queries=[
                    f'"{symbol}" stock (acquires OR acquisition OR merger OR guidance OR layoff OR CEO OR sues OR approved OR FDA) when:1d',
                    f'"{symbol}" (announces OR unveils OR cuts OR raises) -opinion -newsletter when:1d',
                ],
                metadata={"date": date_str, "status": "standing", "standing": True},
            )
        )
    return out


def _normalized_topics() -> list[str]:
    raw = get_setting("search_topics", []) or []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out[:MAX_WEB_TOPICS_PER_CYCLE]


def _seed_theme_intents(today) -> list[Intent]:
    """AI + politics intents from search_topics, with standing fallbacks."""
    date_str = today.isoformat()
    topics = _normalized_topics()
    out: list[Intent] = []

    ai_topics = [t for t in topics if _AI_TOPIC.search(t)]
    pol_topics = [t for t in topics if _POLITICS_TOPIC.search(t)]

    if ai_topics:
        for topic in ai_topics[:3]:
            out.append(
                Intent(
                    kind="ai_catalyst",
                    id=f"ai:{_slug(topic)}:{date_str}",
                    tickers=["NVDA", "MSFT", "GOOGL", "META", "AMZN"],
                    label=topic,
                    window_hours=12.0,
                    queries=[
                        f'"{topic}" (launches OR release OR model OR funding OR partnership) when:1d',
                        f"{topic} (OpenAI OR Anthropic OR Google OR Meta OR Nvidia) when:1d",
                    ],
                    metadata={"date": date_str, "status": "standing", "topic": topic},
                )
            )
    else:
        out.append(
            Intent(
                kind="ai_catalyst",
                id=f"ai:standing:{date_str}",
                tickers=["NVDA", "MSFT", "GOOGL", "META", "AMZN"],
                label="AI",
                window_hours=12.0,
                queries=[
                    "(OpenAI OR Anthropic OR Google OR Meta OR Nvidia) (model OR launches OR release OR GPT OR Claude OR Gemini) when:1d",
                    '"artificial intelligence" (chip OR GPU OR regulation OR partnership) stock when:1d',
                ],
                metadata={"date": date_str, "status": "standing", "standing": True},
            )
        )

    if pol_topics:
        for topic in pol_topics[:3]:
            out.append(
                Intent(
                    kind="politics_policy",
                    id=f"politics:{_slug(topic)}:{date_str}",
                    tickers=["SPY", "XLE", "XLF"],
                    label=topic,
                    window_hours=12.0,
                    queries=[
                        f'"{topic}" (tariff OR sanction OR bill OR executive OR ban OR trade) when:1d',
                        f"{topic} (markets OR stocks OR oil OR rates) when:1d",
                    ],
                    metadata={"date": date_str, "status": "standing", "topic": topic},
                )
            )
    else:
        out.append(
            Intent(
                kind="politics_policy",
                id=f"politics:standing:{date_str}",
                tickers=["SPY", "XLE", "XLF"],
                label="Policy",
                window_hours=12.0,
                queries=[
                    "(tariff OR sanctions OR \"White House\" OR Congress) (trade OR China OR oil OR stocks) when:1d",
                    "(geopolitics OR \"ceasefire\" OR NATO) (markets OR oil OR stocks) when:1d",
                ],
                metadata={"date": date_str, "status": "standing", "standing": True},
            )
        )

    return out


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "topic")[:48]
