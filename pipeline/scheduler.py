"""Background pipeline scheduler."""

import asyncio
from datetime import datetime

from config import MAX_DRAFTS_PER_CYCLE, MAX_HEADLINES_PER_CYCLE, PIPELINE_INTERVAL_SECONDS
from database import get_setting, set_setting
from logging_config import setup_logging
from pipeline.cycle_context import cycle_max_news_age
from pipeline.draft import draft_posts
from pipeline.earnings import process_earnings
from pipeline.finnhub_api import test_finnhub_connection
from pipeline.filter import filter_headlines
from pipeline.freshness import discard_stale_headlines
from pipeline.ingest import get_unfiltered_headlines, ingest_headlines
from pipeline.prioritize import select_diverse_for_drafting, select_headlines_for_filter
from pipeline.schedule import CATCHUP_SETTING_KEY, evaluate_schedule, local_now, schedule_status
from pipeline.stale import expire_stale_drafts

logger = setup_logging()

_pipeline_task: asyncio.Task | None = None
_pipeline_running = False


def get_pipeline_status() -> dict:
    """Return last pipeline run metadata for the settings UI."""
    return {
        "running": _pipeline_running,
        "last_run_at": get_setting("pipeline_last_run_at"),
        "last_ingest_count": get_setting("pipeline_last_ingest_count", 0),
        "last_drafts_created": get_setting("pipeline_last_drafts_created", 0),
        "last_expired": get_setting("pipeline_last_expired", 0),
        "last_error": get_setting("pipeline_last_error"),
        "last_ingest_by_source": get_setting("pipeline_last_ingest_by_source", {}),
        "news_sources": _active_news_sources(),
        "finnhub": get_finnhub_status(),
        "schedule": schedule_status(),
    }


def _active_news_sources() -> list[dict]:
    from config import AI_RSS_FEEDS, RSS_FEEDS, SEC_EDGAR_8K_FEED
    from pipeline.finnhub_api import get_finnhub_key

    sources = [{"name": name, "type": "rss", "enabled": True} for name, _ in RSS_FEEDS]
    sources.extend({"name": name, "type": "ai", "enabled": True} for name, _ in AI_RSS_FEEDS)
    sources.append({"name": SEC_EDGAR_8K_FEED[0], "type": "rss", "enabled": True})
    fh_ok = bool(get_finnhub_key())
    sources.append({
        "name": "Finnhub Earnings",
        "type": "api",
        "enabled": fh_ok,
        "hint": None if fh_ok else "Set FINNHUB_KEY in Railway Variables",
    })
    sources.append({
        "name": "Finnhub (general + company)",
        "type": "api",
        "enabled": fh_ok,
        "hint": None if fh_ok else "Set FINNHUB_KEY in Railway Variables",
    })
    return sources


def get_finnhub_status() -> dict:
    from database import get_setting

    cached = get_setting("finnhub_last_test")
    return cached or {}


def _save_cycle_stats(
    *,
    ingest_count: int = 0,
    drafts_created: int = 0,
    expired: int = 0,
    error: str | None = None,
    ingest_by_source: dict | None = None,
) -> None:
    set_setting("pipeline_last_run_at", datetime.utcnow().isoformat())
    set_setting("pipeline_last_ingest_count", ingest_count)
    set_setting("pipeline_last_drafts_created", drafts_created)
    set_setting("pipeline_last_expired", expired)
    set_setting("pipeline_last_error", error)
    if ingest_by_source is not None:
        set_setting("pipeline_last_ingest_by_source", ingest_by_source)


async def run_pipeline_cycle(*, force: bool = False) -> dict:
    """Single pipeline cycle: expire → ingest → filter → draft (one Sonnet call per story)."""
    global _pipeline_running

    if _pipeline_running:
        return get_pipeline_status()

    decision = evaluate_schedule(force=force)
    if not decision.run:
        logger.debug("Pipeline skipped: %s", decision.reason)
        set_setting("pipeline_last_schedule_skip", decision.reason)
        return get_pipeline_status()

    _pipeline_running = True
    ingest_count = 0
    drafts_created = 0
    expired = 0

    try:
        with cycle_max_news_age(decision.max_news_age_hours):
            if not get_setting("pipeline_enabled", True):
                logger.debug("Pipeline disabled, skipping cycle")
                _save_cycle_stats()
                return get_pipeline_status()

            paused_until = get_setting("paused_until")
            if paused_until:
                try:
                    pause_dt = datetime.fromisoformat(paused_until)
                    if datetime.utcnow() < pause_dt:
                        logger.debug("Pipeline paused until %s", paused_until)
                        _save_cycle_stats()
                        return get_pipeline_status()
                except ValueError:
                    pass

            logger.info("Pipeline cycle starting (%s)", decision.mode)
            expired = expire_stale_drafts()
            discarded = discard_stale_headlines()
            ingest_count, ingest_by_source = ingest_headlines()
            finnhub_test = test_finnhub_connection()
            set_setting("finnhub_last_test", finnhub_test)
            earnings_ingested, earnings_drafts = process_earnings()
            if earnings_ingested:
                ingest_count += earnings_ingested
                ingest_by_source["Finnhub Earnings"] = earnings_ingested
            drafts_created = earnings_drafts

            headlines = get_unfiltered_headlines(limit=MAX_HEADLINES_PER_CYCLE * 2)
            headlines = select_headlines_for_filter(headlines, MAX_HEADLINES_PER_CYCLE)
            if headlines:
                filtered = filter_headlines(headlines)
                filtered = select_diverse_for_drafting(filtered, MAX_DRAFTS_PER_CYCLE * 2)
                if filtered:
                    drafts_created += draft_posts(filtered)

            _save_cycle_stats(
                ingest_count=ingest_count,
                drafts_created=drafts_created,
                expired=expired,
                ingest_by_source=ingest_by_source,
            )
            set_setting("pipeline_last_schedule_mode", decision.mode)
            set_setting("pipeline_last_schedule_skip", None)
            if decision.mode == "catchup":
                set_setting(CATCHUP_SETTING_KEY, local_now().isoformat())

            logger.info(
                "Pipeline cycle complete (%s, ingested=%d, drafts=%d, expired=%d, discarded=%d)",
                decision.mode,
                ingest_count,
                drafts_created,
                expired,
                discarded,
            )
    except Exception as e:
        logger.error("Pipeline cycle error: %s", e, exc_info=True)
        _save_cycle_stats(
            ingest_count=ingest_count,
            drafts_created=drafts_created,
            expired=expired,
            error=str(e),
        )
    finally:
        _pipeline_running = False

    return get_pipeline_status()


async def pipeline_loop(interval: int = 300) -> None:
    """Run pipeline every `interval` seconds (respects overnight schedule)."""
    while True:
        decision = evaluate_schedule()
        if decision.run:
            await run_pipeline_cycle()
        else:
            logger.debug("Pipeline tick skipped: %s", decision.reason)
            set_setting("pipeline_last_schedule_skip", decision.reason)
        await asyncio.sleep(interval)


def start_pipeline(interval: int = 300) -> asyncio.Task:
    global _pipeline_task
    if _pipeline_task and not _pipeline_task.done():
        return _pipeline_task
    _pipeline_task = asyncio.create_task(pipeline_loop(interval))
    logger.info("Pipeline started (interval=%ds)", interval)
    return _pipeline_task


def stop_pipeline() -> None:
    global _pipeline_task
    if _pipeline_task and not _pipeline_task.done():
        _pipeline_task.cancel()
        _pipeline_task = None
        logger.info("Pipeline stopped")
