"""
Follow-up service — sends reminder nudges to borrowers who haven't submitted all docs.

Schedule: days 1, 3, 5, 7 after the initial doc checklist was sent.
After day 7: escalate to RM via activity_feed.

Called as a Celery periodic task (run hourly via beat scheduler or cron).
"""
from datetime import datetime, timezone, timedelta

from bson import ObjectId

from app.database import get_db
from app.utils.logging import get_logger

logger = get_logger("follow_up_service")

# Days after initial checklist on which to send reminders
_REMINDER_DAYS = [1, 3, 5, 7]


async def check_and_send_reminders() -> dict:
    """
    Find all leads in DOC_COLLECTION status with pending reminders and send them.
    Returns a summary of actions taken.
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    sent = 0
    escalated = 0

    # Find leads in DOC_COLLECTION status
    cursor = db.leads.find({"status": "DOC_COLLECTION"})
    async for lead in cursor:
        try:
            await _process_lead_reminders(db, lead, now)
            sent += 1
        except Exception as exc:
            lead_id = str(lead.get("_id", "?"))
            logger.error(f"Reminder error for lead_id={lead_id}: {exc}")

    logger.info(f"Follow-up check complete: sent={sent} escalations={escalated}")
    return {"sent": sent, "escalated": escalated}


async def _process_lead_reminders(db, lead: dict, now: datetime) -> None:
    """Process reminders for a single lead."""
    lead_id = str(lead["_id"])
    tenant_id = lead["tenant_id"]

    # Find the workflow run that started DOC_COLLECTION
    wf_run = await db.workflow_runs.find_one(
        {"lead_id": lead_id, "tenant_id": tenant_id, "node_type": "DOC_COLLECTION"},
        sort=[("executed_at", 1)],  # earliest = when doc collection started
    )
    if not wf_run:
        return

    collection_started_at = wf_run.get("executed_at", now)
    days_elapsed = (now - collection_started_at).days

    # Find which reminders have already been sent
    already_sent = await _get_sent_reminder_days(db, lead_id, tenant_id)

    for day in _REMINDER_DAYS:
        if day in already_sent:
            continue
        if days_elapsed >= day:
            await _send_reminder_for_day(db, lead, lead_id, tenant_id, day, now)

    # Escalate after day 7 if not already done
    if days_elapsed >= 7 and "escalated" not in already_sent:
        await _escalate_to_rm(db, lead, lead_id, tenant_id, now)


async def _get_sent_reminder_days(db, lead_id: str, tenant_id: str) -> set:
    """Return set of reminder days that have already been sent."""
    sent = set()
    cursor = db.whatsapp_messages.find({
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "direction": "OUTBOUND",
        "template_name": {"$regex": "^REMINDER_DAY_"},
    })
    async for msg in cursor:
        template = msg.get("template_name", "")
        try:
            day = int(template.split("_")[-1])
            sent.add(day)
        except (ValueError, IndexError):
            pass

    # Also check for escalation flag
    escalation = await db.activity_feed.find_one({
        "lead_id": lead_id,
        "event_type": "RM_ESCALATION",
    })
    if escalation:
        sent.add("escalated")
    return sent


async def _send_reminder_for_day(
    db, lead: dict, lead_id: str, tenant_id: str, day: int, now: datetime
) -> None:
    """Send a WhatsApp and/or email reminder on the given day."""
    from app.utils.encryption import decrypt_field

    borrower_name = lead.get("name", "there")
    company_name = lead.get("company_name", "")
    mobile_raw = decrypt_field(lead.get("mobile", ""))
    email = lead.get("email", "")

    # Count missing docs
    missing_count = await _count_missing_docs(db, lead_id, tenant_id, lead.get("entity_type"))

    # Get reminder template from DOC_COLLECTION agent config
    from app.services.validation_rules import get_doc_collection_config
    config = await get_doc_collection_config(db, tenant_id)
    reminder_template = config.get(
        "reminder_whatsapp_template",
        "Hi {{borrower_name}}, reminder: {{pending_docs}} documents are still pending.",
    )
    pending_docs_str = f"{missing_count} document(s)"
    message = (
        reminder_template
        .replace("{{borrower_name}}", borrower_name)
        .replace("{{pending_docs}}", pending_docs_str)
        .replace("{{company_name}}", company_name)
    )

    # Send WhatsApp
    if mobile_raw:
        try:
            from app.services.whatsapp_service import send_text_message
            await send_text_message(lead_id, mobile_raw, message)
            await db.whatsapp_messages.insert_one({
                "lead_id": lead_id,
                "tenant_id": tenant_id,
                "direction": "OUTBOUND",
                "message_type": "TEXT",
                "content": message,
                "status": "SENT",
                "template_name": f"REMINDER_DAY_{day}",
                "sent_at": now,
            })
            logger.info(f"Day {day} WhatsApp reminder sent for lead_id={lead_id}")
        except Exception as exc:
            logger.error(f"WhatsApp reminder (day {day}) failed for lead_id={lead_id}: {exc}")

    # Send email
    if email:
        try:
            from app.services.email_service import send_reminder_email
            await send_reminder_email(
                lead_id=lead_id,
                borrower_name=borrower_name,
                borrower_email=email,
                company_name=company_name,
                pending_docs=[f"{missing_count} documents pending"],
                day=day,
            )
            logger.info(f"Day {day} email reminder sent for lead_id={lead_id}")
        except Exception as exc:
            logger.error(f"Email reminder (day {day}) failed for lead_id={lead_id}: {exc}")


async def _escalate_to_rm(db, lead: dict, lead_id: str, tenant_id: str, now: datetime) -> None:
    """Log an escalation event to the activity feed for the RM to action."""
    assigned_to = lead.get("assigned_to")
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "RM_ESCALATION",
        "message": (
            f"Lead '{lead.get('name')}' ({lead.get('company_name')}) has been in "
            f"DOC_COLLECTION for 7+ days without submitting all documents. "
            f"Please contact the borrower directly."
        ),
        "assigned_to": assigned_to,
        "created_at": now,
    })
    logger.info(f"RM escalation logged for lead_id={lead_id}")


async def _count_missing_docs(db, lead_id: str, tenant_id: str, entity_type: str) -> int:
    """Count required doc types that have not yet been submitted."""
    from app.services.validation_rules import get_doc_collection_config

    config = await get_doc_collection_config(db, tenant_id)
    entity_checklist = config.get("doc_checklist_by_entity_type", {}).get(entity_type or "INDIVIDUAL", {})
    required = set(entity_checklist.get("required", []))
    if not required:
        return 0

    submitted_types = set()
    cursor = db.logical_docs.find({
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "status": {"$in": ["TIER1_PASSED", "HUMAN_REVIEWED", "TIER1_VALIDATING", "EXTRACTING", "EXTRACTED", "READY_FOR_EXTRACTION"]},
    })
    async for doc in cursor:
        submitted_types.add(doc.get("doc_type", ""))

    return len(required - submitted_types)
