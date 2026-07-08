"""Chat assistant — search drafts, headlines, and live news by topic."""

from __future__ import annotations

import json
import re
from typing import Any

from sqlmodel import col, or_, select

from config import MAX_WEB_RESULTS_PER_QUERY
from database import get_session, get_setting
from logging_config import setup_logging
from models import Draft, Headline, Post
from pipeline.freshness import format_age
from pipeline.llm import call_chat_llm
from pipeline.noise import is_title_noise
from pipeline.url_resolve import resolve_article_url
from pipeline.web_search import search_google_news

logger = setup_logging()

_EARNINGS_QUERY = re.compile(r"\bearnings?\b", re.I)

_CHAT_PREFIXES = (
    r"^(?:find|search|show me|look for|get|list|any|what(?:'s| is)?)\s+",
    r"^(?:drafts?|posts?)\s+(?:about|on|for|mentioning)\s+",
    r"^(?:news|headlines?|stories)\s+(?:about|on|for)\s+",
    r"^topic\s+",
    r"^(?:tell me about|latest on)\s+",
)


def _extract_query(message: str) -> tuple[str, list[str]]:
    """Return full phrase and optional word tokens for search."""
    text = message.strip()
    for pattern in _CHAT_PREFIXES:
        text = re.sub(pattern, "", text, flags=re.I).strip()
    text = text.strip("?.!")
    if not text:
        return "", []

    # Keep full phrase for Google News and phrase DB match
    phrase = text
    terms = [t for t in re.split(r"\s+", text) if len(t) >= 2]
    # Single ticker-like token (e.g. NVDA)
    if not terms and re.fullmatch(r"\$?[A-Za-z]{1,5}", text):
        terms = [text.upper().lstrip("$")]
    return phrase, terms


def _text_clauses(columns: list, phrase: str, terms: list[str]) -> list:
    clauses = []
    if phrase:
        pattern = f"%{phrase}%"
        for column in columns:
            clauses.append(col(column).ilike(pattern))
    for term in terms:
        if term.lower() == phrase.lower():
            continue
        pattern = f"%{term}%"
        for column in columns:
            clauses.append(col(column).ilike(pattern))
    return clauses


def search_drafts(phrase: str, terms: list[str], *, limit: int = 12) -> list[dict[str, Any]]:
    clauses = _text_clauses(
        [Draft.text, Draft.tickers, Draft.category],
        phrase,
        terms,
    )
    if not clauses:
        return []

    with get_session() as session:
        rows = session.exec(
            select(Draft, Headline)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(or_(*clauses))
            .order_by(Draft.created_at.desc())
            .limit(limit)
        ).all()

        return [_draft_hit(draft, headline) for draft, headline in rows]


def search_headlines(phrase: str, terms: list[str], *, limit: int = 10) -> list[dict[str, Any]]:
    clauses = _text_clauses(
        [Headline.title, Headline.summary, Headline.source],
        phrase,
        terms,
    )
    if not clauses:
        return []

    with get_session() as session:
        rows = session.exec(
            select(Headline)
            .where(or_(*clauses))
            .order_by(Headline.published_at.desc())
            .limit(limit)
        ).all()

        return [_headline_hit(row) for row in rows]


def search_posted(phrase: str, terms: list[str], *, limit: int = 8) -> list[dict[str, Any]]:
    clauses = _text_clauses([Draft.text, Draft.tickers], phrase, terms)
    if not clauses:
        return []

    with get_session() as session:
        rows = session.exec(
            select(Draft, Post, Headline)
            .join(Post, Post.draft_id == Draft.id)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(Draft.status == "posted", or_(*clauses))
            .order_by(Post.posted_at.desc())
            .limit(limit)
        ).all()

        hits = []
        for draft, post, headline in rows:
            hit = _draft_hit(draft, headline)
            hit["posted_at"] = post.posted_at.isoformat()
            hit["tweet_id"] = post.tweet_id
            hits.append(hit)
        return hits


