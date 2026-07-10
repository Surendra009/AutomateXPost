from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import select, or_

from auth import (
    authenticate,
    check_action_rate_limit,
    check_login_rate_limit,
    clear_session_cookie,
    create_session_token,
    record_login_attempt,
    require_auth,
    set_session_cookie,
)
from config import REJECTION_REASONS
from database import get_all_settings, get_session, get_setting, set_setting
from logging_config import setup_logging
from models import Draft, Headline, Post
from pipeline.analytics import analytics_summary, refresh_post_metrics
from pipeline.assistant import chat_search
from pipeline.draft_lane import draft_lane, lane_counts
from pipeline.llm import chat_llm_status
from pipeline.draft import regenerate_draft
from pipeline.feedback import record_rejection
from pipeline.freshness import discard_stale_headlines, format_age, age_minutes, is_fresh
from pipeline.finnhub_api import test_finnhub_connection
from pipeline.post import PostingError, get_today_stats, publish_draft
from pipeline.discord import send_discord_test_message, discord_configured
from pipeline.llm_providers import llm_status
from pipeline.teams import send_teams_test_message, teams_configured
from pipeline.push import (
    get_vapid_public_key,
    push_configured,
    remove_subscription,
    save_subscription,
)
from pipeline.scheduler import get_finnhub_status, get_pipeline_status, trigger_pipeline_cycle
from pipeline.dedup_mode import DEDUP_MODE_LABELS, get_dedup_mode
from pipeline.queue_dedup import dedupe_pending_drafts
from pipeline.stale import expire_stale_drafts

logger = setup_logging()
router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    password: str


class ApproveRequest(BaseModel):
    text: Optional[str] = None
    scheduled_at: Optional[str] = None  # ISO datetime — post later


class RejectRequest(BaseModel):
    reason: str = "other"
    note: Optional[str] = None


class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: dict


class ChatRequest(BaseModel):
    message: str
    fetch_news: bool = True


class SettingsPatch(BaseModel):
    pipeline_enabled: Optional[bool] = None
    daily_post_cap: Optional[int] = None
    cooldown_minutes: Optional[int] = None
    watchlist: Optional[list[str]] = None
    search_topics: Optional[list[str]] = None
    paused_until: Optional[str] = None
    dedup_mode: Optional[str] = None
    allow_hashtags: Optional[bool] = None
    push_enabled: Optional[bool] = None
    discord_enabled: Optional[bool] = None
    teams_enabled: Optional[bool] = None


def _draft_to_dict(draft: Draft, headline: Headline | None) -> dict:
    is_seed = bool(headline and "example.com" in (headline.url or ""))
    story_age = format_age(headline.published_at) if headline else None
    story_mins = age_minutes(headline.published_at) if headline else None
    draft_mins = age_minutes(draft.created_at)

    return {
        "id": draft.id,
        "text": draft.text,
        "format": draft.format,
        "impact": draft.impact,
        "category": draft.category,
        "tickers": draft.tickers.split(",") if draft.tickers else [],
        "confidence": draft.confidence,
        "status": draft.status,
        "scheduled_at": draft.scheduled_at.isoformat() if draft.scheduled_at else None,
        "post_error": draft.post_error,
        "created_at": draft.created_at.isoformat(),
        "age": format_age(draft.created_at),
        "draft_age_minutes": draft_mins,
        "story_age": story_age,
        "story_age_minutes": story_mins,
        "story_fresh": is_fresh(headline.published_at, draft.category) if headline else True,
        "is_seed": is_seed,
        "headline": {
            "source": headline.source if headline else "",
            "url": headline.url if headline else "",
            "title": headline.title if headline else "",
            "published_at": headline.published_at.isoformat() if headline else None,
        } if headline else None,
    }


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    check_login_rate_limit(request)
    if not authenticate(body.password):
        record_login_attempt(request)
        raise HTTPException(status_code=401, detail="Invalid password")
    token = create_session_token()
    set_session_cookie(response, token)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    require_auth(request)
    return {"authenticated": True}


@router.post("/chat")
def post_chat(request: Request, body: ChatRequest):
    require_auth(request)
    check_action_rate_limit(request, "chat", max_calls=40, window_seconds=60)
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    if len(message) > 500:
        raise HTTPException(status_code=400, detail="message too long (max 500)")
    return chat_search(message, fetch_news=body.fetch_news)


