"""Finnhub API client with diagnostics."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from config import FINNHUB_ENV_NAMES, get_finnhub_key
from logging_config import setup_logging

logger = setup_logging()

ENV_KEY_NAMES = FINNHUB_ENV_NAMES


def get_finnhub_key_source() -> str | None:
    for name in ENV_KEY_NAMES:
        if os.getenv(name, "").strip():
            return name
    return None


BASE_URL = "https://finnhub.io/api/v1"


def parse_finnhub_timestamp(ts: Any) -> datetime:
    """Parse Finnhub unix timestamp; fall back to now if missing/invalid."""
    if not ts:
        return datetime.utcnow()
    try:
        dt = datetime.utcfromtimestamp(int(ts))
        if dt.year < 2000:
            return datetime.utcnow()
        return dt
    except (ValueError, OSError, OverflowError, TypeError):
        return datetime.utcnow()


def finnhub_get(path: str, params: dict | None = None) -> tuple[Any | None, str | None]:
    """GET Finnhub API. Returns (data, error_message)."""
    token = get_finnhub_key()
    if not token:
        return None, "FINNHUB_KEY not set — add it in Railway Variables and redeploy"

    query = dict(params or {})
    query["token"] = token

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(f"{BASE_URL}/{path.lstrip('/')}", params=query)
    except httpx.HTTPError as exc:
        logger.warning("Finnhub network error (%s): %s", path, exc)
        return None, f"Network error: {exc}"

    if resp.status_code == 401:
        return None, "Invalid API key — check FINNHUB_KEY on Railway"
    if resp.status_code == 403:
        return None, "API access forbidden — check your Finnhub plan"
    if resp.status_code == 429:
        return None, "Finnhub rate limit hit — try again in a minute"
    if resp.status_code >= 400:
        body = resp.text[:200]
        return None, f"Finnhub HTTP {resp.status_code}: {body}"

    try:
        return resp.json(), None
    except ValueError:
        return None, "Finnhub returned non-JSON response"


def test_finnhub_connection() -> dict:
    """Run live API checks for the settings diagnostics UI."""
    key = get_finnhub_key()
    source = get_finnhub_key_source()
    result: dict[str, Any] = {
        "configured": bool(key),
        "env_var": source,
        "key_hint": f"{key[:4]}…" if len(key) >= 4 else None,
        "news": None,
        "earnings": None,
        "company_news": None,
        "error": None,
    }

    if not key:
        result["error"] = (
            "No API key found. Set FINNHUB_KEY in Railway → Variables → redeploy. "
            "Name must be exactly FINNHUB_KEY."
        )
        return result

    today = datetime.utcnow().date().isoformat()

    news_data, news_err = finnhub_get("news", {"category": "general"})
    if news_err:
        result["error"] = news_err
        result["news"] = {"ok": False, "error": news_err}
    else:
        count = len(news_data) if isinstance(news_data, list) else 0
        result["news"] = {"ok": True, "count": count}

    earn_data, earn_err = finnhub_get("calendar/earnings", {"from": today, "to": today})
    if earn_err and not result["error"]:
        result["error"] = earn_err
    if earn_err:
        result["earnings"] = {"ok": False, "error": earn_err}
    else:
        items = (earn_data or {}).get("earningsCalendar") or []
        result["earnings"] = {"ok": True, "count": len(items)}

    # Spot-check company news for a liquid name
    co_data, co_err = finnhub_get(
        "company-news",
        {"symbol": "NVDA", "from": today, "to": today},
    )
    if co_err:
        result["company_news"] = {"ok": False, "error": co_err}
    else:
        count = len(co_data) if isinstance(co_data, list) else 0
        result["company_news"] = {"ok": True, "count": count}

    if not result["error"]:
        result["error"] = None

    return result