def fetch_topic_news(query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Search Google News for any topic — tries several query shapes."""
    limit = limit or min(MAX_WEB_RESULTS_PER_QUERY, 8)
    query = query.strip()
    if not query:
        return []

    candidates = [query, f'"{query}"']
    if "news" not in query.lower():
        candidates.append(f"{query} news")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for search_q in candidates:
        if len(items) >= limit:
            break
        raw = search_google_news(search_q, source_label="Chat Search", limit=limit)
        for item in raw:
            title = (item.get("title") or "").strip()
            if not title or is_title_noise(title):
                continue
            url = resolve_article_url(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            items.append({
                "title": title,
                "url": url,
                "source": item.get("source") or "Chat Search",
                "summary": (item.get("summary") or "")[:240],
                "published_at": item.get("published_at").isoformat()
                if item.get("published_at")
                else None,
            })
            if len(items) >= limit:
                break

    return items


def _draft_hit(draft: Draft, headline: Headline) -> dict[str, Any]:
    return {
        "id": draft.id,
        "text": draft.text,
        "status": draft.status,
        "format": draft.format,
        "impact": draft.impact,
        "category": draft.category,
        "tickers": draft.tickers.split(",") if draft.tickers else [],
        "confidence": draft.confidence,
        "created_at": draft.created_at.isoformat(),
        "age": format_age(draft.created_at),
        "headline": {
            "id": headline.id,
            "title": headline.title,
            "source": headline.source,
            "url": headline.url,
        },
    }


def _headline_hit(headline: Headline) -> dict[str, Any]:
    return {
        "id": headline.id,
        "title": headline.title,
        "source": headline.source,
        "url": headline.url,
        "summary": headline.summary[:240] if headline.summary else "",
        "status": headline.status,
        "published_at": headline.published_at.isoformat(),
        "age": format_age(headline.published_at),
    }


def _earnings_query_symbols(terms: list[str]) -> list[str]:
    return [t.upper().lstrip("$") for t in terms if re.fullmatch(r"\$?[A-Za-z]{1,5}", t)]


def _fetch_earnings_for_chat(message: str, terms: list[str]) -> list[dict[str, Any]]:
    if not _EARNINGS_QUERY.search(message) and not _earnings_query_symbols(terms):
        return []
    from pipeline.earnings_calendar import get_earnings_snapshot

    symbols = _earnings_query_symbols(terms)
    return get_earnings_snapshot(
        watchlist=get_setting("watchlist", []),
        query_symbols=symbols or None,
        limit=10,
    )


def _fallback_reply(
    query: str,
    *,
    drafts: list[dict],
    headlines: list[dict],
    posted: list[dict],
    news: list[dict],
    earnings: list[dict] | None = None,
) -> str:
    earnings = earnings or []
    total = len(drafts) + len(headlines) + len(posted) + len(news) + len(earnings)
    if total == 0:
        return (
            f'Nothing in your library for "{query}". '
            "Live news was searched — try a shorter phrase or different wording."
        )

    parts: list[str] = []
    pending = [d for d in drafts if d["status"] in ("pending", "scheduled")]
    other_drafts = [d for d in drafts if d["status"] not in ("pending", "scheduled")]

    if pending:
        parts.append(f"{len(pending)} draft{'s' if len(pending) != 1 else ''} in your queue.")
    if posted:
        parts.append(f"{len(posted)} posted match{'es' if len(posted) != 1 else ''}.")
    if other_drafts:
        parts.append(f"{len(other_drafts)} other draft{'s' if len(other_drafts) != 1 else ''} (rejected/stale).")
    if headlines:
        parts.append(f"{len(headlines)} headline{'s' if len(headlines) != 1 else ''} in the feed.")
    if earnings:
        parts.append(f"{len(earnings)} earnings calendar row{'s' if len(earnings) != 1 else ''}.")
    if news:
        parts.append(f"{len(news)} live news result{'s' if len(news) != 1 else ''}.")

    return " ".join(parts)


def _llm_reply(
    message: str,
    query: str,
    *,
    drafts: list[dict],
    headlines: list[dict],
    posted: list[dict],
    news: list[dict],
    earnings: list[dict] | None = None,
) -> str | None:
    context = {
        "user_message": message,
        "search_query": query,
        "drafts": drafts[:6],
        "posted": posted[:4],
        "headlines": headlines[:4],
        "news": news[:6],
        "earnings_calendar": (earnings or [])[:8],
    }
    system = (
        "You are PostPilot's search assistant for stock, tech, and general news topics. "
        "Summarize search results in 2-4 short sentences. Be direct and helpful. "
        "Mention counts and highlight the most relevant match. "
        "If earnings_calendar rows are present, mention upcoming or reported names. "
        "If only live news returned, say that and note the top headline. Plain text only."
    )
    user = f"Results JSON:\n{json.dumps(context, default=str)[:6000]}"
    raw = call_chat_llm(system, user, max_tokens=300)
    return raw.strip() if raw else None


def chat_search(message: str, *, fetch_news: bool = True) -> dict[str, Any]:
    """Search drafts, headlines, and live news for any topic or keyword."""
    message = message.strip()
    if not message:
        return {
            "reply": "Ask about any topic, ticker, company, or draft text.",
            "query": "",
            "drafts": [],
            "headlines": [],
            "posted": [],
            "news": [],
            "earnings": [],
        }

    phrase, terms = _extract_query(message)
    query = phrase or message.strip()

    earnings = _fetch_earnings_for_chat(message, terms)
    drafts = search_drafts(phrase, terms)
    posted = search_posted(phrase, terms)
    posted_ids = {p["id"] for p in posted}
    drafts = [d for d in drafts if d["id"] not in posted_ids and d["status"] != "posted"]
    headlines = search_headlines(phrase, terms)
    draft_headline_ids = {
        d["headline"]["id"] for d in drafts if d.get("headline") and d["headline"].get("id")
    }
    headlines = [h for h in headlines if h["id"] not in draft_headline_ids]

    local_count = len(drafts) + len(headlines) + len(posted) + len(earnings)
    news: list[dict[str, Any]] = []
    if fetch_news or local_count == 0:
        news = fetch_topic_news(query)
        if _EARNINGS_QUERY.search(message) and len(news) < 4:
            extra = fetch_topic_news(f"stocks earnings reports {query}", limit=6)
            seen = {n["url"] for n in news}
            for item in extra:
                if item["url"] not in seen:
                    news.append(item)
                    seen.add(item["url"])

    reply = _llm_reply(
        message, query, drafts=drafts, headlines=headlines, posted=posted, news=news, earnings=earnings,
    )
    if not reply:
        reply = _fallback_reply(
            query, drafts=drafts, headlines=headlines, posted=posted, news=news, earnings=earnings,
        )

    return {
        "reply": reply,
        "query": query,
        "drafts": drafts,
        "headlines": headlines,
        "posted": posted,
        "news": news,
        "earnings": earnings,
    }
