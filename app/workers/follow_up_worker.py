"""
Follow-up Worker — Celery periodic task that sends doc collection reminders.

Schedule: runs hourly. Checks all leads in DOC_COLLECTION status and sends
WhatsApp/email nudges on days 1, 3, 5, 7. Escalates to RM after day 7.
"""
import asyncio
import logging

from app.celery_app import celery_app

logger = logging.getLogger("gain.workers.follow_up")


@celery_app.task(name="tasks.run_follow_up_check", queue="whatsapp")
def run_follow_up_check() -> dict:
    """Hourly task: check all leads in DOC_COLLECTION for pending reminders."""
    logger.info("[FOLLOW-UP] Starting hourly follow-up check")
    try:
        result = asyncio.run(_run_async())
        return result
    except Exception as exc:
        logger.error(f"[FOLLOW-UP] Failed: {exc}")
        return {"status": "error", "error": str(exc)}


async def _run_async() -> dict:
    from app.services.follow_up_service import check_and_send_reminders
    return await check_and_send_reminders()
