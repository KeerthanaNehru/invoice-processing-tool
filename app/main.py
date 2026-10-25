"""
app/main.py — FastAPI application entry point.

Features:
- Lifespan initialization: SQLite schema + seed data + sample cache preload.
- SlowAPI rate limiting honoring X-Forwarded-For (proxy & Hugging Face compatible).
- CORS middleware for embedding and local development.
- Static file serving for the single-page web app.
- REST & Server-Sent Events (SSE) API routes.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from app.config import HOST, PORT, STATIC_DIR
from app.database import init_db
from app.routes.api import limiter, router as api_router
from app.seed import seed_all

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("Initializing database and tables...")
    init_db()
    logger.info("Running database seeding (samples, vendors, POs, cache)...")
    try:
        seed_all()
    except Exception as exc:
        logger.warning("Seeding encountered warning: %s", exc)
    logger.info("Application startup complete. Ready to process invoices.")
    yield
    logger.info("Application shutting down...")


app = FastAPI(
    title="Invoice Processing Tool",
    description="Automated 8-stage accounts payable invoice processing pipeline with LLM extraction and rule-based decisioning.",
    version="1.0.0",
    lifespan=lifespan,
)

# SlowAPI setup
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "detail": "Too many invoice uploads from this IP address. Limit is 60 uploads per hour.",
        },
    )


# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routes
app.include_router(api_router)

# Mount static files
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "Invoice Processing Tool",
        "version": "1.0.0",
    }


@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse({
        "message": "Zamp Invoice Engine API is running.",
        "docs": "/docs",
        "api": "/api/metrics",
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