@router.get("/chat/status")
def get_chat_status(request: Request):
    require_auth(request)
    return chat_llm_status()


@router.get("/queue")
def get_queue(request: Request):
    require_auth(request)
    expire_stale_drafts()
    discard_stale_headlines()
    hidden_duplicates = 0
    with get_session() as session:
        drafts = list(
            session.exec(
                select(Draft)
                .where(or_(Draft.status == "pending", Draft.status == "scheduled"))
                .order_by(Draft.created_at.desc())
            ).all()
        )
        headline_ids = [d.headline_id for d in drafts]
        headlines: dict[int, Headline] = {}
        if headline_ids:
            rows = session.exec(select(Headline).where(Headline.id.in_(headline_ids))).all()
            headlines = {h.id: h for h in rows if h.id is not None}

        from pipeline.dedup_mode import dedup_at_queue

        pending_only = [d for d in drafts if d.status == "pending"]
        if dedup_at_queue() and pending_only:
            pairs, hidden_duplicates = dedupe_pending_drafts(pending_only, headlines)
            scheduled = [
                (d, headlines[d.headline_id])
                for d in drafts
                if d.status == "scheduled" and d.headline_id in headlines
            ]
            pairs = pairs + scheduled
        else:
            pairs = [(d, headlines.get(d.headline_id)) for d in drafts if d.headline_id in headlines]

        pairs.sort(
            key=lambda p: (
                1 if (p[1] and is_fresh(p[1].published_at, p[0].category)) else 0,
                {"high": 3, "med": 2, "low": 1}.get(p[0].impact or "med", 2),
                p[1].published_at.timestamp() if p[1] and p[1].published_at else 0,
                p[0].created_at.timestamp() if p[0].created_at else 0,
            ),
            reverse=True,
        )

        result = []
        for d, h in pairs:
            if not h:
                continue
            item = _draft_to_dict(d, h)
            item["lane"] = draft_lane(d.category, d.tickers)
            result.append(item)

    counts = lane_counts(result)

    return {
        "drafts": result,
        "count": len(result),
        "counts": counts,
        "dedup_mode": get_dedup_mode(),
        "hidden_duplicates": hidden_duplicates,
        "rejection_reasons": list(REJECTION_REASONS),
    }


@router.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: int, request: Request, body: ApproveRequest = ApproveRequest()):
    require_auth(request)
    daily_cap = get_setting("daily_post_cap", 20)
    cooldown = get_setting("cooldown_minutes", 5)

    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status not in ("pending", "scheduled"):
            raise HTTPException(status_code=400, detail=f"Draft is {draft.status}, not pending")

        if body.text:
            draft.text = body.text

        if body.scheduled_at:
            try:
                sched = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
                if sched.tzinfo:
                    sched = sched.replace(tzinfo=None) - sched.utcoffset()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid scheduled_at") from exc
            if sched <= datetime.utcnow():
                raise HTTPException(status_code=400, detail="scheduled_at must be in the future")
            draft.status = "scheduled"
            draft.scheduled_at = sched
            draft.post_error = None
            session.add(draft)
            session.commit()
            return {"ok": True, "scheduled": True, "scheduled_at": sched.isoformat()}

    try:
        post = publish_draft(draft, text=body.text, daily_cap=daily_cap, cooldown_minutes=cooldown)
        return {
            "ok": True,
            "tweet_id": post.tweet_id,
            "thread_count": len(post.thread_tweet_ids.split(",")) if post.thread_tweet_ids else 1,
            "tweet_url": f"https://x.com/i/status/{post.tweet_id}" if not post.tweet_id.startswith("dry_run") else None,
        }
    except PostingError as e:
        with get_session() as session:
            row = session.get(Draft, draft_id)
            if row:
                row.post_error = str(e)
                session.add(row)
                session.commit()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/drafts/{draft_id}/reject")
def reject_draft(draft_id: int, request: Request, body: RejectRequest = RejectRequest()):
    require_auth(request)
    if body.reason not in REJECTION_REASONS:
        raise HTTPException(status_code=400, detail="Invalid rejection reason")
    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status not in ("pending", "scheduled"):
            raise HTTPException(status_code=400, detail=f"Draft is {draft.status}, not pending")
        draft.status = "rejected"
        draft.scheduled_at = None
        session.add(draft)
        session.commit()

    record_rejection(draft_id, reason=body.reason, note=body.note or "")
    return {"ok": True}


