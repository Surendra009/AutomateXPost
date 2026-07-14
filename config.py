import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from security import getenv_secret, validate_security_config

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
APP_BUILD = os.getenv("APP_BUILD", "79")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'postpilot.db'}")
SECRET_KEY = getenv_secret("SECRET_KEY", "dev-secret-change-in-production")
APP_PASSWORD = getenv_secret("APP_PASSWORD", "changeme")
APP_PASSWORD_HASH = getenv_secret("APP_PASSWORD_HASH")
ANTHROPIC_API_KEY = getenv_secret("ANTHROPIC_API_KEY")
OPENAI_API_KEY = getenv_secret("OPENAI_API_KEY")
DEEPSEEK_ENV_NAMES = ("DEEPSEEK_API_KEY", "DEEPSEEK_KEY")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")


def get_deepseek_key() -> str:
    """Read DeepSeek API key (trimmed). Supports common env var aliases."""
    for name in DEEPSEEK_ENV_NAMES:
        value = getenv_secret(name)
        if value:
            return value
    return ""


DEEPSEEK_API_KEY = get_deepseek_key()

FINNHUB_ENV_NAMES = ("FINNHUB_KEY", "FINNHUB_API_KEY", "FINHUB_KEY")


def get_finnhub_key() -> str:
    """Read Finnhub API key (trimmed). Supports common env var name typos."""
    for name in FINNHUB_ENV_NAMES:
        value = getenv_secret(name)
        if value:
            return value
    return ""


