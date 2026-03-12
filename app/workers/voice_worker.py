"""
Voice Worker — Celery task for outbound ElevenLabs Conversational AI calls.

Runs in a separate Celery worker process. Uses asyncio.run() to call the async
voice_service.trigger_outbound_call(). On retry failures, falls back to WhatsApp.
"""
import asyncio
import logging

from app.celery_app import celery_app

logger = logging.getLogger("gain.workers.voice")


@celery_app.task(name="tasks.place_voice_call", queue="voice", bind=True, max_retries=3)
def place_voice_call(self, lead_id: str, tenant_id: str) -> dict:
    """
    Place an outbound qualification call via ElevenLabs Conversational AI.

    Reads VOICE_AI agent config from DB, injects lead dynamic variables,
    and calls the ElevenLabs /v1/convai/twilio/outbound-call API.

    Retry logic:
      - Retries up to max_retries (3) times with 2-hour countdown.
      - After all retries exhausted, falls back to WhatsApp checklist.
    """
    logger.info(f"[VOICE WORKER] Processing call — lead_id={lead_id}")

    try:
        from app.services.voice_service import trigger_outbound_call, should_retry_call

        # Run the async service function synchronously inside Celery task
        conversation_id = asyncio.run(trigger_outbound_call(lead_id, tenant_id))

        if conversation_id:
            logger.info(
                f"[VOICE WORKER] Call placed — lead_id={lead_id} "
                f"conversation_id={conversation_id}"
            )
            return {"status": "placed", "lead_id": lead_id, "conversation_id": conversation_id}
        else:
            logger.warning(
                f"[VOICE WORKER] Call not placed (ElevenLabs not configured or error) "
                f"— lead_id={lead_id}"
            )
            return {"status": "skipped", "lead_id": lead_id, "reason": "not_configured"}

    except Exception as exc:
        logger.error(f"[VOICE WORKER] Task failed for lead_id={lead_id}: {exc}")

        # Check if we've exhausted retries — if so, fall back to WhatsApp
        if self.request.retries >= self.max_retries - 1:
            logger.warning(
                f"[VOICE WORKER] Max retries reached for lead_id={lead_id}. "
                "Falling back to WhatsApp message."
            )
            try:
                _fallback_to_whatsapp(lead_id, tenant_id)
            except Exception as wb_exc:
                logger.error(
                    f"[VOICE WORKER] WhatsApp fallback also failed for lead_id={lead_id}: {wb_exc}"
                )
            return {"status": "failed_fallback_sent", "lead_id": lead_id}

        # Retry after 2 hours (7200 seconds)
        raise self.retry(exc=exc, countdown=60 * 60 * 2)


def _fallback_to_whatsapp(lead_id: str, tenant_id: str) -> None:
    """
    Send a WhatsApp message to borrower after voice call attempts are exhausted.
    Enqueues the doc checklist WhatsApp task.
    """
    try:
        from app.workers.whatsapp_worker import send_doc_checklist_whatsapp
        send_doc_checklist_whatsapp.apply_async(
            kwargs={"lead_id": lead_id, "tenant_id": tenant_id},
            queue="whatsapp",
        )
        logger.info(
            f"[VOICE WORKER] Fallback WhatsApp enqueued for lead_id={lead_id}"
        )
    except Exception as exc:
        logger.error(f"[VOICE WORKER] Failed to enqueue WhatsApp fallback: {exc}")


@celery_app.task(name="tasks.send_doc_checklist_whatsapp", queue="whatsapp", bind=True, max_retries=3)
def send_doc_checklist_whatsapp(self, lead_id: str, tenant_id: str) -> dict:
    """Send document checklist via WhatsApp. Full implementation in Day 2."""
    logger.info(f"[WHATSAPP WORKER] Sending checklist — lead_id={lead_id}")
    try:
        from app.workers.whatsapp_worker import send_doc_checklist_whatsapp as wa_task
        # Delegate to the whatsapp_worker implementation
        wa_task.apply_async(
            kwargs={"lead_id": lead_id, "tenant_id": tenant_id},
            queue="whatsapp",
        )
        return {"status": "delegated", "lead_id": lead_id}
    except Exception as exc:
        logger.error(f"[VOICE WORKER] WhatsApp checklist failed — lead_id={lead_id}: {exc}")
        return {"status": "error", "lead_id": lead_id}


@celery_app.task(name="tasks.send_doc_checklist_email", queue="email", bind=True, max_retries=3)
def send_doc_checklist_email(self, lead_id: str, tenant_id: str) -> dict:
    """Send document checklist via email. Full implementation in Day 2."""
    logger.info(f"[EMAIL WORKER] Sending checklist — lead_id={lead_id}")
    return {"status": "stub", "lead_id": lead_id}
