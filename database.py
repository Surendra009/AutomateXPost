import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator

from sqlmodel import Session, SQLModel, create_engine, select

from config import DATABASE_URL, DEFAULT_SETTINGS
from models import AppSetting

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


def _enable_sqlite_wal() -> None:
    if not DATABASE_URL.startswith("sqlite"):
        return
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    if not db_path or db_path == DATABASE_URL:
        return
    try:
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.close()
    except Exception:
        pass


def _migrate_headline_title_fp() -> None:
    """Add title_fp column to existing SQLite DBs (create_all won't alter tables)."""
    import sqlite3

    if not DATABASE_URL.startswith("sqlite"):
        return

    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    if not db_path or db_path == DATABASE_URL:
        return

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(headlines)")
        columns = {row[1] for row in cur.fetchall()}
        if "title_fp" not in columns:
            cur.execute("ALTER TABLE headlines ADD COLUMN title_fp TEXT DEFAULT ''")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_headlines_title_fp ON headlines (title_fp)")
            conn.commit()
        # Backfill empty title_fp for existing rows
        from pipeline.story_key import title_fingerprint

        cur.execute("SELECT id, title FROM headlines WHERE title_fp IS NULL OR title_fp = '' LIMIT 500")
        rows = cur.fetchall()
        for row_id, title in rows:
            cur.execute(
                "UPDATE headlines SET title_fp = ? WHERE id = ?",
                (title_fingerprint(title or ""), row_id),
            )
        if rows:
            conn.commit()
        conn.close()
    except Exception:
        pass


def _migrate_schema() -> None:
    """Add columns/tables for existing SQLite DBs."""
    import sqlite3

    if not DATABASE_URL.startswith("sqlite"):
        return

    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    if not db_path or db_path == DATABASE_URL:
        return

    alters = [
        ("drafts", "scheduled_at", "TEXT"),
        ("drafts", "post_error", "TEXT"),
        ("posts", "thread_tweet_ids", "TEXT DEFAULT ''"),
        ("posts", "media_url", "TEXT DEFAULT ''"),
        ("posts", "like_count", "INTEGER DEFAULT 0"),
        ("posts", "retweet_count", "INTEGER DEFAULT 0"),
        ("posts", "reply_count", "INTEGER DEFAULT 0"),
        ("posts", "impression_count", "INTEGER DEFAULT 0"),
        ("posts", "metrics_updated_at", "TEXT"),
    ]

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        for table, column, col_type in alters:
            cur.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in cur.fetchall()}
            if column not in columns:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
        conn.close()
    except Exception:
        pass


def init_db() -> None:
    _enable_sqlite_wal()
    SQLModel.metadata.create_all(engine)
    _migrate_headline_title_fp()
    _migrate_schema()
    with Session(engine) as session:
        for key, value in DEFAULT_SETTINGS.items():
            existing = session.get(AppSetting, key)
            if not existing:
                session.add(AppSetting(key=key, value=json.dumps(value)))
        session.commit()
    from pipeline.feedback import backfill_from_rejected_drafts

    backfill_from_rejected_drafts()


@contextmanager
def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def get_setting(key: str, default: Any = None) -> Any:
    with get_session() as session:
        row = session.get(AppSetting, key)
        if not row:
            return default
        try:
            value = json.loads(row.value)
        except json.JSONDecodeError:
            return default
        if value is None:
            return default
        return value


def _coerce_settings(result: dict) -> dict:
    """Ensure settings values match expected types after DB load."""
    if not isinstance(result.get("watchlist"), list):
        result["watchlist"] = []
    if not isinstance(result.get("search_topics"), list):
        result["search_topics"] = []
    if result.get("paused_until") == "":
        result["paused_until"] = None
    return result


def get_all_settings() -> dict:
    with get_session() as session:
        rows = session.exec(select(AppSetting)).all()
        result = dict(DEFAULT_SETTINGS)
        for row in rows:
            try:
                value = json.loads(row.value)
            except json.JSONDecodeError:
                continue
            if value is not None:
                result[row.key] = value
        return _coerce_settings(result)


def set_setting(key: str, value: Any) -> None:
    with get_session() as session:
        row = session.get(AppSetting, key)
        if row:
            row.value = json.dumps(value)
        else:
            session.add(AppSetting(key=key, value=json.dumps(value)))
        session.commit()


def count_posts_today() -> int:
    from models import Post

    today = datetime.utcnow().date()
    with get_session() as session:
        posts = session.exec(select(Post)).all()
        return sum(1 for p in posts if p.posted_at.date() == today)


def last_post_time() -> datetime | None:
    from models import Post

    with get_session() as session:
        posts = session.exec(select(Post).order_by(Post.posted_at.desc())).all()
        return posts[0].posted_at if posts else None
