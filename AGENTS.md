# AGENTS.md

## Cursor Cloud specific instructions

PostPilot is a single FastAPI service (a mobile PWA) that ingests market news, drafts social posts, and holds them for manual approval. There is one product/service — no separate frontend/backend. See `README.md` for the full feature overview and env var reference.

### Running locally (dev)
- Python deps live in a virtualenv at `.venv` (created by the update script). Activate with `. .venv/bin/activate` before running anything.
- Dev server (with hot reload): `python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload`. Alternatively `python app.py --dry-run --port 8000`.
- A local `.env` is required. It is gitignored, so it does NOT persist across fresh cloud VMs — recreate it if missing. Minimal working dev config:
  ```
  APP_PASSWORD=postpilot
  SECRET_KEY=local-dev-secret-key-not-for-production
  DRY_RUN=true
  SEED_ON_START=true
  DATABASE_URL=sqlite:///./postpilot.db
  ```
- `SEED_ON_START=true` loads 5 fake drafts on boot so the queue/approve flow can be tested immediately without any API keys. Log in at `http://localhost:8000` with the value of `APP_PASSWORD`.

### Non-obvious gotchas
- **Startup is async and delayed.** `app.py`'s lifespan initializes the DB in a background task and only starts the pipeline after a ~12s sleep. `/health` returns `{"ready": true}` once the DB is up; seeding/pipeline logs appear a few seconds later. Don't assume failure if the queue is briefly empty right after boot.
- **No LLM/X keys needed to run.** Without `DEEPSEEK_API_KEY`/`ANTHROPIC_API_KEY` the pipeline logs `No LLM provider configured` and creates 0 real drafts — this is expected. `DRY_RUN=true` (default) makes approvals print `[DRY RUN] Would post ...` instead of hitting the X API. Seeded drafts still let you exercise the full approve/history flow.
- **Approval cooldown is a real feature, not a bug.** There's a min-minutes cooldown between posts (default 5) and a daily cap (default 20). Approving two drafts in quick succession returns HTTP 400 "Cooldown active". Adjust via Settings or wait; not an environment problem.
- **Security checks only fire in "production".** `run_security_checks()` (weak-secret rejection) is skipped unless `RAILWAY_ENVIRONMENT` is set or `ENFORCE_SECURITY=true`. Local dev with the weak defaults above starts fine.
- No test suite and no linter config exist in this repo.

### Quick API smoke test
```
curl -s -c /tmp/c.txt -X POST localhost:8000/api/login -H 'Content-Type: application/json' -d '{"password":"postpilot"}'
curl -s -b /tmp/c.txt localhost:8000/api/queue
curl -s -b /tmp/c.txt -X POST localhost:8000/api/drafts/<id>/approve -H 'Content-Type: application/json' -d '{}'
```
