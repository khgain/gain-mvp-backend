"""
Gain AI Backend — FastAPI application entry point.

Start with:
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Swagger UI:
  http://localhost:8000/docs

ReDoc:
  http://localhost:8000/redoc
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.database import connect_to_mongo, close_mongo_connection
from app.utils.logging import setup_logging, get_logger

# Import all routers
from app.routes import auth, leads, campaigns, agents, tenants, webhooks, dashboard, calls, documents

setup_logging("DEBUG" if not settings.is_production else "INFO")
logger = get_logger("main")

# ---------------------------------------------------------------------------
# Rate limiter — per-IP (extend to per-tenant in Day 3)
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def _gmail_watch_renewal_loop():
    """
    Register Gmail push notifications on startup and renew every 6 days.
    Gmail watch() expires after 7 days — renewing at 6 days keeps it continuous.
    No-op if GMAIL_SERVICE_ACCOUNT_JSON or GMAIL_PUBSUB_TOPIC are not set.
    """
    import asyncio
    from app.services.gmail_service import setup_watch, _is_configured

    if not _is_configured():
        logger.info("Gmail push: env vars not configured — skipping")
        return

    SIX_DAYS = 6 * 24 * 60 * 60

    while True:
        try:
            history_id = await setup_watch()
            if history_id:
                logger.info(f"Gmail watch active — historyId={history_id}, renewing in 6 days")
            else:
                logger.warning("Gmail watch returned no historyId — retrying in 1 hour")
                await asyncio.sleep(3600)
                continue
        except Exception as exc:
            logger.error(f"Gmail watch renewal error: {exc}")
        await asyncio.sleep(SIX_DAYS)


async def _auto_seed_if_empty():
    """Seed default users/tenant if the database is empty (first deploy)."""
    try:
        from app.database import get_db
        db = get_db()
        if await db.users.count_documents({}) == 0:
            logger.info("No users found — running auto-seed...")
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from scripts.seed import seed
            await seed()
            logger.info("Auto-seed complete")
        else:
            logger.info("Database already has users — skipping auto-seed")
    except Exception as e:
        logger.error(f"Auto-seed failed (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    logger.info("=== Gain AI Backend starting ===")
    await connect_to_mongo()
    await _auto_seed_if_empty()
    asyncio.create_task(_gmail_watch_renewal_loop())
    yield
    await close_mongo_connection()
    logger.info("=== Gain AI Backend stopped ===")


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Gain AI — Lending Operations Backend",
    description=(
        "Backend API for Gain AI — automates loan application onboarding, "
        "document collection, and validation for banks, NBFCs, and fintechs."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------

import time

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)

    # Never log auth headers or body
    logger.info(
        f"{request.method} {request.url.path} → {response.status_code} ({duration_ms}ms)"
    )
    return response


# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for error in exc.errors():
        errors.append(
            {
                "field": " → ".join(str(loc) for loc in error["loc"]),
                "message": error["msg"],
            }
        )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"success": False, "data": {"errors": errors}, "message": "Validation failed"},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True)
    # Never expose stack traces to clients
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"success": False, "data": None, "message": "An internal error occurred"},
    )


# ---------------------------------------------------------------------------
# Health check (unauthenticated)
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": "gain-backend", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# Register all routers under /api/v1
# ---------------------------------------------------------------------------

API_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(leads.router, prefix=API_PREFIX)
app.include_router(campaigns.router, prefix=API_PREFIX)
app.include_router(agents.router, prefix=API_PREFIX)
app.include_router(tenants.router, prefix=API_PREFIX)
app.include_router(webhooks.router, prefix=API_PREFIX)
app.include_router(calls.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(dashboard.router, prefix=API_PREFIX)

logger.info(f"API prefix: {API_PREFIX}")
logger.info(f"CORS origins: {settings.allowed_origins_list}")