@router.post("/drafts/{draft_id}/regenerate")
def regenerate_draft_route(draft_id: int, request: Request):
    require_auth(request)
    draft, err = regenerate_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=400, detail=err or "Could not regenerate draft")
    with get_session() as session:
        headline = session.get(Headline, draft.headline_id)
    return {"ok": True, "draft": _draft_to_dict(draft, headline)}


@router.post("/drafts/clear-samples")
def clear_sample_drafts(request: Request):
    """Remove pending sample/seed drafts (example.com URLs)."""
    require_auth(request)
    cleared = 0
    with get_session() as session:
        rows = session.exec(
            select(Draft, Headline)
            .join(Headline, Draft.headline_id == Headline.id)
            .where(Draft.status.in_(("pending", "scheduled")))
        ).all()
        for draft, headline in rows:
            if "example.com" in (headline.url or ""):
                draft.status = "stale"
                session.add(draft)
                cleared += 1
        session.commit()
    return {"ok": True, "cleared": cleared}


@router.get("/history")
def get_history(request: Request):
    require_auth(request)
    stats = get_today_stats()
    metrics = analytics_summary()

    with get_session() as session:
        posted_drafts = session.exec(
            select(Draft, Post)
            .join(Post, Post.draft_id == Draft.id)
            .order_by(Post.posted_at.desc())
            .limit(50)
        ).all()

        posted = []
        for draft, post in posted_drafts:
            tweet_id = post.tweet_id or ""
            posted.append({
                "id": draft.id,
                "text": draft.text,
                "posted_at": post.posted_at.isoformat() if post.posted_at else None,
                "tweet_id": tweet_id,
                "tweet_url": f"https://x.com/i/status/{tweet_id}" if tweet_id and not tweet_id.startswith("dry_run") else None,
                "likes": post.like_count or 0,
                "retweets": post.retweet_count or 0,
                "thread_count": len(post.thread_tweet_ids.split(",")) if post.thread_tweet_ids else 1,
            })

        rejected = session.exec(
            select(Draft)
            .where(Draft.status == "rejected")
            .order_by(Draft.created_at.desc())
            .limit(50)
        ).all()

        rejected_list = [
            {"id": d.id, "text": d.text, "created_at": d.created_at.isoformat()}
            for d in rejected
        ]

    return {
        "posted": posted,
        "rejected": rejected_list,
        "stats": stats,
        "analytics": metrics,
    }


@router.get("/settings")
def get_settings_route(request: Request):
    require_auth(request)
    from config import DEFAULT_SETTINGS, get_settings as app_config
    from logging_config import setup_logging

    logger = setup_logging()
    settings: dict = dict(DEFAULT_SETTINGS)

    try:
        settings = get_all_settings()
    except Exception as exc:
        logger.error("get_all_settings failed: %s", exc, exc_info=True)

    try:
        pipeline = get_pipeline_status(lightweight=True)
        pipeline["finnhub"] = get_finnhub_status()
        try:
            from pipeline.earnings_calendar import get_earnings_pipeline_summary

            pipeline["earnings"] = get_earnings_pipeline_summary()
        except Exception as exc:
            logger.warning("earnings panel failed: %s", exc)
            pipeline["earnings"] = {
                "configured": False,
                "watchlist_count": len(settings.get("watchlist") or []),
                "reporting_today": 0,
                "upcoming": [],
            }
        settings["pipeline"] = pipeline
        settings["finnhub"] = pipeline.get("finnhub") or {}
    except Exception as exc:
        logger.error("pipeline status failed: %s", exc, exc_info=True)
        settings["pipeline"] = {"running": False, "last_error": str(exc)[:200]}
        settings["finnhub"] = {}

    try:
        settings["config"] = app_config()
    except Exception as exc:
        logger.warning("app config failed: %s", exc)
        settings["config"] = {"build": "?"}

    settings["push"] = {
        "configured": push_configured(),
        "public_key": get_vapid_public_key() if push_configured() else None,
    }
    settings["teams"] = {"configured": teams_configured()}
    settings["discord"] = {"configured": discord_configured()}

    try:
        settings["llm"] = llm_status()
    except Exception as exc:
        logger.warning("llm_status failed: %s", exc)
        settings["llm"] = {}

    try:
        settings["chat"] = chat_llm_status()
    except Exception as exc:
        logger.warning("chat_llm_status failed: %s", exc)
        settings["chat"] = {}

    return settings


