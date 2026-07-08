import argparse
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import PIPELINE_INTERVAL_SECONDS, STATIC_DIR, get_settings, run_security_checks
from database import init_db
from logging_config import setup_logging
from pipeline.scheduler import start_pipeline, stop_pipeline
from routes.api import router as api_router
from seed import seed

logger = setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_security_checks()
    init_db()
    if os.getenv("SEED_ON_START", "false").lower() in ("1", "true", "yes"):
        seed()

    async def _deferred_pipeline_start() -> None:
        # Let /health respond before the first ingest cycle (Railway healthcheck).
        await asyncio.sleep(12)
        start_pipeline(PIPELINE_INTERVAL_SECONDS)

    pipeline_boot = asyncio.create_task(_deferred_pipeline_start())
    logger.info("PostPilot started (dry_run=%s)", get_settings()["dry_run"])
    yield
    pipeline_boot.cancel()
    stop_pipeline()
    logger.info("PostPilot stopped")


app = FastAPI(title="PostPilot", lifespan=lifespan)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("HTTPS", "").lower() == "true":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text())


@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PostPilot")
    parser.add_argument("--dry-run", action="store_true", help="Post to console instead of X")
    parser.add_argument("--no-seed", action="store_true", help="Skip seeding fake drafts")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    if args.no_seed:
        os.environ["SEED_ON_START"] = "false"

    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=args.port, reload=False)
