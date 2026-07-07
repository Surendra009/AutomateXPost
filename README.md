# PostPilot

Automated X (Twitter) posting system for stock, AI, and macro news — with a mobile approval app. Nothing posts without your explicit approval.

## How it works

1. **Ingest** — Every 5 minutes, fetches headlines from CNBC, Reuters, TechCrunch, The Verge AI, SEC EDGAR 8-K, and optionally Finnhub
2. **Filter** — Claude Haiku classifies relevance (stock-moving news only)
3. **Draft** — Claude Sonnet writes terse, factual X posts
4. **Queue** — Drafts wait for your review on the mobile PWA
5. **Post** — Approved drafts publish to X via API (with safety rails)

## Quick start

```bash
# Clone and enter the project
cd postpilot

# Create virtual environment (recommended)
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure secrets
cp .env.example .env
# Edit .env — at minimum set APP_PASSWORD

# Generate PWA icons
python3 generate_icons.py

# Seed 5 fake drafts for immediate UI testing
python3 seed.py

# Run (dry-run mode by default — prints instead of posting)
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` on your phone or browser. Default password: value of `APP_PASSWORD` in `.env` (default `postpilot` in the example `.env`).

### Flags

```bash
# Dry-run: pipeline runs, approve prints to log instead of posting to X
DRY_RUN=true uvicorn app:app

# Or via Python
python3 app.py --dry-run --port 8000

# Skip auto-seeding on startup
SEED_ON_START=false uvicorn app:app
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `APP_PASSWORD` | Yes | Single password for mobile login |
| `SECRET_KEY` | Yes | Random string for session cookies |
| `ANTHROPIC_API_KEY` | For pipeline | Claude API key for filter + draft |
| `X_API_KEY` | For posting | X API consumer key |
| `X_API_SECRET` | For posting | X API consumer secret |
| `X_ACCESS_TOKEN` | For posting | X OAuth access token |
| `X_ACCESS_TOKEN_SECRET` | For posting | X OAuth access token secret |
| `FINNHUB_KEY` | No | Optional Finnhub news feed |
| `DRY_RUN` | No | `true` to skip actual X posting (default: true) |
| `DATABASE_URL` | No | SQLite path (default: `./postpilot.db`) |
| `LOG_LEVEL` | No | `INFO`, `DEBUG`, etc. |

## Getting X API keys (free tier)

1. Go to [developer.x.com](https://developer.x.com) and create a developer account
2. Create a Project and App in the Developer Portal
3. Under **Keys and Tokens**, generate:
   - API Key and Secret (Consumer Keys)
   - Access Token and Secret (with **Read and Write** permissions)
4. Set all four values in `.env`
5. Set `DRY_RUN=false` when ready to post for real

The free tier allows limited posts per month — PostPilot enforces its own daily cap (default 20) and cooldown (default 5 min).

## Mobile PWA — Add to iPhone Home Screen

1. Deploy PostPilot to a server with HTTPS (required for PWA + secure cookies)
2. Open the URL in **Safari** on your iPhone
3. Tap the **Share** button → **Add to Home Screen**
4. The app launches fullscreen with the PostPilot icon

Features:
- Queue screen with approve / reject / edit
- Auto-refresh every 30s + pull-to-refresh
- History of posted and rejected drafts
- Settings: pipeline toggle, daily cap, cooldown, watchlist, pause

## API routes

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/login` | Password login (sets httpOnly cookie) |
| GET | `/api/queue` | Pending drafts |
| POST | `/api/drafts/{id}/approve` | Approve (optional `{"text": "edited"}`) |
| POST | `/api/drafts/{id}/reject` | Reject |
| GET | `/api/history` | Posted + rejected + today's stats |
| GET | `/api/settings` | Current settings |
| PATCH | `/api/settings` | Update settings |

## Docker deployment

```bash
docker build -t postpilot .
docker run -d \
  --name postpilot \
  -p 8000:8000 \
  --env-file .env \
  -v postpilot-data:/app/postpilot.db \
  postpilot
```

### Railway

1. Connect your GitHub repo
2. Railway auto-detects the Dockerfile
3. Add environment variables from `.env.example` in the Railway dashboard
4. Deploy — Railway provides HTTPS automatically

### Render

1. New **Web Service** → connect repo
2. Environment: Docker
3. Add env vars from `.env.example`
4. Render provides HTTPS on `*.onrender.com`

### Any VPS

```bash
git clone <your-repo> && cd postpilot
cp .env.example .env && nano .env
docker compose up -d   # or use the docker run command above
```

Put nginx or Caddy in front for HTTPS. Set `secure=True` on session cookies in `auth.py` when behind HTTPS.

## Safety rails

- **Nothing posts automatically** — every draft requires approval
- Max 20 posts/day (configurable)
- Min 5 minutes between posts (configurable)
- Drafts older than 12 hours are rejected as stale
- Dry-run mode for testing without X API keys

## Project structure

```
postpilot/
├── app.py              # FastAPI app + lifespan
├── config.py           # Settings from .env
├── database.py         # SQLite helpers
├── models.py           # SQLModel tables
├── auth.py             # Session auth + rate limiting
├── seed.py             # Fake draft seeder
├── generate_icons.py   # PWA icon generator
├── routes/api.py       # API endpoints
├── pipeline/
│   ├── ingest.py       # RSS + Finnhub
│   ├── filter.py       # Claude Haiku classifier
│   ├── draft.py        # Claude Sonnet drafter
│   ├── post.py         # X posting + safety rails
│   └── scheduler.py    # Background asyncio loop
├── static/             # PWA (HTML, CSS, JS, icons)
├── Dockerfile
└── requirements.txt
```

## Development

```bash
# Re-seed fake drafts
python3 seed.py

# Test ingestion only
python3 -c "from pipeline.ingest import ingest_headlines; print(ingest_headlines())"

# View logs
tail -f postpilot.log
```

## License

MIT
