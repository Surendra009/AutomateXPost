# AGENTS.md

## Cursor Cloud specific instructions

PostPilot is a single self-contained Python 3.11+ FastAPI app (served by Uvicorn) with an embedded SQLite DB and an in-process background scheduler. There is no separate frontend build — the PWA in `static/` is served directly. See `README.md` for the full product/setup docs and `config.py` for all env vars.

### Environment
- Dependencies live in a project venv at `.venv` (created/refreshed by the startup update script: `python3 -m venv .venv` + `pip install -r requirements.txt`). Always run app/scripts with `.venv/bin/python` or after `source .venv/bin/activate`.
- System python is 3.12 (repo `.python-version` says 3.11); requirements use `>=` constraints and run fine on 3.12. `python3.12-venv` is installed at the system level (needed for `python3 -m venv`).
- `.env` is gitignored and not in the repo. For local dev the app runs with weak defaults (login password `changeme`) even without `.env`. This environment ships a dev `.env` with login password `postpilot`, `DRY_RUN=true`, and a dev `SECRET_KEY`.

### Running the app (dev)
- Start: `.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000` (or `./start.sh`, or `python app.py --dry-run --port 8000`). Open `http://localhost:8000`.
- Health: `GET /health`. Log in via the UI or `POST /api/login {"password": "..."}`.
- The background pipeline starts ~12s after boot and hits live RSS/web feeds; this is normal and does not block the server or `/health`.

### Non-obvious caveats
- Keep `DRY_RUN=true` (default) unless intentionally posting: approvals then log `[DRY RUN] Would post ...` and write a `posts` row with a `dry_run_*` tweet id instead of hitting the X API. No X/LLM API keys are needed to run/test the UI in dry-run.
- Posting safety rails apply in the UI: a per-post cooldown (default 5 min) and daily cap. When manually approving multiple drafts back-to-back, an approve can be blocked by "Cooldown active"; set `cooldown_minutes` to 0 via `PATCH /api/settings` to test rapid approvals.
- The History screen refreshes asynchronously (on a timer / navigation), so a just-approved post may take a moment to appear there; the source of truth is the DB (`drafts.status='posted'` + a row in `posts`).
- Seed sample drafts for UI testing with `.venv/bin/python seed.py` (idempotent-ish: skips if pending drafts already exist). Set `SEED_ON_START=true` to auto-seed on boot. Pending drafts auto-expire as `stale` after a few hours.

### Lint / test / build
- There is no test suite, no linter config, and no build step in this repo. "Build" = install deps into the venv; "run" = the uvicorn command above.
