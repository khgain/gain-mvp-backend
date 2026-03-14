"""
Follow-up Worker — runs doc collection reminders.

Schedule: runs hourly. Checks all leads in DOC_COLLECTION status and sends
WhatsApp/email nudges on days 1, 3, 5, 7. Escalates to RM after day 7.

Two modes:
  1. Celery Beat (production with SQS) — periodic task via celery_app
  2. Direct call via FastAPI background task (eager mode / Railway)
"""
import asyncio
import os
import logging

from app.celery_app import celery_app

logger = logging.getLogger("gain.workers.follow_up")

_always_eager = os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() == "true"


@celery_app.task(name="tasks.run_follow_up_check", queue="whatsapp")
def run_follow_up_check() -> dict:
    """Celery task: check all leads in DOC_COLLECTION for pending reminders.
    In eager mode, this may be called from within an already-running event loop,
    so we handle both cases.
    """
    logger.info("[FOLLOW-UP] Starting follow-up check")
    try:
        if _always_eager:
            # In eager mode, we might be inside FastAPI's event loop.
            # Try to get the running loop — if it exists, schedule as a task.
            try:
                loop = asyncio.get_running_loop()
                # We're inside an event loop — use nest_asyncio (applied in celery_app)
                import nest_asyncio
                nest_asyncio.apply()
                result = asyncio.run(_run_async())
            except RuntimeError:
                # No running loop — safe to use asyncio.run()
                result = asyncio.run(_run_async())
        else:
            result = asyncio.run(_run_async())
        return result
    except Exception as exc:
        logger.error(f"[FOLLOW-UP] Failed: {exc}")
        return {"status": "error", "error": str(exc)}


async def run_follow_up_check_async() -> dict:
    """Direct async entry point — call from FastAPI background tasks.
    Bypasses Celery entirely. Use this when running in eager mode.
    """
    logger.info("[FOLLOW-UP] Starting async follow-up check (direct)")
    try:
        return await _run_async()
    except Exception as exc:
        logger.error(f"[FOLLOW-UP] Async check failed: {exc}")
        return {"status": "error", "error": str(exc)}


async def _run_async() -> dict:
    from app.services.follow_up_service import check_and_send_reminders
    return await check_and_send_reminders()
