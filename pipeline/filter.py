"""LLM filter step — classify headline relevance."""

import json
from typing import Any

from sqlmodel import select

from config import ANTHROPIC_API_KEY, FILTER_MODEL, MIN_AI_RELEVANCE_SCORE, MIN_RELEVANCE_SCORE
from database import get_session, get_setting
from logging_config import setup_logging
from models import Headline
from pipeline.ai_news import enrich_ai_classification, is_ai_source, is_material_ai_update
from pipeline.classify_cache import cache_classification, get_cached_classification, prune_classification_cache
from pipeline.freshness import is_fresh
from pipeline.noise import is_obvious_noise
from pipeline.prioritize import composite_score

logger = setup_logging()

FILTER_SYSTEM_PROMPT = """You are a ruthless filter for a professional stock/AI/macro X account. Most news is NOISE. Default to relevant=false.

Return strict JSON per item:
{
  "relevant": bool,
  "relevance_score": 0.0-1.0,
  "tradeable": bool,
  "tickers": ["NVDA"],
  "impact": "high"|"med"|"low",
  "category": "earnings"|"macro"|"ai"|"geopolitics"|"ipo"|"regulatory"|"other",
  "angle": "one sentence why a trader would care",
  "key_facts": ["fact with number if any"]
}

## relevant=true ONLY for material market movers:
- Earnings, guidance, revenue/profit surprises (with numbers or clear beat/miss)
- Fed, CPI, PPI, NFP, GDP, Treasury auctions — macro DATA releases
- M&A, buybacks, dividends, major layoffs, CEO exits at large caps
- SEC filings with material terms (8-K earnings, deals, financing)
- IPO pricing, major funding rounds for public-market peers
- Tariffs, sanctions, antitrust rulings affecting public companies

## AI company news — relevant=true for material product updates:
Pass AI stories when a major lab ships something new or meaningfully upgrades a product.
- New model releases or versions (GPT, Claude, Gemini, Llama, etc.)
- New capabilities, features, APIs, agents, or tools from OpenAI, Anthropic, Google, Meta, Microsoft, Amazon, Nvidia, Apple
- Major AI product launches tied to public companies (Copilot, Gemini, Meta AI, etc.)
- Significant funding or partnership news for OpenAI, Anthropic when it moves related stocks
Set category="ai", impact="high" for flagship model launches, "med" for feature updates.
Include tickers: MSFT (OpenAI), GOOGL (Google/Gemini), META (Meta/Llama), AMZN (Anthropic/AWS), NVDA, AAPL as appropriate.
tradeable=true for category ai when impact is high or med.

## relevant=false (noise) — reject these:
- Product demos, minor UI tweaks, and small app updates without a real AI capability change
- "AI will change everything" trend pieces, opinion, explainers, listicles, reviews
- Minor partnerships, awards, marketing hires, conference appearances
- Celebrity/crypto meme stories, human interest
- Rehashed news without new data ("markets watch Fed" with no new info)
- Generic geopolitics unless oil/rates/supply chain for public cos
- PR fluff, sponsored content tone

## Scoring
- relevance_score 0.9+: hard data (numbers, filings, central bank decision)
- 0.7-0.89: clear tradeable event but thinner detail
- below 0.7: reject (set relevant=false)
- tradeable=true only if a reasonable trader would act or reprice risk

Be strict. When in doubt, relevant=false. JSON array only."""


def _call_claude(system: str, user: str, model: str, retry: bool = True, max_tokens: int = 4096) -> str | None:
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set, skipping filter")
        return None

    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text
    except Exception as e:
        logger.error("Claude API error: %s", e)
        if retry:
            logger.info("Retrying Claude call once...")
            return _call_claude(system, user, model, retry=False, max_tokens=max_tokens)
        return None


def _parse_json_array(text: str) -> list[dict] | None:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError as e:
        logger.warning("Malformed LLM JSON: %s", e)
    return None


def _build_batch_prompt(headlines: list[Headline], watchlist: list[str]) -> str:
    watchlist_str = ", ".join(watchlist) if watchlist else "none (only pass high-impact stories)"
    lines = [
        f"User watchlist: {watchlist_str}",
        "User wants tech & stock news: specific companies, earnings, AI product launches, tickers.",
        "Deprioritize vague wire headlines (market wrap, stocks rise/fall) unless they name a company or data release.",
        "Without watchlist: pass high-impact stock/tech stories OR material AI product news.",
        "",
    ]
    for i, h in enumerate(headlines):
        lines.append(f"[{i}] Source: {h.source}")
        lines.append(f"Title: {h.title}")
        if h.summary:
            lines.append(f"Summary: {h.summary[:300]}")
        lines.append("")
    lines.append(f"Return a JSON array of exactly {len(headlines)} objects, one per item in order.")
    return "\n".join(lines)


