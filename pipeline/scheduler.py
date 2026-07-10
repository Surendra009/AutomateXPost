"""Background pipeline scheduler."""

import asyncio
import threading
from datetime import datetime

from config import (
    MARKET_CLOSE_HOUR,
    MARKET_HOURS_INTERVAL_SECONDS,
    MAX_DRAFTS_PER_CYCLE,
    MAX_HEADLINES_PER_CYCLE,
    PIPELINE_INTERVAL_SECONDS,
    PREMARKET_START_HOUR,
    SCHEDULED_POST_CHECK_SECONDS,
)
from database import get_setting, set_setting
from logging_config import setup_logging
from pipeline.alerting import check_pipeline_health, send_alert
from pipeline.analytics import refresh_post_metrics
from pipeline.company_news import process_company_news
from pipeline.cycle_context import cycle_max_news_age
from pipeline.draft import draft_posts
from pipeline.draft_budget import DraftBudget
from pipeline.earnings import process_earnings
from pipeline.earnings_enrich import reset_earnings_enrich_stats
from pipeline.earnings_parse import reset_earnings_highlight_budget
from pipeline.feedback import feedback_stats
from pipeline.filter import filter_headlines
from pipeline.freshness import discard_stale_headlines
from pipeline.ingest import get_unfiltered_headlines, ingest_headlines
from pipeline.macro_calendar import process_macro_calendar
from pipeline.prioritize import select_diverse_for_drafting, select_headlines_for_filter
from pipeline.stale import expire_stale_drafts
from pipeline.schedule import (
    CATCHUP_SETTING_KEY,
    evaluate_schedule,
    is_market_hours,
    local_now,
    pipeline_interval_seconds,
    schedule_status,
)
from pipeline.sec_filings import clear_sec_feed_cache, process_sec_filings
from pipeline.scheduled_posts import process_scheduled_posts
from pipeline.earnings_calendar import get_earnings_pipeline_summary
from pipeline.earnings_enrich import earnings_enrich_summary, reset_earnings_enrich_stats
from pipeline.discord import notify_discord_new_drafts
from pipeline.push import notify_new_drafts
from pipeline.teams import notify_teams_new_drafts

logger = setup_logging()

_pipeline_task: asyncio.Task | None = None
_scheduled_task: asyncio.Task | None = None
_manual_run_task: asyncio.Task | None = None
_pipeline_running = False
_pipeline_started_at: datetime | None = None
_pipeline_lock = threading.Lock()

_PIPELINE_STUCK_SECONDS = 900  # 15 min — reset flag so UI/API recover


def _maybe_reset_stuck_pipeline() -> None:
    global _pipeline_running, _pipeline_started_at
    if not _pipeline_running or not _pipeline_started_at:
        return
    elapsed = (datetime.utcnow() - _pipeline_started_at).total_seconds()
    if elapsed > _PIPELINE_STUCK_SECONDS:
        logger.warning("Pipeline running flag stale (%.0fs) — resetting", elapsed)
        _pipeline_running = False
        _pipeline_started_at = None


def get_pipeline_status(*, lightweight: bool = False) -> dict:
    """Return last pipeline run metadata for the settings UI."""
    _maybe_reset_stuck_pipeline()
    ingest_by_source = get_setting("pipeline_last_ingest_by_source", {})
    if not isinstance(ingest_by_source, dict):
        ingest_by_source = {}
    try:
        sched = schedule_status()
    except Exception as exc:
        logger.warning("schedule_status failed: %s", exc)
        sched = {"error": str(exc)[:120]}
    status = {
        "running": _pipeline_running,
        "last_run_at": get_setting("pipeline_last_run_at"),
        "last_ingest_count": get_setting("pipeline_last_ingest_count", 0),
        "last_drafts_created": get_setting("pipeline_last_drafts_created", 0),
        "last_filter_kept": get_setting("pipeline_last_filter_kept", 0),
        "last_expired": get_setting("pipeline_last_expired", 0),
        "last_error": get_setting("pipeline_last_error"),
        "last_ingest_by_source": ingest_by_source,
        "schedule": sched,
    }
    if lightweight:
        status["earnings_enrich"] = earnings_enrich_summary()
        return status

    try:
        status["news_sources"] = _active_news_sources()
    except Exception as exc:
        logger.warning("news_sources failed: %s", exc)
        status["news_sources"] = []
    status["finnhub"] = get_finnhub_status()
    try:
        status["earnings"] = get_earnings_pipeline_summary()
    except Exception as exc:
        logger.warning("earnings summary failed: %s", exc)
        status["earnings"] = {"configured": False, "reporting_today": 0, "upcoming": []}
    status["earnings_enrich"] = earnings_enrich_summary()
    try:
        status["feedback"] = feedback_stats()
    except Exception as exc:
        logger.warning("feedback_stats failed: %s", exc)
        status["feedback"] = {}
    return status


