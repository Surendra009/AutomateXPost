"""Post a draft to X (Twitter) with safety rails, retries, threads, and media."""

from __future__ import annotations

import io
import re
import time
from datetime import datetime, timedelta

import httpx
from sqlmodel import select

from config import (
    DRY_RUN,
    EARNINGS_STALE_DRAFT_HOURS,
    ENABLE_POST_MEDIA,
    ENABLE_THREADS,
    STALE_DRAFT_HOURS,
    X_POST_MAX_RETRIES,
    get_settings,
)
from database import count_posts_today, get_session, last_post_time
from logging_config import setup_logging
from models import Draft, Headline, Post
from pipeline.freshness import is_fresh, max_age_hours_for_category
from pipeline.url_resolve import fetch_og_image

logger = setup_logging()

TWEET_LIMIT = 280


class PostingError(Exception):
    pass


def _get_twitter_client():
    import tweepy

    from config import X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, X_API_KEY, X_API_SECRET

    auth = tweepy.OAuth1UserHandler(
        X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
    )
    return tweepy.API(auth)


def check_safety_rails(draft: Draft, daily_cap: int, cooldown_minutes: int) -> None:
    settings = get_settings()
    if not settings["x_configured"] and not settings["dry_run"]:
        raise PostingError("X API keys not configured")

    with get_session() as session:
        headline = session.get(Headline, draft.headline_id)
        if headline and not is_fresh(headline.published_at, draft.category):
            max_h = max_age_hours_for_category(draft.category)
            raise PostingError(
                f"Story is older than {max_h} hours — too stale to post"
            )

    stale_hours = EARNINGS_STALE_DRAFT_HOURS if draft.category == "earnings" else STALE_DRAFT_HOURS
    age = datetime.utcnow() - draft.created_at
    if age > timedelta(hours=stale_hours):
        raise PostingError(f"Draft is older than {stale_hours} hours (stale)")

    today_count = count_posts_today()
    if today_count >= daily_cap:
        raise PostingError(f"Daily post cap reached ({daily_cap}/day)")

    last = last_post_time()
    if last:
        elapsed = (datetime.utcnow() - last).total_seconds() / 60
        if elapsed < cooldown_minutes:
            remaining = cooldown_minutes - elapsed
            raise PostingError(f"Cooldown active — wait {remaining:.0f} more minutes")


def _split_thread(text: str, fmt: str) -> list[str]:
    """Split long posts into tweet-sized chunks."""
    text = text.strip()
    if not ENABLE_THREADS or fmt != "SUMMARY":
        if len(text) <= TWEET_LIMIT:
            return [text]
        # Hard truncate single tweet
        return [text[: TWEET_LIMIT - 1] + "…"]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return [text[:TWEET_LIMIT]]

    tweets: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) <= TWEET_LIMIT:
            current = candidate
        else:
            if current:
                tweets.append(current)
            if len(para) <= TWEET_LIMIT:
                current = para
            else:
                words = para.split()
                chunk = ""
                for word in words:
                    test = f"{chunk} {word}".strip()
                    if len(test) <= TWEET_LIMIT:
                        chunk = test
                    else:
                        if chunk:
                            tweets.append(chunk)
                        chunk = word
                current = chunk
    if current:
        tweets.append(current)

    return tweets or [text[:TWEET_LIMIT]]


def _download_media(url: str) -> bytes | None:
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            if len(resp.content) > 4_000_000:
                return None
            ctype = resp.headers.get("content-type", "")
            if "image" not in ctype:
                return None
            return resp.content
    except Exception:
        return None


def _post_to_x(
    client,
    tweets: list[str],
    *,
    media_bytes: bytes | None = None,
) -> tuple[str, list[str]]:
    """Post one or more tweets. Returns (root_id, all_ids)."""
    media_ids = None
    if media_bytes:
        upload = client.media_upload(filename="image.jpg", file=io.BytesIO(media_bytes))
        media_ids = [upload.media_id]

    first = client.update_status(
        tweets[0],
        media_ids=media_ids,
    )
    ids = [str(first.id)]
    prev_id = first.id

    for part in tweets[1:]:
        resp = client.update_status(
            part,
            in_reply_to_status_id=prev_id,
            auto_populate_reply_metadata=True,
        )
        ids.append(str(resp.id))
        prev_id = resp.id

    return ids[0], ids


def publish_draft(
    draft: Draft,
    text: str | None = None,
    daily_cap: int = 20,
    cooldown_minutes: int = 5,
) -> Post:
    """Approve and publish a draft. Returns the Post record."""
    check_safety_rails(draft, daily_cap, cooldown_minutes)

    post_text = text or draft.text
    settings = get_settings()
    tweets = _split_thread(post_text, draft.format)
    media_url = ""
    media_bytes = None

    if settings["dry_run"] or not settings["x_configured"]:
        tweet_id = f"dry_run_{draft.id}_{int(datetime.utcnow().timestamp())}"
        thread_ids = [tweet_id]
        logger.info("[DRY RUN] Would post %d tweet(s): %s", len(tweets), tweets[0][:80])
    else:
        if ENABLE_POST_MEDIA:
            with get_session() as session:
                headline = session.get(Headline, draft.headline_id)
                if headline and headline.url:
                    media_url = fetch_og_image(headline.url) or ""
                    if media_url:
                        media_bytes = _download_media(media_url)

        client = _get_twitter_client()
        last_error = None
        for attempt in range(X_POST_MAX_RETRIES):
            try:
                tweet_id, thread_ids = _post_to_x(client, tweets, media_bytes=media_bytes)
                logger.info("Posted tweet %s (%d parts)", tweet_id, len(thread_ids))
                break
            except Exception as exc:
                last_error = exc
                wait = 2 ** attempt
                logger.warning(
                    "X post attempt %d failed: %s — retry in %ds",
                    attempt + 1,
                    exc,
                    wait,
                )
                time.sleep(wait)
        else:
            raise PostingError(f"X API error after {X_POST_MAX_RETRIES} tries: {last_error}") from last_error

    with get_session() as session:
        draft_row = session.get(Draft, draft.id)
        if draft_row:
            draft_row.status = "posted"
            draft_row.scheduled_at = None
            draft_row.post_error = None
            if text:
                draft_row.text = text
            session.add(draft_row)

        post = Post(
            draft_id=draft.id,
            tweet_id=tweet_id,
            thread_tweet_ids=",".join(thread_ids),
            media_url=media_url or "",
            posted_at=datetime.utcnow(),
        )
        session.add(post)
        session.commit()
        session.refresh(post)
        return post


def get_today_stats() -> dict:
    from database import get_setting

    daily_cap = get_setting("daily_post_cap", 20)
    return {
        "posted_today": count_posts_today(),
        "daily_cap": daily_cap,
    }