def _passes_hard_filter(classification: dict, watchlist: list[str], headline: Headline | None = None) -> bool:
    """Code-level gate after LLM — strict for markets, relaxed for AI product news."""
    if not classification.get("relevant"):
        return False

    category = classification.get("category", "other")
    score = float(classification.get("relevance_score", 0))
    min_score = MIN_AI_RELEVANCE_SCORE if category == "ai" else MIN_RELEVANCE_SCORE
    if score < min_score:
        return False

    impact = classification.get("impact", "low")
    if impact == "low":
        return False

    tickers = [t.upper() for t in classification.get("tickers", [])]
    watchlist_upper = [w.upper() for w in watchlist]
    on_watchlist = bool(watchlist_upper and any(t in watchlist_upper for t in tickers))

    # AI product news — allow without forcing stock-trade framing
    if category == "ai":
        if classification.get("tradeable") is False and impact != "high":
            return False
        if impact == "med" and score < MIN_RELEVANCE_SCORE and not on_watchlist:
            return False
        text = ""
        if headline:
            text = f"{headline.title} {headline.summary}"
        if not tickers and headline and not is_material_ai_update(text) and not is_ai_source(headline):
            return False
        return True

    if classification.get("tradeable") is False:
        return False

    # med impact: require watchlist hit OR score >= 0.85
    if impact == "med" and not on_watchlist and score < 0.85:
        return False

    # "other" category needs high impact and strong score
    if category == "other" and (impact != "high" or score < 0.8):
        return False

    # Need at least one ticker OR macro/geopolitics with high impact
    macro_cats = {"macro", "geopolitics", "regulatory"}
    if not tickers and category not in macro_cats:
        return False

    return True


def _discard_headline(headline: Headline, reason: str) -> None:
    logger.debug("Discarded headline %s: %s", headline.id, reason)
    with get_session() as session:
        row = session.get(Headline, headline.id)
        if row:
            row.status = "discarded"
            session.add(row)
            session.commit()


def _apply_classification(
    headline: Headline,
    classification: dict[str, Any],
    watchlist: list[str],
    results: list[tuple[Headline, dict]],
) -> None:
    """Run hard filter and mark headline filtered or discarded."""
    classification = enrich_ai_classification(classification, headline)

    if not _passes_hard_filter(classification, watchlist, headline):
        _discard_headline(headline, "failed hard filter")
        return

    results.append((headline, classification))
    with get_session() as session:
        row = session.get(Headline, headline.id)
        if row:
            row.status = "filtered"
            session.add(row)
            session.commit()


def filter_headlines(headlines: list[Headline]) -> list[tuple[Headline, dict]]:
    """Filter headlines via pre-check + cache + Claude Haiku. Returns (headline, classification) pairs."""
    if not headlines:
        return []

    watchlist = get_setting("watchlist", [])
    results: list[tuple[Headline, dict]] = []

    # Pre-filter obvious noise (no API cost)
    candidates: list[Headline] = []
    for h in headlines:
        if not is_fresh(h.published_at):
            _discard_headline(h, f"story older than freshness window")
            continue
        noise_reason = is_obvious_noise(h)
        if noise_reason:
            _discard_headline(h, f"pre-filter: {noise_reason}")
            continue
        candidates.append(h)

    if not candidates:
        logger.info("All headlines removed by pre-filter")
        return []

    cache_hits = 0
    need_llm: list[Headline] = []
    for h in candidates:
        cached = get_cached_classification(h.title, h.source)
        if cached is not None:
            cache_hits += 1
            _apply_classification(h, cached, watchlist, results)
        else:
            need_llm.append(h)

    for batch_start in range(0, len(need_llm), 10):
        batch = need_llm[batch_start : batch_start + 10]
        prompt = _build_batch_prompt(batch, watchlist)
        raw = _call_claude(FILTER_SYSTEM_PROMPT, prompt, FILTER_MODEL)
        if not raw:
            continue

        parsed = _parse_json_array(raw)
        if not parsed:
            logger.warning("Skipping batch due to unparseable filter response")
            continue

        for i, h in enumerate(batch):
            if i >= len(parsed):
                break
            classification: dict[str, Any] = parsed[i]
            cache_classification(h.title, h.source, classification)
            _apply_classification(h, classification, watchlist, results)

    prune_classification_cache()

    # Sort by composite score (tech/stock/AI sources boosted)
    results.sort(key=lambda x: composite_score(x[0], x[1]), reverse=True)

    logger.info(
        "Filter kept %d/%d headlines (pre-filter %d, cache hits %d, Haiku %d)",
        len(results),
        len(headlines),
        len(candidates),
        cache_hits,
        len(need_llm),
    )
    return results
