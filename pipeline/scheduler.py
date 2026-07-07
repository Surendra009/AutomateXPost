"""Background pipeline scheduler."""

import asyncio
from datetime import datetime

from config import MAX_DRAFTS_PER_CYCLE, MAX_HEADLINES_PER_CYCLE, PIPELINE_INTERVAL_SECONDS
from database import get_setting, set_setting
from logging_config import setup_logging
from pipeline.draft import draft_posts
from pipeline.filter import filter_headlines
from pipeline.ingest import get_unfiltered_headlines, ingest_headlines
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
    }


def _save_cycle_stats(
    *,
    ingest_count: int = 0,
    drafts_created: int = 0,
    expired: int = 0,
    error: str | None = None,
) -> None:
    set_setting("pipeline_last_run_at", datetime.utcnow().isoformat())
    set_setting("pipeline_last_ingest_count", ingest_count)
    set_setting("pipeline_last_drafts_created", drafts_created)
    set_setting("pipeline_last_expired", expired)
    set_setting("pipeline_last_error", error)


async def run_pipeline_cycle() -> dict:
    """Single pipeline cycle: expire → ingest → filter → enrich → analyze → draft."""
    global _pipeline_running

    if _pipeline_running:
        return get_pipeline_status()

    _pipeline_running = True
    ingest_count = 0
    drafts_created = 0
    expired = 0

    try:
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

        logger.info("Pipeline cycle starting")
        expired = expire_stale_drafts()
        ingest_count = ingest_headlines()
        headlines = get_unfiltered_headlines(limit=MAX_HEADLINES_PER_CYCLE)
        if headlines:
            filtered = filter_headlines(headlines)
            filtered = filtered[: MAX_DRAFTS_PER_CYCLE * 2]
            if filtered:
                drafts_created = draft_posts(filtered)

        _save_cycle_stats(
            ingest_count=ingest_count,
            drafts_created=drafts_created,
            expired=expired,
        )
        logger.info(
            "Pipeline cycle complete (ingested=%d, drafts=%d, expired=%d)",
            ingest_count,
            drafts_created,
            expired,
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
    """Run pipeline every `interval` seconds."""
    while True:
        await run_pipeline_cycle()
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
