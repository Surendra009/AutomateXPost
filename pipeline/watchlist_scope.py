"""Watchlist scoping — structured feeds only run for user-configured tickers."""


def normalized_watchlist(watchlist: list[str] | None) -> list[str]:
    if not watchlist or not isinstance(watchlist, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in watchlist:
        sym = raw.upper().strip()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def in_watchlist(symbol: str | None, watchlist: list[str]) -> bool:
    if not symbol:
        return False
    symbols = normalized_watchlist(watchlist)
    if not symbols:
        return False
    return symbol.upper() in symbols
