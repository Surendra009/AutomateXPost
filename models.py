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
    status: str = "pending"  # pending, approved, rejected, posted, stale
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Post(SQLModel, table=True):
    __tablename__ = "posts"

    id: Optional[int] = Field(default=None, primary_key=True)
    draft_id: int = Field(foreign_key="drafts.id")
    tweet_id: str = ""
    posted_at: datetime = Field(default_factory=datetime.utcnow)


class AppSetting(SQLModel, table=True):
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: str