@router.get("/push/vapid-public-key")
def push_vapid_key(request: Request):
    require_auth(request)
    if not push_configured():
        raise HTTPException(status_code=503, detail="Push not configured (set VAPID keys)")
    return {"public_key": get_vapid_public_key()}


@router.post("/push/subscribe")
def push_subscribe(request: Request, body: PushSubscribeRequest):
    require_auth(request)
    save_subscription(body.endpoint, body.keys.get("p256dh", ""), body.keys.get("auth", ""))
    return {"ok": True}


@router.post("/push/unsubscribe")
def push_unsubscribe(request: Request, body: PushSubscribeRequest):
    require_auth(request)
    remove_subscription(body.endpoint)
    return {"ok": True}


@router.post("/discord/test")
def discord_test(request: Request):
    require_auth(request)
    if not discord_configured():
        raise HTTPException(
            status_code=503,
            detail="Discord not configured — set DISCORD_WEBHOOK_URL on Railway and redeploy",
        )
    if not send_discord_test_message():
        raise HTTPException(status_code=502, detail="Discord webhook request failed")
    return {"ok": True}


@router.post("/teams/test")
def teams_test(request: Request):
    require_auth(request)
    if not teams_configured():
        raise HTTPException(
            status_code=503,
            detail="Teams not configured — set TEAMS_WEBHOOK_URL on Railway and redeploy",
        )
    if not send_teams_test_message():
        raise HTTPException(status_code=502, detail="Teams webhook request failed")
    return {"ok": True}


@router.get("/pipeline/status")
def pipeline_status(request: Request):
    require_auth(request)
    return get_pipeline_status(lightweight=True)


@router.get("/finnhub/test")
def finnhub_test(request: Request):
    require_auth(request)
    from database import set_setting

    result = test_finnhub_connection()
    set_setting("finnhub_last_test", result)
    return result


@router.post("/pipeline/run", status_code=202)
async def pipeline_run(request: Request):
    require_auth(request)
    check_action_rate_limit(request, "pipeline", max_calls=10, window_seconds=60)
    status = get_pipeline_status(lightweight=True)
    if status["running"]:
        raise HTTPException(status_code=409, detail="Pipeline is already running")
    return trigger_pipeline_cycle(force=True)


@router.post("/analytics/refresh")
def analytics_refresh(request: Request):
    require_auth(request)
    updated = refresh_post_metrics()
    return {"ok": True, "updated": updated, "analytics": analytics_summary()}


@router.patch("/settings")
def patch_settings(request: Request, body: SettingsPatch):
    require_auth(request)
    if body.pipeline_enabled is not None:
        set_setting("pipeline_enabled", body.pipeline_enabled)
    if body.daily_post_cap is not None:
        set_setting("daily_post_cap", body.daily_post_cap)
    if body.cooldown_minutes is not None:
        set_setting("cooldown_minutes", body.cooldown_minutes)
    if body.watchlist is not None:
        set_setting("watchlist", [t.upper().strip() for t in body.watchlist if t.strip()])
    if body.search_topics is not None:
        seen: set[str] = set()
        topics: list[str] = []
        for raw in body.search_topics:
            topic = " ".join(str(raw).strip().split())
            if not topic:
                continue
            key = topic.lower()
            if key in seen:
                continue
            seen.add(key)
            topics.append(topic[:80])
        set_setting("search_topics", topics)
    if body.paused_until == "":
        set_setting("paused_until", None)
    elif body.paused_until is not None:
        set_setting("paused_until", body.paused_until)
    if body.dedup_mode is not None:
        from pipeline.dedup_mode import DEDUP_MODES

        if body.dedup_mode not in DEDUP_MODES:
            raise HTTPException(status_code=400, detail="dedup_mode must be pipeline, queue, or off")
        set_setting("dedup_mode", body.dedup_mode)
    if body.allow_hashtags is not None:
        set_setting("allow_hashtags", body.allow_hashtags)
    if body.push_enabled is not None:
        set_setting("push_enabled", body.push_enabled)
    if body.discord_enabled is not None:
        set_setting("discord_enabled", body.discord_enabled)
    if body.teams_enabled is not None:
        set_setting("teams_enabled", body.teams_enabled)
    return get_all_settings()
