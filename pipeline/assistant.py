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
from pipeline.draft_lane import draft_lane
from pipeline.freshness import format_age
from pipeline.llm import call_chat_llm
from pipeline.noise import is_title_noise
from pipeline.url_resolve import resolve_article_url
from pipeline.web_search import search_news

logger = setup_logging()

_EARNINGS_QUERY = re.compile(r"\bearnings?\b", re.I)
_TICKER_RE = re.compile(r"\$?([A-Z]{1,5})\b")
_WATCHLIST_INTENT = re.compile(
    r"\b(?:my watchlist|watchlist stocks?|stocks? i(?:'m| am) tracking|what am i tracking)\b",
    re.I,
)
_TOPICS_INTENT = re.compile(
    r"\b(?:tracked topics?|search topics?|topics? i(?:'m| am) tracking|what topics)\b",
    re.I,
)

_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "as", "is", "was", "are", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "can", "about", "into", "through", "during", "before", "after", "above", "below",
    "between", "under", "over", "again", "further", "then", "once", "here", "there",
    "when", "where", "why", "how", "all", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "don", "now", "what", "which", "who", "whom", "this", "that", "these",
    "those", "am", "any", "both", "get", "got", "tell", "me", "show", "find", "search",
    "look", "see", "give", "latest", "news", "happening", "going", "update", "updates",
})

_CHAT_PREFIXES = (
    r"^(?:find|search|show me|look for|get|list|any|what(?:'s| is| are)?)\s+",
    r"^(?:drafts?|posts?)\s+(?:about|on|for|mentioning)\s+",
    r"^(?:news|headlines?|stories)\s+(?:about|on|for)\s+",
    r"^topic\s+",
    r"^(?:tell me about|latest on|what(?:'s| is) (?:the )?latest on)\s+",
    r"^(?:how (?:is|are)|what(?:'s| is) happening (?:with|in))\s+",
    r"^(?:anything on|updates? on)\s+",
)


def _extract_query(message: str) -> tuple[str, list[str]]:
    """Return full phrase and optional word tokens for search."""
    text = message.strip()
    for pattern in _CHAT_PREFIXES:
        text = re.sub(pattern, "", text, flags=re.I).strip()
    text = text.strip("?.!")
    if not text:
        return "", []

    phrase = text
    terms = [t for t in re.split(r"\s+", text) if len(t) >= 2]
    tickers = [m.group(1) for m in _TICKER_RE.finditer(message.upper())]
    terms.extend(t for t in tickers if t not in terms)
    if not terms and re.fullmatch(r"\$?[A-Za-z]{1,5}", text):
        terms = [text.upper().lstrip("$")]
    return phrase, terms


