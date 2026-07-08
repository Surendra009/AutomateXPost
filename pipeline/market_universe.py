"""Default ticker universe when watchlist is empty or thin."""

from __future__ import annotations

import os

from config import DEFAULT_MARKET_UNIVERSE
from database import get_setting
from pipeline.watchlist_scope import normalized_watchlist


def _parse_env_tickers(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        sym = part.strip().upper().lstrip("$")
        if sym and sym not in out:
            out.append(sym)
    return out


def default_market_universe() -> list[str]:
    """Liquid large caps + indices used for earnings, movers, and company news."""
    env = os.getenv("DEFAULT_MARKET_UNIVERSE", "").strip()
    if env:
        return _parse_env_tickers(env)
    return list(DEFAULT_MARKET_UNIVERSE)


def scan_universe(*, max_symbols: int | None = None) -> list[str]:
    """Watchlist first, then fill from default universe."""
    watchlist = normalized_watchlist(get_setting("watchlist", []))
    seen: set[str] = set()
    out: list[str] = []
    for sym in watchlist:
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    for sym in default_market_universe():
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
        if max_symbols and len(out) >= max_symbols:
            break
    if max_symbols:
        return out[:max_symbols]
    return out