def _active_news_sources() -> list[dict]:
    from config import AI_RSS_FEEDS, RSS_FEEDS, WEB_SEARCH_ENABLED
    from pipeline.finnhub_api import get_finnhub_key

    sources = [{"name": name, "type": "rss", "enabled": True} for name, _ in RSS_FEEDS]
    sources.extend({"name": name, "type": "ai", "enabled": True} for name, _ in AI_RSS_FEEDS)
    sources.append({
        "name": "Web Search (Google News)",
        "type": "search",
        "enabled": WEB_SEARCH_ENABLED,
        "hint": "Primary for earnings, mergers, company news",
    })
    fh_ok = bool(get_finnhub_key())
    sources.append({
        "name": "Finnhub Earnings",
        "type": "api",
        "enabled": fh_ok,
        "hint": "Supplement" if fh_ok else "Optional — FINNHUB_KEY",
    })
    sources.append({
        "name": "Finnhub Macro",
        "type": "api",
        "enabled": fh_ok,
        "hint": "Supplement" if fh_ok else "Optional — FINNHUB_KEY",
    })
    sources.append({
        "name": "Finnhub Company",
        "type": "api",
        "enabled": fh_ok,
        "hint": "Supplement + watchlist" if fh_ok else "Optional — FINNHUB_KEY",
    })
    sources.append({
        "name": "SEC 8-K (structured)",
        "type": "api",
        "enabled": True,
    })
    return sources


def get_finnhub_status() -> dict:
    cached = get_setting("finnhub_last_test")
    if isinstance(cached, dict):
        return cached
    return {}


def _save_cycle_stats(
    *,
    ingest_count: int = 0,
    drafts_created: int = 0,
    filter_kept: int = 0,
    expired: int = 0,
    error: str | None = None,
    ingest_by_source: dict | None = None,
) -> None:
    set_setting("pipeline_last_run_at", datetime.utcnow().isoformat())
    set_setting("pipeline_last_ingest_count", ingest_count)
    set_setting("pipeline_last_drafts_created", drafts_created)
    set_setting("pipeline_last_filter_kept", filter_kept)
    set_setting("pipeline_last_expired", expired)
    set_setting("pipeline_last_error", error)
    if ingest_by_source is not None:
        set_setting("pipeline_last_ingest_by_source", ingest_by_source)


async def run_pipeline_cycle(*, force: bool = False) -> dict:
    """Single pipeline cycle: expire → ingest → structured drafts → filter → LLM draft."""
    return await asyncio.to_thread(_run_pipeline_cycle, force=force)


def trigger_pipeline_cycle(*, force: bool = False) -> dict:
    """Schedule a pipeline cycle on the running event loop; returns immediately."""
    global _manual_run_task

    if _pipeline_running:
        return {"started": False, "reason": "already_running", **get_pipeline_status(lightweight=True)}

    async def _run() -> None:
        try:
            await run_pipeline_cycle(force=force)
        except Exception as exc:
            logger.error("Background pipeline cycle failed: %s", exc, exc_info=True)

    _manual_run_task = asyncio.get_running_loop().create_task(_run())
    return {"started": True, "running": True}


