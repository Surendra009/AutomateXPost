"""Cross-pull earnings context from web search + Finnhub before drafting."""

from __future__ import annotations

from dataclasses import dataclass, field

from config import (
    MAX_EARNINGS_WEB_ARTICLES,
    MAX_EARNINGS_WEB_QUERIES,
    MAX_WEB_RESULTS_PER_QUERY,
    WEB_SEARCH_ENABLED,
)
from logging_config import setup_logging
from pipeline.earnings_parse import (
    EarningsFacts,
    _EARNINGS_NEWS,
    extract_earnings_facts,
    fetch_earnings_article_text,
    fetch_earnings_news_context,
)
from pipeline.enrich import fetch_article_text
from pipeline.web_search import search_google_news

logger = setup_logging()

_cycle_enrichments: list[dict] = []


def reset_earnings_enrich_stats() -> None:
    global _cycle_enrichments
    _cycle_enrichments = []


def earnings_enrich_summary() -> dict:
    """Last pipeline cycle earnings web cross-pull stats for settings UI."""
    verified = sum(1 for row in _cycle_enrichments if row.get("verified"))
    return {
        "tickers_enriched": len(_cycle_enrichments),
        "articles_fetched": sum(row.get("articles", 0) for row in _cycle_enrichments),
        "web_headlines": sum(row.get("headlines", 0) for row in _cycle_enrichments),
        "cross_verified": verified,
        "recent": _cycle_enrichments[-5:],
    }


@dataclass
class EarningsEnrichment:
    news_context: str = ""
    article_text: str = ""
    facts: EarningsFacts | None = None
    sources: list[str] = field(default_factory=list)
    cross_check: list[str] = field(default_factory=list)


