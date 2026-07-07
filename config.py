import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'postpilot.db'}")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
APP_PASSWORD = os.getenv("APP_PASSWORD", "changeme")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")
X_API_KEY = os.getenv("X_API_KEY", "")
X_API_SECRET = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = BASE_DIR / "postpilot.log"

FILTER_MODEL = "claude-haiku-4-5"
DRAFT_MODEL = "claude-sonnet-4-6"
PIPELINE_INTERVAL_SECONDS = 300  # 5 min

RSS_FEEDS = [
    ("CNBC Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("SEC EDGAR 8-K", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-k&company=&dateb=&owner=include&count=40&output=atom"),
]

DEFAULT_SETTINGS = {
    "pipeline_enabled": True,
    "daily_post_cap": 20,
    "cooldown_minutes": 5,
    "watchlist": [],
    "paused_until": None,
}


@lru_cache
def get_settings():
    return {
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
        "x_configured": all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]),
        "finnhub_configured": bool(FINNHUB_KEY),
        "dry_run": DRY_RUN,
    }