def _run_pipeline_cycle(*, force: bool = False) -> dict:
    global _pipeline_running, _pipeline_started_at

    if not _pipeline_lock.acquire(blocking=False):
        logger.debug("Pipeline cycle skipped — lock held by another cycle")
        return get_pipeline_status(lightweight=True)

    try:
        if _pipeline_running:
            return get_pipeline_status(lightweight=True)

        decision = evaluate_schedule(force=force)
        if not decision.run:
            logger.debug("Pipeline skipped: %s", decision.reason)
            set_setting("pipeline_last_schedule_skip", decision.reason)
            return get_pipeline_status(lightweight=True)

        _pipeline_running = True
        _pipeline_started_at = datetime.utcnow()
        ingest_count = 0
        expired = 0
        budget = DraftBudget()
        cycle_start = datetime.utcnow()

        try:
            with cycle_max_news_age(decision.max_news_age_hours):
                if not get_setting("pipeline_enabled", True):
                    logger.debug("Pipeline disabled, skipping cycle")
                    _save_cycle_stats()
                    return get_pipeline_status(lightweight=True)

                paused_until = get_setting("paused_until")
                if paused_until:
                    try:
                        pause_dt = datetime.fromisoformat(paused_until)
                        if datetime.utcnow() < pause_dt:
                            logger.debug("Pipeline paused until %s", paused_until)
                            _save_cycle_stats()
                            return get_pipeline_status(lightweight=True)
                    except ValueError:
                        pass

                logger.info("Pipeline cycle starting (%s)", decision.mode)
                reset_earnings_highlight_budget()
                reset_earnings_enrich_stats()
                clear_sec_feed_cache()
                expired = expire_stale_drafts()
                discarded = discard_stale_headlines()
                ingest_count, ingest_by_source = ingest_headlines()

                earnings_ingested, _ = process_earnings(budget)
                if earnings_ingested:
                    ingest_count += earnings_ingested
                    ingest_by_source["Finnhub Earnings"] = earnings_ingested

                macro_ingested, _ = process_macro_calendar(budget)
                if macro_ingested:
                    ingest_count += macro_ingested
                    ingest_by_source["Finnhub Macro"] = macro_ingested

                sec_ingested, _ = process_sec_filings(budget)
                if sec_ingested:
                    ingest_count += sec_ingested
                    ingest_by_source["SEC 8-K (structured)"] = sec_ingested

                company_ingested, _ = process_company_news(budget)
                if company_ingested:
                    ingest_count += company_ingested
                    ingest_by_source["Finnhub Company"] = company_ingested

                headlines = get_unfiltered_headlines(limit=MAX_HEADLINES_PER_CYCLE * 2)
                headlines = select_headlines_for_filter(headlines, MAX_HEADLINES_PER_CYCLE)
                filter_kept = 0
                if headlines and budget.remaining > 0:
                    filtered = filter_headlines(headlines)
                    filter_kept = len(filtered)
                    filtered = select_diverse_for_drafting(filtered, budget.remaining * 2)
                    if filtered:
                        draft_posts(filtered, budget)

                _save_cycle_stats(
                    ingest_count=ingest_count,
                    drafts_created=budget.created,
                    filter_kept=filter_kept,
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
                    budget.created,
                    expired,
                    discarded,
                )

                if budget.created:
                    notify_new_drafts(budget.created)
                    notify_discord_new_drafts(cycle_start)
                    notify_teams_new_drafts(cycle_start)

                check_pipeline_health(budget.created, None)
                refresh_post_metrics()

        except Exception as e:
            logger.error("Pipeline cycle error: %s", e, exc_info=True)
            send_alert("Pipeline cycle failed", str(e), level="error")
            _save_cycle_stats(
                ingest_count=ingest_count,
                drafts_created=budget.created,
                expired=expired,
                error=str(e),
            )
            check_pipeline_health(budget.created, str(e))
        finally:
            _pipeline_running = False
            _pipeline_started_at = None

        return get_pipeline_status(lightweight=True)
    finally:
        _pipeline_lock.release()


async def pipeline_loop(interval: int = 300) -> None:
    """Run pipeline on a dynamic interval (faster during market hours)."""
    while True:
        decision = evaluate_schedule()
        if decision.run:
            await run_pipeline_cycle()
        else:
            logger.debug("Pipeline tick skipped: %s", decision.reason)
            set_setting("pipeline_last_schedule_skip", decision.reason)
        sleep_s = pipeline_interval_seconds()
        await asyncio.sleep(sleep_s)


async def scheduled_post_loop() -> None:
    """Check for due scheduled posts every minute."""
    while True:
        try:
            process_scheduled_posts()
        except Exception as exc:
            logger.warning("Scheduled post loop error: %s", exc)
        await asyncio.sleep(SCHEDULED_POST_CHECK_SECONDS)


def start_pipeline(interval: int = 300) -> asyncio.Task:
    global _pipeline_task, _scheduled_task
    if _pipeline_task and not _pipeline_task.done():
        return _pipeline_task
    _pipeline_task = asyncio.create_task(pipeline_loop(interval))
    if not _scheduled_task or _scheduled_task.done():
        _scheduled_task = asyncio.create_task(scheduled_post_loop())
    logger.info("Pipeline started (base interval=%ds)", interval)
    return _pipeline_task


def stop_pipeline() -> None:
    global _pipeline_task, _scheduled_task
    if _pipeline_task and not _pipeline_task.done():
        _pipeline_task.cancel()
        _pipeline_task = None
    if _scheduled_task and not _scheduled_task.done():
        _scheduled_task.cancel()
        _scheduled_task = None
    logger.info("Pipeline stopped")
