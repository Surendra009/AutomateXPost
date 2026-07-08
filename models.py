from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Headline(SQLModel, table=True):
    __tablename__ = "headlines"

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str
    url: str
    title: str
    summary: str = ""
    published_at: datetime
    hash: str = Field(index=True)
    title_fp: str = Field(default="", index=True)  # cross-source normalized title key
    status: str = "new"  # new, filtered, discarded, drafted


class ClassificationCache(SQLModel, table=True):
    __tablename__ = "classification_cache"

    fingerprint: str = Field(primary_key=True)
    result_json: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Draft(SQLModel, table=True):
    __tablename__ = "drafts"

    id: Optional[int] = Field(default=None, primary_key=True)
    headline_id: int = Field(foreign_key="headlines.id")
    text: str
    format: str  # BREAKING, CONTEXT, SUMMARY
    impact: str = "med"
    category: str = "other"
    tickers: str = ""  # comma-separated
    confidence: float = 0.5
    status: str = "pending"  # pending, scheduled, approved, rejected, posted, stale
    scheduled_at: datetime | None = None
    post_error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Post(SQLModel, table=True):
    __tablename__ = "posts"

    id: Optional[int] = Field(default=None, primary_key=True)
    draft_id: int = Field(foreign_key="drafts.id")
    tweet_id: str = ""
    thread_tweet_ids: str = ""  # comma-separated root + replies
    media_url: str = ""
    posted_at: datetime = Field(default_factory=datetime.utcnow)
    like_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    impression_count: int = 0
    metrics_updated_at: datetime | None = None


class RejectionFeedback(SQLModel, table=True):
    __tablename__ = "rejection_feedback"

    normalized_title: str = Field(primary_key=True)
    title_sample: str = ""
    reject_count: int = 1
    pattern: str | None = None  # optional regex learned from repeated rejects
    last_rejected_at: datetime = Field(default_factory=datetime.utcnow)


class RejectionNote(SQLModel, table=True):
    __tablename__ = "rejection_notes"

    id: Optional[int] = Field(default=None, primary_key=True)
    draft_id: int = Field(index=True)
    reason: str = "other"
    note: str = ""
    draft_text_sample: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class PushSubscription(SQLModel, table=True):
    __tablename__ = "push_subscriptions"

    id: Optional[int] = Field(default=None, primary_key=True)
    endpoint: str = Field(unique=True, index=True)
    p256dh: str
    auth: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AppSetting(SQLModel, table=True):
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: str
