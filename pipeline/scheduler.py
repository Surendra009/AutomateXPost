"""Background pipeline scheduler."""

import asyncio
from datetime import datetime

from database import get_setting
from logging_config import setup_logging
from pipeline.draft import draft_posts
from pipeline.filter import filter_headlines
from pipeline.ingest import get_unfiltered_headlines, ingest_headlines

logger = setup_logging()

_pipeline_task: asyncio.Task | None = None


async def run_pipeline_cycle() -> None:
    """Single pipeline cycle: ingest → filter → enrich → analyze → draft."""
    if not get_setting("pipeline_enabled", True):
        logger.debug("Pipeline disabled, skipping cycle")
        return

    paused_until = get_setting("paused_until")
    if paused_until:
        try:
            pause_dt = datetime.fromisoformat(paused_until)
            if datetime.utcnow() < pause_dt:
                logger.debug("Pipeline paused until %s", paused_until)
                return
        except ValueError:
            pass

    logger.info("Pipeline cycle starting")
    try:
        ingest_headlines()
        headlines = get_unfiltered_headlines(limit=30)
        if headlines:
            filtered = filter_headlines(headlines)
            if filtered:
                draft_posts(filtered)
    except Exception as e:
        logger.error("Pipeline cycle error: %s", e, exc_info=True)
    logger.info("Pipeline cycle complete")


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