def _significant_terms(terms: list[str], phrase: str = "") -> list[str]:
    """Drop stop words; keep tickers and meaningful tokens."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in terms:
        t = raw.strip().lstrip("$")
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        if re.fullmatch(r"[A-Za-z]{1,5}", t):
            out.append(t.upper())
            seen.add(key)
            continue
        if len(t) < 3 or key in _STOP_WORDS:
            continue
        out.append(t)
        seen.add(key)
    if not out and phrase:
        for t in re.split(r"\s+", phrase):
            key = t.lower().strip("?.!")
            if len(key) >= 3 and key not in _STOP_WORDS and key not in seen:
                out.append(t.strip("?.!"))
                seen.add(key)
    return out[:10]


def _search_phrase(phrase: str, terms: list[str]) -> str:
    """Short phrase for DB ILIKE — avoid matching the entire question string."""
    sig = _significant_terms(terms, phrase)
    if len(sig) >= 2:
        return " ".join(sig[:5])
    if sig:
        return sig[0]
    words = [w for w in re.split(r"\s+", phrase) if w.lower() not in _STOP_WORDS and len(w) >= 3]
    return " ".join(words[:5]) if words else phrase[:80]


def _heuristic_web_queries(message: str, phrase: str, terms: list[str]) -> list[str]:
    """Build several Google News query shapes without an LLM."""
    sig = _significant_terms(terms, phrase)
    core = _search_phrase(phrase, terms)
    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = q.strip()
        if not q or q.lower() in seen:
            return
        seen.add(q.lower())
        queries.append(q)

    if core:
        add(core)
        if len(core.split()) <= 4:
            add(f'"{core}"')
    if sig:
        add(" ".join(sig[:4]))
        tickers = [t for t in sig if re.fullmatch(r"[A-Z]{1,5}", t)]
        for ticker in tickers[:2]:
            add(f"{ticker} stock news")
    if _EARNINGS_QUERY.search(message) and core:
        add(f"{core} earnings report")
    if len(queries) < 2 and phrase and phrase.lower() != core.lower():
        add(phrase[:120])
    return queries[:5]


def _llm_expand_query(message: str) -> dict[str, Any] | None:
    """Use LLM to turn natural language into search phrases and web queries."""
    system = (
        "Extract search terms from questions about stocks, tech, macro, geopolitics, and news. "
        "Return JSON only with keys: phrase (2-6 word core topic), terms (2-8 keywords/tickers), "
        "web_queries (2-4 diverse Google News search strings). "
        "No stop words in terms. Include ticker symbols when obvious (e.g. Apple -> AAPL). "
        "web_queries should be concrete news searches, not questions."
    )
    user = f"User message:\n{message[:400]}"
    raw = call_chat_llm(system, user, max_tokens=220)
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    phrase = str(data.get("phrase") or "").strip()
    terms = data.get("terms") if isinstance(data.get("terms"), list) else []
    web_queries = data.get("web_queries") if isinstance(data.get("web_queries"), list) else []
    return {
        "phrase": phrase,
        "terms": [str(t).strip() for t in terms if str(t).strip()],
        "web_queries": [str(q).strip() for q in web_queries if str(q).strip()],
    }


def _needs_llm_expand(message: str, phrase: str, terms: list[str]) -> bool:
    """Skip LLM rewrite for short/ticker-like queries — saves latency and tokens."""
    words = message.split()
    if len(words) <= 3 and _significant_terms(terms, phrase):
        return False
    if len(message) < 20 and re.fullmatch(r"\$?[A-Za-z]{1,5}", phrase.strip()):
        return False
    if len(words) <= 5 and not re.search(r"\?", message):
        return False
    return True


def _prepare_search(message: str) -> dict[str, Any]:
    """Normalize user message into DB phrase, terms, and web query list."""
    phrase, terms = _extract_query(message)
    expanded = _llm_expand_query(message) if _needs_llm_expand(message, phrase, terms) else None

    web_queries: list[str] = []
    if expanded:
        if expanded.get("phrase"):
            phrase = expanded["phrase"]
        merged_terms = list(dict.fromkeys((expanded.get("terms") or []) + terms))
        terms = merged_terms
        web_queries = expanded.get("web_queries") or []

    terms = _significant_terms(terms, phrase)
    db_phrase = _search_phrase(phrase or message, terms)
    if not web_queries:
        web_queries = _heuristic_web_queries(message, phrase or message, terms)

    return {
        "phrase": db_phrase,
        "terms": terms,
        "web_queries": web_queries,
        "display_query": phrase or message.strip()[:120],
    }


def _text_clauses(columns: list, phrase: str, terms: list[str]) -> list:
    clauses = []
    if phrase:
        pattern = f"%{phrase}%"
        for column in columns:
            clauses.append(col(column).ilike(pattern))
    for term in terms:
        if phrase and term.lower() == phrase.lower():
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


def _search_for_tickers(tickers: list[str], *, draft_limit: int = 8) -> tuple[list[dict], list[dict], list[dict]]:
    """Search drafts/headlines/posted for a list of tickers."""
    if not tickers:
        return [], [], []
    drafts: list[dict] = []
    headlines: list[dict] = []
    posted: list[dict] = []
    seen_draft: set[int] = set()
    seen_headline: set[int] = set()
    for ticker in tickers[:12]:
        for hit in search_drafts(ticker, [ticker], limit=draft_limit):
            if hit["id"] not in seen_draft:
                drafts.append(hit)
                seen_draft.add(hit["id"])
        for hit in search_headlines(ticker, [ticker], limit=6):
            if hit["id"] not in seen_headline:
                headlines.append(hit)
                seen_headline.add(hit["id"])
        for hit in search_posted(ticker, [ticker], limit=4):
            posted.append(hit)
    return drafts, headlines, posted


def _search_tracked_topics(topics: list[str], *, limit: int = 10) -> tuple[list[dict], list[dict]]:
    """Search library content matching user's tracked search topics."""
    drafts: list[dict] = []
    headlines: list[dict] = []
    seen_draft: set[int] = set()
    seen_headline: set[int] = set()
    for topic in topics[:8]:
        phrase, terms = _extract_query(topic)
        sig = _significant_terms(terms, phrase or topic)
        db_phrase = _search_phrase(phrase or topic, sig)
        for hit in search_drafts(db_phrase, sig, limit=limit):
            if hit["id"] not in seen_draft:
                drafts.append(hit)
                seen_draft.add(hit["id"])
        for hit in search_headlines(db_phrase, sig, limit=6):
            if hit["id"] not in seen_headline:
                headlines.append(hit)
                seen_headline.add(hit["id"])
    return drafts, headlines


