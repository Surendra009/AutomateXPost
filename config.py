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

FINNHUB_ENV_NAMES = ("FINNHUB_KEY", "FINNHUB_API_KEY", "FINHUB_KEY")


def get_finnhub_key() -> str:
    """Read Finnhub API key (trimmed). Supports common env var name typos."""
    for name in FINNHUB_ENV_NAMES:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


FINNHUB_KEY = get_finnhub_key()
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
MAX_HEADLINES_PER_CYCLE = 35
MAX_DRAFTS_PER_CYCLE = 3
MAX_EARNINGS_DRAFTS_PER_CYCLE = 5
MIN_RELEVANCE_SCORE = 0.75
MIN_AI_RELEVANCE_SCORE = 0.72  # slightly lower bar for major AI product news
STALE_DRAFT_HOURS = 4  # pending drafts older than this are auto-removed
MAX_NEWS_AGE_HOURS = 4  # ignore headlines published before this window
MIN_SUMMARY_CHARS_FOR_SKIP_FETCH = 100  # skip full article fetch when RSS summary is enough
ARTICLE_FETCH_CATEGORIES = frozenset({"earnings", "macro"})  # always fetch article for these
DRAFT_DEDUP_HOURS = 24  # skip LLM if same story was drafted within this window

# Market news RSS feeds (Reuters feed is deprecated/broken — use alternatives below)
RSS_FEEDS = [
    ("CNBC Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
    ("Financial Times", "https://www.ft.com/rss/home"),
]

# AI company / product news
AI_RSS_FEEDS = [
    ("CNBC AI", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("OpenAI Blog", "https://openai.com/blog/rss.xml"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("MIT Tech Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
]

# SEC Atom feed requires a User-Agent with contact info (SEC policy)
SEC_EDGAR_8K_FEED = (
    "SEC EDGAR 8-K",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-k&company=&dateb=&owner=include&count=40&output=atom",
)
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "PostPilot/1.0 (automated news bot)")

# Default tickers for Finnhub company news when watchlist is empty
DEFAULT_FINNHUB_TICKERS = ["NVDA", "MSFT", "GOOGL", "META", "AMZN", "AAPL", "TSLA", "AMD"]

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
        "finnhub_configured": bool(get_finnhub_key()),
        "dry_run": DRY_RUN,
    }
