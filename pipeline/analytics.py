"""Fetch X engagement metrics for posted tweets."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import select

from config import ANALYTICS_REFRESH_HOURS
from database import get_session
from logging_config import setup_logging
from models import Post

logger = setup_logging()


def refresh_post_metrics() -> int:
    """Update like/RT/reply counts for recent posts. Returns rows updated."""
    cutoff = datetime.utcnow() - timedelta(days=14)
    updated = 0

    with get_session() as session:
        posts = session.exec(
            select(Post).where(
                Post.posted_at >= cutoff,
                Post.tweet_id != "",
            )
        ).all()

        for post in posts:
            if post.tweet_id.startswith("dry_run"):
                continue
            if post.metrics_updated_at:
                age_h = (datetime.utcnow() - post.metrics_updated_at).total_seconds() / 3600
                if age_h < ANALYTICS_REFRESH_HOURS:
                    continue

            metrics = _fetch_tweet_metrics(post.tweet_id)
            if not metrics:
                continue

            post.like_count = metrics.get("likes", post.like_count)
            post.retweet_count = metrics.get("retweets", post.retweet_count)
            post.reply_count = metrics.get("replies", post.reply_count)
            post.impression_count = metrics.get("impressions", post.impression_count)
            post.metrics_updated_at = datetime.utcnow()
            session.add(post)
            updated += 1

        if updated:
            session.commit()
            logger.info("Refreshed metrics for %d posts", updated)

    return updated


def _fetch_tweet_metrics(tweet_id: str) -> dict | None:
    from config import get_settings

    if not get_settings()["x_configured"]:
        return None

    try:
        import tweepy

        from config import X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, X_API_KEY, X_API_SECRET

        auth = tweepy.OAuth1UserHandler(
            X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
        )
        client = tweepy.API(auth)
        status = client.get_status(tweet_id, tweet_mode="extended")
        return {
            "likes": int(status.favorite_count or 0),
            "retweets": int(status.retweet_count or 0),
            "replies": 0,
            "impressions": 0,
        }
    except Exception as exc:
        logger.debug("Metrics fetch failed for %s: %s", tweet_id, exc)
        return None


def analytics_summary() -> dict:
    with get_session() as session:
        posts = session.exec(select(Post).order_by(Post.posted_at.desc()).limit(50)).all()
        if not posts:
            return {"posts": [], "totals": {}}

        rows = []
        total_likes = total_rts = 0
        for p in posts:
            total_likes += p.like_count
            total_rts += p.retweet_count
            rows.append(
                {
                    "tweet_id": p.tweet_id,
                    "posted_at": p.posted_at.isoformat(),
                    "likes": p.like_count,
                    "retweets": p.retweet_count,
                    "replies": p.reply_count,
                }
            )

        return {
            "posts": rows[:20],
            "totals": {
                "likes": total_likes,
                "retweets": total_rts,
                "count": len(posts),
            },
        }