def _parse_eps(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _parse_money(value: str | None) -> float | None:
    if not value:
        return None
    s = value.replace("$", "").replace(",", "").strip()
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
    try:
        return float(s) * mult
    except ValueError:
        return None


def _money_close(a: str | None, b: str | None, tol: float = 0.06) -> bool:
    fa, fb = _parse_money(a), _parse_money(b)
    if fa is None or fb is None:
        return False
    if fb == 0:
        return fa == 0
    return abs(fa - fb) / abs(fb) <= tol


def cross_check_facts(finnhub: EarningsFacts, web: EarningsFacts) -> list[str]:
    """Compare Finnhub structured numbers to text extracted from web articles."""
    notes: list[str] = []
    verified: list[str] = []

    fe, we = _parse_eps(finnhub.eps_actual), _parse_eps(web.eps_actual)
    if fe is not None and we is not None:
        if abs(fe - we) <= 0.02:
            verified.append("EPS")
        else:
            notes.append(f"Web EPS {web.eps_actual} differs from Finnhub {finnhub.eps_actual}")

    fe, we = _parse_eps(finnhub.eps_estimate), _parse_eps(web.eps_estimate)
    if fe is not None and we is not None and abs(fe - we) > 0.02:
        notes.append(f"Web EPS est {web.eps_estimate} differs from Finnhub {finnhub.eps_estimate}")

    if finnhub.revenue_actual and web.revenue_actual:
        if _money_close(finnhub.revenue_actual, web.revenue_actual):
            verified.append("revenue")
        else:
            notes.append(
                f"Web revenue {web.revenue_actual} differs from Finnhub {finnhub.revenue_actual}"
            )

    if verified:
        notes.insert(0, f"Cross-verified: {', '.join(verified)}")
    return notes


def merge_facts(primary: EarningsFacts, supplemental: EarningsFacts) -> EarningsFacts:
    """Keep Finnhub numbers; fill gaps from web extraction."""
    return EarningsFacts(
        quarter=primary.quarter or supplemental.quarter,
        eps_actual=primary.eps_actual or supplemental.eps_actual,
        eps_estimate=primary.eps_estimate or supplemental.eps_estimate,
        revenue_actual=primary.revenue_actual or supplemental.revenue_actual,
        revenue_estimate=primary.revenue_estimate or supplemental.revenue_estimate,
        yoy_pct=primary.yoy_pct or supplemental.yoy_pct,
    )


def _is_earnings_hit(symbol: str, title: str, summary: str) -> bool:
    blob = f"{title} {summary}"
    if _EARNINGS_NEWS.search(blob):
        return True
    return symbol.upper() in title.upper()


def enrich_earnings_context(
    symbol: str,
    *,
    quarter: int | None = None,
    year: int | None = None,
    finnhub_facts: EarningsFacts | None = None,
    finnhub_summary: str = "",
    headline_url: str = "",
) -> EarningsEnrichment:
    """Web search + article fetch to enrich/verify Finnhub earnings before LLM."""
    symbol = symbol.upper()
    chunks: list[str] = []
    articles: list[str] = []
    sources: list[str] = []

    if finnhub_summary:
        chunks.append(finnhub_summary)

    fh_news = fetch_earnings_news_context(symbol)
    if fh_news:
        chunks.append(fh_news)

    web_items: list[dict] = []
    if WEB_SEARCH_ENABLED:
        q_label = f"Q{quarter}" if quarter else ""
        year_s = str(year) if year else ""
        queries = [
            f'"{symbol}" {q_label} earnings EPS revenue beat miss {year_s}'.strip(),
            f'"{symbol}" earnings results segment guidance outlook',
            f'"{symbol}" quarterly earnings press release',
        ][:MAX_EARNINGS_WEB_QUERIES]
        seen_urls: set[str] = set()
        for query in queries:
            batch = search_google_news(
                query,
                source_label="Web Search · earnings verify",
                limit=MAX_WEB_RESULTS_PER_QUERY,
            )
            for item in batch:
                title = (item.get("title") or "").strip()
                summary = (item.get("summary") or "").strip()
                url = (item.get("url") or "").strip()
                if not title or not url or url in seen_urls:
                    continue
                if not _is_earnings_hit(symbol, title, summary):
                    continue
                seen_urls.add(url)
                chunks.append(f"{title} {summary}".strip())
                web_items.append(item)

        logger.info(
            "Earnings web search %s: %d headlines from %d queries",
            symbol,
            len(web_items),
            len(queries),
        )

    fh_article = fetch_earnings_article_text(symbol)
    if fh_article:
        articles.append(fh_article)

    if headline_url and headline_url.startswith("http"):
        direct = fetch_article_text(headline_url)
        if direct and len(direct) > 200 and direct not in articles:
            articles.append(direct[:3500])

    fetched = 0
    for item in web_items:
        if fetched >= MAX_EARNINGS_WEB_ARTICLES:
            break
        url = (item.get("url") or "").strip()
        if not url:
            continue
        text = fetch_article_text(url)
        if text and len(text) > 200:
            articles.append(text[:3500])
            sources.append(url)
            fetched += 1

    news_context = " ".join(chunks)[:5000]
    article_text = "\n\n---\n\n".join(articles)[:6000]

    cross_check: list[str] = []
    merged = finnhub_facts
    if finnhub_facts and (news_context or article_text):
        web_facts = extract_earnings_facts(f"{news_context} {article_text}")
        cross_check = cross_check_facts(finnhub_facts, web_facts)
        merged = merge_facts(finnhub_facts, web_facts)
        if cross_check:
            logger.info("Earnings cross-check %s: %s", symbol, "; ".join(cross_check))

    verified = any(note.startswith("Cross-verified:") for note in cross_check)
    _cycle_enrichments.append(
        {
            "symbol": symbol,
            "headlines": len(web_items),
            "articles": len(sources),
            "verified": verified,
            "notes": cross_check[:2],
        }
    )

    return EarningsEnrichment(
        news_context=news_context,
        article_text=article_text,
        facts=merged,
        sources=sources,
        cross_check=cross_check,
    )
