"""Post a draft to X (Twitter) with safety rails."""

from datetime import datetime, timedelta

from sqlmodel import select

from config import DRY_RUN, get_settings
from database import count_posts_today, get_session, last_post_time
from logging_config import setup_logging
from models import Draft, Post

logger = setup_logging()


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

    age = datetime.utcnow() - draft.created_at
    if age > timedelta(hours=12):
        raise PostingError("Draft is older than 12 hours (stale news)")

    today_count = count_posts_today()
    if today_count >= daily_cap:
        raise PostingError(f"Daily post cap reached ({daily_cap}/day)")

    last = last_post_time()
    if last:
        elapsed = (datetime.utcnow() - last).total_seconds() / 60
        if elapsed < cooldown_minutes:
            remaining = cooldown_minutes - elapsed
            raise PostingError(f"Cooldown active — wait {remaining:.0f} more minutes")


def publish_draft(draft: Draft, text: str | None = None, daily_cap: int = 20, cooldown_minutes: int = 5) -> Post:
    """Approve and publish a draft. Returns the Post record."""
    check_safety_rails(draft, daily_cap, cooldown_minutes)

    post_text = text or draft.text
    settings = get_settings()

    if settings["dry_run"] or not settings["x_configured"]:
        tweet_id = f"dry_run_{draft.id}_{int(datetime.utcnow().timestamp())}"
        logger.info("[DRY RUN] Would post: %s", post_text)
    else:
        try:
            client = _get_twitter_client()
            response = client.update_status(status=post_text)
            tweet_id = str(response.id)
            logger.info("Posted tweet %s", tweet_id)
        except Exception as e:
            logger.error("Failed to post to X: %s", e)
            raise PostingError(f"X API error: {e}") from e

    with get_session() as session:
        draft_row = session.get(Draft, draft.id)
        if draft_row:
            draft_row.status = "posted"
            if text:
                draft_row.text = text
            session.add(draft_row)

        post = Post(draft_id=draft.id, tweet_id=tweet_id, posted_at=datetime.utcnow())
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