FINNHUB_KEY = get_finnhub_key()
X_API_KEY = getenv_secret("X_API_KEY")
X_API_SECRET = getenv_secret("X_API_SECRET")
X_ACCESS_TOKEN = getenv_secret("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = getenv_secret("X_ACCESS_TOKEN_SECRET")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = BASE_DIR / "postpilot.log"

FILTER_MODEL = os.getenv("FILTER_MODEL", "deepseek-chat")
DRAFT_MODEL = os.getenv("DRAFT_MODEL", "deepseek-chat")
DRAFT_PROVIDER = os.getenv("DRAFT_PROVIDER", "auto")  # auto | deepseek | anthropic | openai
FILTER_PROVIDER = os.getenv("FILTER_PROVIDER", "auto")
ANTHROPIC_FILTER_MODEL = os.getenv("ANTHROPIC_FILTER_MODEL", "claude-3-5-haiku-20241022")
ANTHROPIC_DRAFT_MODEL = os.getenv("ANTHROPIC_DRAFT_MODEL", "claude-3-5-sonnet-20241022")
OPENAI_FILTER_MODEL = os.getenv("OPENAI_FILTER_MODEL", "gpt-4o-mini")
OPENAI_DRAFT_MODEL = os.getenv("OPENAI_DRAFT_MODEL", "gpt-4o-mini")
DEEPSEEK_DEFAULT_MODEL = os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat")
DRAFT_MAX_TOKENS = int(os.getenv("DRAFT_MAX_TOKENS", "1200"))
DRAFT_ARTICLE_CHARS = int(os.getenv("DRAFT_ARTICLE_CHARS", "4500"))
# Chat assistant: uses same provider order as drafts when CHAT_PROVIDER=auto (DeepSeek first).
#   CHAT_PROVIDER=deepseek|anthropic|openai  CHAT_MODEL=...
CHAT_PROVIDER = os.getenv("CHAT_PROVIDER", "auto")  # auto | deepseek | anthropic | openai
CHAT_MODEL = os.getenv("CHAT_MODEL", "").strip()
CHAT_DEEPSEEK_MODEL = os.getenv("CHAT_DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL)
CHAT_ANTHROPIC_MODEL = os.getenv("CHAT_ANTHROPIC_MODEL", ANTHROPIC_FILTER_MODEL)
CHAT_OPENAI_MODEL = os.getenv("CHAT_OPENAI_MODEL", "gpt-4o-mini")
PIPELINE_INTERVAL_SECONDS = 300  # 5 min
PIPELINE_TIMEZONE = os.getenv("PIPELINE_TIMEZONE", "America/New_York")
OVERNIGHT_QUIET_START_HOUR = int(os.getenv("OVERNIGHT_QUIET_START_HOUR", "22"))  # 10 PM local
OVERNIGHT_QUIET_END_HOUR = int(os.getenv("OVERNIGHT_QUIET_END_HOUR", "5"))  # 5 AM local
OVERNIGHT_CATCHUP_HOUR = int(os.getenv("OVERNIGHT_CATCHUP_HOUR", "5"))  # run once around 5 AM
OVERNIGHT_CATCHUP_MAX_AGE_HOURS = int(os.getenv("OVERNIGHT_CATCHUP_MAX_AGE_HOURS", "8"))  # 10pm–5am window
WEEKEND_INTERVAL_HOURS = int(os.getenv("WEEKEND_INTERVAL_HOURS", "3"))  # Sat/Sun: one run per 3 hours
MAX_HEADLINES_PER_CYCLE = 35
MAX_DRAFTS_PER_CYCLE = int(os.getenv("MAX_DRAFTS_PER_CYCLE", "5"))
MAX_EARNINGS_DRAFTS_PER_CYCLE = 5
MAX_MARKET_EARNINGS_DRAFTS_PER_CYCLE = 3  # beat/miss drafts when watchlist is empty
EARNINGS_PREVIEW_DAYS_FORWARD = 2  # preview drafts for watchlist tickers
MAX_MACRO_DRAFTS_PER_CYCLE = 3
MAX_SEC_DRAFTS_PER_CYCLE = 2
MAX_COMPANY_NEWS_DRAFTS_PER_CYCLE = 3
MIN_RELEVANCE_SCORE = 0.75
MIN_AI_RELEVANCE_SCORE = 0.72  # slightly lower bar for major AI product news
STALE_DRAFT_HOURS = 8  # pending drafts removed from queue after this
EARNINGS_STALE_DRAFT_HOURS = 2  # earnings drafts expire faster — post within ~2h of release
MAX_NEWS_AGE_HOURS = 4  # ignore headlines published before this window
MAX_EARNINGS_AGE_HOURS = 2  # skip/post-block earnings results older than this
EARNINGS_WINDOW_END_HOUR = 20  # faster polling until 8pm ET for AMC results
MIN_SUMMARY_CHARS_FOR_SKIP_FETCH = 100  # skip full article fetch when RSS summary is enough
ARTICLE_FETCH_CATEGORIES = frozenset({"earnings", "macro", "ai", "regulatory", "ipo"})
DRAFT_DEDUP_HOURS = 24  # skip LLM if same story was drafted within this window
INGEST_DEDUP_HOURS = 24  # cross-source title dedup window at ingest
INGEST_TITLE_FUZZY_THRESHOLD = 88  # fuzzy match across sources (slightly below exact)
CLASSIFICATION_CACHE_HOURS = 12  # reuse Haiku classifications within this window
REJECTION_LEARN_THRESHOLD = 2  # rejects before a title shape is treated as noise
REJECTION_FUZZY_THRESHOLD = 88  # fuzzy match against learned rejected titles

WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "true").lower() in ("1", "true", "yes")
MAX_WEB_RESULTS_PER_QUERY = int(os.getenv("MAX_WEB_RESULTS_PER_QUERY", "6"))
MAX_WEB_TICKERS_PER_CYCLE = int(os.getenv("MAX_WEB_TICKERS_PER_CYCLE", "8"))
MAX_WEB_TOPICS_PER_CYCLE = int(os.getenv("MAX_WEB_TOPICS_PER_CYCLE", "5"))
MAX_EARNINGS_WEB_QUERIES = int(os.getenv("MAX_EARNINGS_WEB_QUERIES", "3"))
MAX_EARNINGS_WEB_ARTICLES = int(os.getenv("MAX_EARNINGS_WEB_ARTICLES", "3"))
EARNINGS_ENRICH_BUDGET_SECONDS = int(os.getenv("EARNINGS_ENRICH_BUDGET_SECONDS", "35"))
MAX_EARNINGS_HIGHLIGHTS = int(os.getenv("MAX_EARNINGS_HIGHLIGHTS", "10"))
EARNINGS_PRESS_DAYS_BACK = int(os.getenv("EARNINGS_PRESS_DAYS_BACK", "10"))
_LLM_EARNINGS_BULLET_BATCHES_PER_CYCLE = int(os.getenv("LLM_EARNINGS_BULLET_BATCHES_PER_CYCLE", "6"))
LLM_EARNINGS_BULLET_BATCHES_PER_CYCLE = _LLM_EARNINGS_BULLET_BATCHES_PER_CYCLE
FINNHUB_GENERAL_SUPPLEMENT = os.getenv("FINNHUB_GENERAL_SUPPLEMENT", "false").lower() in (
    "1",
    "true",
    "yes",
)

# Posting & engagement
X_POST_MAX_RETRIES = int(os.getenv("X_POST_MAX_RETRIES", "3"))
SCHEDULED_POST_CHECK_SECONDS = int(os.getenv("SCHEDULED_POST_CHECK_SECONDS", "60"))
MARKET_HOURS_INTERVAL_SECONDS = int(os.getenv("MARKET_HOURS_INTERVAL_SECONDS", "120"))
PREMARKET_START_HOUR = int(os.getenv("PREMARKET_START_HOUR", "7"))
MARKET_CLOSE_HOUR = int(os.getenv("MARKET_CLOSE_HOUR", "16"))
ENABLE_THREADS = os.getenv("ENABLE_THREADS", "true").lower() in ("1", "true", "yes")
ENABLE_POST_MEDIA = os.getenv("ENABLE_POST_MEDIA", "true").lower() in ("1", "true", "yes")
ANALYTICS_REFRESH_HOURS = int(os.getenv("ANALYTICS_REFRESH_HOURS", "6"))
ALERT_WEBHOOK_URL = getenv_secret("ALERT_WEBHOOK_URL")
TEAMS_WEBHOOK_URL = getenv_secret("TEAMS_WEBHOOK_URL")
DISCORD_WEBHOOK_URL = getenv_secret("DISCORD_WEBHOOK_URL")
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
MAX_CHANNEL_DRAFTS_PER_CYCLE = int(os.getenv("MAX_CHANNEL_DRAFTS_PER_CYCLE", "3"))
MAX_TEAMS_DRAFTS_PER_CYCLE = MAX_CHANNEL_DRAFTS_PER_CYCLE  # backwards compat
VAPID_PUBLIC_KEY = getenv_secret("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = getenv_secret("VAPID_PRIVATE_KEY")
VAPID_CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:admin@postpilot.local")

REJECTION_REASONS = (
    "too_vague",
    "too_small",
    "too_opinionated",
    "wrong_ticker",
    "bad_hook",
    "too_long",
    "duplicate",
    "off_topic",
    "listicle",
    "other",
)

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

DEFAULT_SETTINGS = {
    "pipeline_enabled": True,
    "daily_post_cap": 20,
    "cooldown_minutes": 5,
    "watchlist": [],
    "search_topics": [],
    "paused_until": None,
    "dedup_mode": "pipeline",
    "allow_hashtags": False,
    "push_enabled": True,
    "discord_enabled": True,
    "teams_enabled": False,
}


@lru_cache
def get_settings():
    return {
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
        "openai_configured": bool(OPENAI_API_KEY),
        "deepseek_configured": bool(get_deepseek_key()),
        "x_configured": all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]),
        "finnhub_configured": bool(get_finnhub_key()),
        "dry_run": DRY_RUN,
        "web_search_enabled": WEB_SEARCH_ENABLED,
        "push_configured": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY),
        "alert_webhook_configured": bool(ALERT_WEBHOOK_URL),
        "teams_configured": bool(TEAMS_WEBHOOK_URL),
        "discord_configured": bool(DISCORD_WEBHOOK_URL),
        "build": APP_BUILD,
    }


def run_security_checks() -> None:
    """Validate secrets; raises in production if misconfigured."""
    validate_security_config(
        secret_key=SECRET_KEY,
        app_password=APP_PASSWORD,
        app_password_hash=APP_PASSWORD_HASH,
    )
