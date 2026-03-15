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
    """Send a WhatsApp and/or email reminder on the given day.
    Uses doc_tracker for smart per-doc status in the message.
    """
    from app.utils.encryption import decrypt_field
    from app.services.doc_tracker import get_missing_docs_summary

    borrower_name = lead.get("name", "there")
    company_name = lead.get("company_name", "")
    mobile_raw = decrypt_field(lead.get("mobile", ""))
    email = lead.get("email", "")

    # Get smart doc status from doc_tracker
    doc_summary = await get_missing_docs_summary(db, lead_id, tenant_id)

    # If all docs are done, skip the reminder
    if doc_summary.get("all_done"):
        logger.info(f"All docs complete for lead_id={lead_id}, skipping day {day} reminder")
        return

    labels = {1: "Gentle Reminder", 3: "Reminder", 5: "Important Reminder", 7: "Final Reminder"}
    label = labels.get(day, "Reminder")

    # Build smart WhatsApp message using doc_tracker's per-doc breakdown
    message = (
        f"📋 *{label}: Documents Update*\n\n"
        f"Hi {borrower_name}, here's the current status of your loan application"
        f"{' for ' + company_name if company_name else ''}:\n\n"
        f"{doc_summary.get('message', 'Please submit your pending documents.')}\n\n"
        f"Please reply with any pending documents to continue your application."
    )

    # Send WhatsApp
    if mobile_raw:
        try:
            from app.services.whatsapp_service import send_text_message
            await send_text_message(lead_id, mobile_raw, message, tenant_id=tenant_id)
            logger.info(f"Day {day} WhatsApp reminder sent for lead_id={lead_id}")
        except Exception as exc:
            logger.error(f"WhatsApp reminder (day {day}) failed for lead_id={lead_id}: {exc}")

    # Send email with smart doc status
    if email:
        try:
            pending = doc_summary.get("pending_docs", [])
            received = doc_summary.get("received_docs", [])
            failed = doc_summary.get("failed_docs", [])

            # Build HTML email body with per-doc status
            html_sections = [f"<p>Dear {borrower_name},</p>"]
            html_sections.append(
                f"<p>This is a <strong>{label.lower()}</strong> regarding your loan application"
                f"{' for ' + company_name if company_name else ''}.</p>"
            )

            if received:
                html_sections.append("<p><strong>✅ Received & Verified:</strong></p><ul>")
                for doc in received:
                    html_sections.append(f"<li>{doc}</li>")
                html_sections.append("</ul>")

            if failed:
                html_sections.append("<p><strong>❌ Needs Resubmission:</strong></p><ul>")
                for doc in failed:
                    html_sections.append(f"<li>{doc}</li>")
                html_sections.append("</ul>")

            if pending:
                html_sections.append("<p><strong>📋 Still Needed:</strong></p><ul>")
                for doc in pending:
                    html_sections.append(f"<li>{doc}</li>")
                html_sections.append("</ul>")

            html_sections.append(
                "<p>Please reply to this email with the pending documents attached.</p>"
                "<p>Best regards,<br/>Gain AI Lending Team</p>"
            )
            body_html = "\n".join(html_sections)

            from app.services.email_service import send_status_update_email, get_lead_email_subject
            await send_status_update_email(
                lead_id=lead_id,
                tenant_id=tenant_id,
                borrower_email=email,
                subject=f"Re: {get_lead_email_subject(company_name)}",
                body_html=body_html,
            )
            logger.info(f"Day {day} email reminder sent for lead_id={lead_id}")
        except Exception as exc:
            logger.error(f"Email reminder (day {day}) failed for lead_id={lead_id}: {exc}")

    # Log to activity_feed for Activity tab visibility
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "FOLLOW_UP_SENT",
        "message": f"Day {day} follow-up reminder sent. {len(doc_summary.get('pending_docs', []))} docs pending.",
        "detail": {
            "day": day,
            "pending_count": len(doc_summary.get("pending_docs", [])),
            "received_count": len(doc_summary.get("received_docs", [])),
            "failed_count": len(doc_summary.get("failed_docs", [])),
        },
        "created_at": now,
    })


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


    # _count_missing_docs removed — now uses doc_tracker.get_missing_docs_summary()
