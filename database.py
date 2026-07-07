import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator

from sqlmodel import Session, SQLModel, create_engine, select

from config import DATABASE_URL, DEFAULT_SETTINGS
from models import AppSetting

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


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

        cur.execute("SELECT id, title FROM headlines WHERE title_fp IS NULL OR title_fp = ''")
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


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_headline_title_fp()
    with Session(engine) as session:
        for key, value in DEFAULT_SETTINGS.items():
            existing = session.get(AppSetting, key)
            if not existing:
                session.add(AppSetting(key=key, value=json.dumps(value)))
        session.commit()


@contextmanager
def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def get_setting(key: str, default: Any = None) -> Any:
    with get_session() as session:
        row = session.get(AppSetting, key)
        if not row:
            return default
        return json.loads(row.value)


def set_setting(key: str, value: Any) -> None:
    with get_session() as session:
        row = session.get(AppSetting, key)
        if row:
            row.value = json.dumps(value)
        else:
            session.add(AppSetting(key=key, value=json.dumps(value)))
        session.commit()


def get_all_settings() -> dict:
    with get_session() as session:
        rows = session.exec(select(AppSetting)).all()
        result = dict(DEFAULT_SETTINGS)
        for row in rows:
            result[row.key] = json.loads(row.value)
        return result


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
