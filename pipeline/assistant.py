"""Chat assistant — search drafts, headlines, and live news by topic."""

from __future__ import annotations

import json
import re
from typing import Any

from sqlmodel import col, or_, select

from config import ANTHROPIC_API_KEY, FILTER_MODEL, MAX_WEB_RESULTS_PER_QUERY
from database import get_session
from logging_config import setup_logging
from models import Draft, Headline, Post
from pipeline.filter import _call_claude
from pipeline.freshness import format_age
from pipeline.noise import is_title_noise
from pipeline.url_resolve import resolve_article_url
from pipeline.web_search import search_google_news

logger = setup_logging()

_CHAT_PREFIXES = (
    r"^(?:find|search|show me|look for|get|list|any)\s+",
    r"^(?:drafts?|posts?)\s+(?:about|on|for|mentioning)\s+",
    r"^(?:news|headlines?|stories)\s+(?:about|on|for)\s+",
    r"^topic\s+",
)


def _extract_terms(message: str) -> list[str]:
    text = message.strip()
    for pattern in _CHAT_PREFIXES:
        text = re.sub(pattern, "", text, flags=re.I).strip()
    text = text.strip("?.!")
    if not text:
        return []
    terms = [t for t in re.split(r"\s+", text) if len(t) >= 2]
    return terms or [message.strip()]


def _term_clauses(column, terms: list[str]):
    return [col(column).ilike(f"%{term}%") for term in terms]


def search_drafts(terms: list[str], *, limit: int = 12) -> list[dict[str, Any]]:
    if not terms:
        return []

    draft_filters = []
    for term in terms:
        draft_filters.extend([
            col(Draft.text).ilike(f"%{term}%"),
            col(Draft.tickers).ilike(f"%{term}%"),
            col(Draft.category).ilike(f"%{term}%"),
        ])

    with get_session() as session:
        rows = session.exec(
            select(Draft, Headline)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(or_(*draft_filters))
            .order_by(Draft.created_at.desc())
            .limit(limit)
        ).all()

        return [_draft_hit(draft, headline) for draft, headline in rows]


def search_headlines(terms: list[str], *, limit: int = 10) -> list[dict[str, Any]]:
    if not terms:
        return []

    filters = []
    for term in terms:
        filters.extend([
            col(Headline.title).ilike(f"%{term}%"),
            col(Headline.summary).ilike(f"%{term}%"),
            col(Headline.source).ilike(f"%{term}%"),
        ])

    with get_session() as session:
        rows = session.exec(
            select(Headline)
            .where(or_(*filters))
            .order_by(Headline.published_at.desc())
            .limit(limit)
        ).all()

        return [_headline_hit(row) for row in rows]


def search_posted(terms: list[str], *, limit: int = 8) -> list[dict[str, Any]]:
    if not terms:
        return []

    filters = []
    for term in terms:
        filters.extend([
            col(Draft.text).ilike(f"%{term}%"),
            col(Draft.tickers).ilike(f"%{term}%"),
        ])

    with get_session() as session:
        rows = session.exec(
            select(Draft, Post, Headline)
            .join(Post, Post.draft_id == Draft.id)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(Draft.status == "posted", or_(*filters))
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
    limit = limit or min(MAX_WEB_RESULTS_PER_QUERY, 8)
    search_q = query if "news" in query.lower() else f'"{query}" news'
    raw = search_google_news(search_q, source_label="Chat Search", limit=limit)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

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


def _fallback_reply(
    query: str,
    *,
    drafts: list[dict],
    headlines: list[dict],
    posted: list[dict],
    news: list[dict],
) -> str:
    total = len(drafts) + len(headlines) + len(posted) + len(news)
    if total == 0:
        return (
            f'No matches for "{query}". Try a ticker ($NVDA), company name, or topic. '
            "Enable Live news to search Google News."
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
) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None

    context = {
        "user_message": message,
        "search_query": query,
        "drafts": drafts[:6],
        "posted": posted[:4],
        "headlines": headlines[:4],
        "news": news[:4],
    }
    system = (
        "You are PostPilot's search assistant. Summarize search results in 2-4 short sentences. "
        "Be direct and helpful. Mention counts and highlight the most relevant match. "
        "If nothing found, suggest trying a ticker or enabling live news. Plain text only."
    )
    user = f"Results JSON:\n{json.dumps(context, default=str)[:6000]}"
    raw = _call_claude(system, user, FILTER_MODEL, max_tokens=300)
    return raw.strip() if raw else None


def chat_search(message: str, *, fetch_news: bool = False) -> dict[str, Any]:
    """Search drafts, headlines, and optionally live news for a natural-language query."""
    message = message.strip()
    if not message:
        return {
            "reply": "Ask about a ticker, company, topic, or draft text.",
            "query": "",
            "drafts": [],
            "headlines": [],
            "posted": [],
            "news": [],
        }

    terms = _extract_terms(message)
    query = " ".join(terms)

    drafts = search_drafts(terms)
    posted = search_posted(terms)
    posted_ids = {p["id"] for p in posted}
    drafts = [d for d in drafts if d["id"] not in posted_ids and d["status"] != "posted"]
    headlines = search_headlines(terms)
    draft_headline_ids = {
        d["headline"]["id"] for d in drafts if d.get("headline") and d["headline"].get("id")
    }
    headlines = [h for h in headlines if h["id"] not in draft_headline_ids]

    news: list[dict[str, Any]] = []
    if fetch_news:
        news = fetch_topic_news(query)

    reply = _llm_reply(message, query, drafts=drafts, headlines=headlines, posted=posted, news=news)
    if not reply:
        reply = _fallback_reply(query, drafts=drafts, headlines=headlines, posted=posted, news=news)

    return {
        "reply": reply,
        "query": query,
        "drafts": drafts,
        "headlines": headlines,
        "posted": posted,
        "news": news,
    }