def fetch_topic_news(
    queries: str | list[str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Search Google News — tries multiple query shapes and widens recency if needed."""
    if isinstance(queries, str):
        query_list = [queries] if queries.strip() else []
    else:
        query_list = [q.strip() for q in queries if q and q.strip()]
    if not query_list:
        return []

    limit = limit or min(MAX_WEB_RESULTS_PER_QUERY + 4, 12)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for search_q in query_list:
        if len(items) >= limit:
            break
        variants = [search_q]
        if "news" not in search_q.lower():
            variants.append(f"{search_q} news")
        if not search_q.startswith('"') and len(search_q.split()) <= 5:
            variants.append(f'"{search_q}"')

        for variant in variants:
            if len(items) >= limit:
                break
            for recency in ("1d", "7d"):
                raw = search_news(
                    variant,
                    source_label="Chat Search",
                    limit=limit,
                    recency=recency,
                )
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
                        "summary": (item.get("summary") or "")[:280],
                        "published_at": item.get("published_at").isoformat()
                        if item.get("published_at")
                        else None,
                    })
                    if len(items) >= limit:
                        break
                if len(items) >= 4 or recency == "7d":
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
        "lane": draft_lane(draft.category, draft.tickers),
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
        "summary": headline.summary[:280] if headline.summary else "",
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
            f'Nothing found for "{query}". '
            "Try a shorter phrase, a ticker symbol, or a company name."
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
        top = news[0]["title"][:80] if news else ""
        parts.append(
            f"{len(news)} live news result{'s' if len(news) != 1 else ''}"
            + (f" — top: {top}" if top else "") + "."
        )

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
    web_queries: list[str] | None = None,
) -> str | None:
    context = {
        "user_message": message,
        "search_query": query,
        "web_queries_used": (web_queries or [])[:4],
        "drafts": drafts[:8],
        "posted": posted[:5],
        "headlines": headlines[:6],
        "news": news[:8],
        "earnings_calendar": (earnings or [])[:8],
    }
    system = (
        "You are PostPilot's research assistant for stocks, tech, macro, and news. "
        "Answer the user's question using the search results provided. "
        "Write 3-6 concise sentences. Lead with the direct answer, then mention your library "
        "(drafts, queue, posted) vs live web news. Highlight the most relevant headline or draft. "
        "If earnings_calendar rows exist, note upcoming or recent reports. "
        "If results are thin, say what was searched and suggest a tighter query or ticker. "
        "Facts only — no speculation. Plain text, no markdown."
    )
    user = f"Results JSON:\n{json.dumps(context, default=str)[:8000]}"
    raw = call_chat_llm(system, user, max_tokens=450)
    return raw.strip() if raw else None


def chat_search(message: str, *, fetch_news: bool = True) -> dict[str, Any]:
    """Search drafts, headlines, and live news for any topic or keyword."""
    message = message.strip()
    if not message:
        return {
            "reply": "Ask about any topic, ticker, company, macro event, or draft text.",
            "query": "",
            "web_queries": [],
            "drafts": [],
            "headlines": [],
            "posted": [],
            "news": [],
            "earnings": [],
        }

    search = _prepare_search(message)
    phrase = search["phrase"]
    terms = search["terms"]
    web_queries = search["web_queries"]
    display_query = search["display_query"]

    watchlist = get_setting("watchlist", [])
    search_topics = get_setting("search_topics", [])

    earnings = _fetch_earnings_for_chat(message, terms)
    drafts = search_drafts(phrase, terms)
    posted = search_posted(phrase, terms)
    headlines = search_headlines(phrase, terms)

    if _WATCHLIST_INTENT.search(message) and watchlist:
        wl_drafts, wl_headlines, wl_posted = _search_for_tickers(watchlist)
        drafts = _merge_hits(drafts, wl_drafts, key="id")
        headlines = _merge_hits(headlines, wl_headlines, key="id")
        posted = _merge_hits(posted, wl_posted, key="id")
        if not web_queries:
            web_queries = [f"{t} stock news" for t in watchlist[:3]]

    if _TOPICS_INTENT.search(message) and search_topics:
        topic_drafts, topic_headlines = _search_tracked_topics(search_topics)
        drafts = _merge_hits(drafts, topic_drafts, key="id")
        headlines = _merge_hits(headlines, topic_headlines, key="id")
        if not web_queries:
            web_queries = list(search_topics[:4])

    posted_ids = {p["id"] for p in posted}
    drafts = [d for d in drafts if d["id"] not in posted_ids and d["status"] != "posted"]
    draft_headline_ids = {
        d["headline"]["id"] for d in drafts if d.get("headline") and d["headline"].get("id")
    }
    headlines = [h for h in headlines if h["id"] not in draft_headline_ids]

    local_count = len(drafts) + len(headlines) + len(posted) + len(earnings)
    news: list[dict[str, Any]] = []
    if fetch_news or local_count == 0:
        news = fetch_topic_news(web_queries or [display_query])
        if _EARNINGS_QUERY.search(message) and len(news) < 4:
            extra = fetch_topic_news(
                [f"stocks earnings reports {display_query}", f"{display_query} earnings"],
                limit=6,
            )
            seen = {n["url"] for n in news}
            for item in extra:
                if item["url"] not in seen:
                    news.append(item)
                    seen.add(item["url"])

    reply = _llm_reply(
        message,
        display_query,
        drafts=drafts,
        headlines=headlines,
        posted=posted,
        news=news,
        earnings=earnings,
        web_queries=web_queries,
    )
    if not reply:
        reply = _fallback_reply(
            display_query,
            drafts=drafts,
            headlines=headlines,
            posted=posted,
            news=news,
            earnings=earnings,
        )

    return {
        "reply": reply,
        "query": display_query,
        "web_queries": web_queries,
        "drafts": drafts,
        "headlines": headlines,
        "posted": posted,
        "news": news,
        "earnings": earnings,
    }


def _merge_hits(existing: list[dict], extra: list[dict], *, key: str) -> list[dict]:
    seen = {item[key] for item in existing if key in item}
    out = list(existing)
    for item in extra:
        val = item.get(key)
        if val is None or val in seen:
            continue
        seen.add(val)
        out.append(item)
    return out
