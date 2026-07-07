"""LLM filter step — classify headline relevance."""

import json
from typing import Any

from sqlmodel import select

from config import ANTHROPIC_API_KEY, FILTER_MODEL, MIN_RELEVANCE_SCORE
from database import get_session, get_setting
from logging_config import setup_logging
from models import Headline
from pipeline.noise import is_obvious_noise

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
- AI news ONLY if it affects hyperscaler/chip demand or a public company's revenue

## relevant=false (noise) — reject these:
- Product demos, feature launches, app updates without revenue impact
- "AI will change everything" trend pieces, opinion, explainers
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
        "Without watchlist: only pass HIGH impact stories with hard data.",
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


def _passes_hard_filter(classification: dict, watchlist: list[str]) -> bool:
    """Code-level gate after LLM — strict."""
    if not classification.get("relevant"):
        return False

    score = float(classification.get("relevance_score", 0))
    if score < MIN_RELEVANCE_SCORE:
        return False

    if classification.get("tradeable") is False:
        return False

    impact = classification.get("impact", "low")
    if impact == "low":
        return False

    tickers = [t.upper() for t in classification.get("tickers", [])]
    watchlist_upper = [w.upper() for w in watchlist]
    on_watchlist = bool(watchlist_upper and any(t in watchlist_upper for t in tickers))

    # med impact: require watchlist hit OR score >= 0.85
    if impact == "med" and not on_watchlist and score < 0.85:
        return False

    # "other" category needs high impact and strong score
    if classification.get("category") == "other" and (impact != "high" or score < 0.8):
        return False

    # Need at least one ticker OR macro/geopolitics with high impact
    macro_cats = {"macro", "geopolitics", "regulatory"}
    if not tickers and classification.get("category") not in macro_cats:
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


def filter_headlines(headlines: list[Headline]) -> list[tuple[Headline, dict]]:
    """Filter headlines via pre-check + Claude Haiku. Returns (headline, classification) pairs."""
    if not headlines:
        return []

    watchlist = get_setting("watchlist", [])
    results: list[tuple[Headline, dict]] = []

    # Pre-filter obvious noise (no API cost)
    candidates: list[Headline] = []
    for h in headlines:
        noise_reason = is_obvious_noise(h)
        if noise_reason:
            _discard_headline(h, f"pre-filter: {noise_reason}")
            continue
        candidates.append(h)

    if not candidates:
        logger.info("All headlines removed by pre-filter")
        return []

    for batch_start in range(0, len(candidates), 10):
        batch = candidates[batch_start : batch_start + 10]
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

            if not _passes_hard_filter(classification, watchlist):
                _discard_headline(h, "failed hard filter")
                continue

            results.append((h, classification))
            with get_session() as session:
                row = session.get(Headline, h.id)
                if row:
                    row.status = "filtered"
                    session.add(row)
                    session.commit()

    # Sort by relevance_score desc, keep top N for drafting
    results.sort(key=lambda x: float(x[1].get("relevance_score", 0)), reverse=True)

    logger.info(
        "Filter kept %d/%d headlines (after pre-filter %d)",
        len(results),
        len(headlines),
        len(candidates),
    )
    return results
