from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import select

from auth import (
    authenticate,
    check_login_rate_limit,
    clear_session_cookie,
    create_session_token,
    record_login_attempt,
    require_auth,
    set_session_cookie,
)
from database import get_all_settings, get_session, get_setting, set_setting
from logging_config import setup_logging
from models import Draft, Headline, Post
from pipeline.post import PostingError, get_today_stats, publish_draft
from pipeline.feedback import record_rejection
from pipeline.freshness import discard_stale_headlines, format_age, age_minutes, is_fresh
from pipeline.finnhub_api import test_finnhub_connection
from pipeline.scheduler import get_pipeline_status, run_pipeline_cycle
from pipeline.stale import expire_stale_drafts

logger = setup_logging()
router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    password: str


class ApproveRequest(BaseModel):
    text: Optional[str] = None


class SettingsPatch(BaseModel):
    pipeline_enabled: Optional[bool] = None
    daily_post_cap: Optional[int] = None
    cooldown_minutes: Optional[int] = None
    watchlist: Optional[list[str]] = None
    paused_until: Optional[str] = None


def _draft_to_dict(draft: Draft, headline: Headline | None) -> dict:
    is_seed = bool(headline and "example.com" in (headline.url or ""))
    story_age = format_age(headline.published_at) if headline else None
    story_mins = age_minutes(headline.published_at) if headline else None

    return {
        "id": draft.id,
        "text": draft.text,
        "format": draft.format,
        "impact": draft.impact,
        "category": draft.category,
        "tickers": draft.tickers.split(",") if draft.tickers else [],
        "confidence": draft.confidence,
        "status": draft.status,
        "created_at": draft.created_at.isoformat(),
        "age": format_age(draft.created_at),
        "story_age": story_age,
        "story_age_minutes": story_mins,
        "story_fresh": is_fresh(headline.published_at) if headline else True,
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


@router.get("/queue")
def get_queue(request: Request):
    require_auth(request)
    expire_stale_drafts()
    discard_stale_headlines()
    with get_session() as session:
        drafts = session.exec(
            select(Draft)
            .where(Draft.status == "pending")
            .order_by(Draft.created_at.desc())
        ).all()
        result = []
        for d in drafts:
            headline = session.get(Headline, d.headline_id)
            result.append(_draft_to_dict(d, headline))
    return {"drafts": result, "count": len(result)}


@router.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: int, request: Request, body: ApproveRequest = ApproveRequest()):
    require_auth(request)
    daily_cap = get_setting("daily_post_cap", 20)
    cooldown = get_setting("cooldown_minutes", 5)

    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status != "pending":
            raise HTTPException(status_code=400, detail=f"Draft is {draft.status}, not pending")

    try:
        post = publish_draft(draft, text=body.text, daily_cap=daily_cap, cooldown_minutes=cooldown)
        return {
            "ok": True,
            "tweet_id": post.tweet_id,
            "tweet_url": f"https://x.com/i/status/{post.tweet_id}" if not post.tweet_id.startswith("dry_run") else None,
        }
    except PostingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/drafts/{draft_id}/reject")
def reject_draft(draft_id: int, request: Request):
    require_auth(request)
    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status != "pending":
            raise HTTPException(status_code=400, detail=f"Draft is {draft.status}, not pending")
        draft.status = "rejected"
        session.add(draft)
        session.commit()

    record_rejection(draft_id)
    return {"ok": True}


@router.get("/history")
def get_history(request: Request):
    require_auth(request)
    stats = get_today_stats()

    with get_session() as session:
        posted_drafts = session.exec(
            select(Draft, Post)
            .join(Post, Post.draft_id == Draft.id)
            .order_by(Post.posted_at.desc())
            .limit(50)
        ).all()

        posted = []
        for draft, post in posted_drafts:
            posted.append({
                "id": draft.id,
                "text": draft.text,
                "posted_at": post.posted_at.isoformat(),
                "tweet_id": post.tweet_id,
                "tweet_url": f"https://x.com/i/status/{post.tweet_id}" if not post.tweet_id.startswith("dry_run") else None,
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
    }


@router.get("/settings")
def get_settings_route(request: Request):
    require_auth(request)
    from config import get_settings as app_config
    settings = get_all_settings()
    settings["config"] = app_config()
    settings["pipeline"] = get_pipeline_status()
    settings["finnhub"] = get_pipeline_status().get("finnhub") or {}
    return settings


@router.get("/pipeline/status")
def pipeline_status(request: Request):
    require_auth(request)
    return get_pipeline_status()


@router.get("/finnhub/test")
def finnhub_test(request: Request):
    require_auth(request)
    from database import set_setting

    result = test_finnhub_connection()
    set_setting("finnhub_last_test", result)
    return result


@router.post("/pipeline/run")
async def pipeline_run(request: Request):
    require_auth(request)
    status = get_pipeline_status()
    if status["running"]:
        raise HTTPException(status_code=409, detail="Pipeline is already running")
    return await run_pipeline_cycle(force=True)


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
    if body.paused_until is not None:
        set_setting("paused_until", body.paused_until)
    return get_all_settings()
