"""
Email service — SendGrid v3 REST API for outbound document checklist emails.

Uses httpx directly (no sendgrid SDK).
Incoming emails are handled by the SendGrid Inbound Parse webhook in routes/webhooks.py.
"""
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger("email_service")

SENDGRID_SEND_URL = "https://api.sendgrid.com/v3/mail/send"

_FROM_EMAIL = "ops@unlockgain.com"       # Verified SendGrid sender
_INBOUND_EMAIL = "demo.docs@unlockgain.com"  # Borrowers reply/send docs to this address
_FROM_NAME = "Gain AI"


def _inject(template: str, variables: dict) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", str(value) if value else "")
    return result


def _fmt_text(docs: list[str]) -> str:
    return "\n".join(f"  {i + 1}. {doc}" for i, doc in enumerate(docs))


def _fmt_html(docs: list[str]) -> str:
    items = "".join(f"<li>{doc}</li>" for doc in docs)
    return f"<ol>{items}</ol>"


async def send_document_checklist_email(
    lead_id: str,
    tenant_id: str,
    borrower_name: str,
    borrower_email: str,
    company_name: str,
    loan_type: str,
    doc_list: list[str],
    subject_template: str,
    body_template: str,
) -> bool:
    """Send the initial document checklist email via SendGrid."""
    if not settings.SENDGRID_API_KEY:
        logger.warning(f"SENDGRID_API_KEY not configured — skipping email for lead_id={lead_id}")
        return False
    if not borrower_email:
        logger.warning(f"Lead {lead_id} has no email — skipping email checklist")
        return False

    variables = {
        "borrower_name": borrower_name,
        "company_name": company_name,
        "loan_type": loan_type.replace("_", " ").title(),
        "doc_list": _fmt_text(doc_list),
    }
    subject = _inject(subject_template, variables)
    plain_body = _inject(body_template, variables)
    html_body = (
        f"<html><body><pre style='font-family:sans-serif;white-space:pre-wrap'>"
        f"{_inject(body_template, {**variables, 'doc_list': _fmt_html(doc_list)})}"
        f"</pre></body></html>"
    )

    payload = {
        "personalizations": [{"to": [{"email": borrower_email, "name": borrower_name}]}],
        "from": {"email": _FROM_EMAIL, "name": _FROM_NAME},
        "reply_to": {"email": _INBOUND_EMAIL, "name": _FROM_NAME},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain_body},
            {"type": "text/html", "value": html_body},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                SENDGRID_SEND_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"},
            )
            if resp.status_code not in (200, 202):
                logger.error(
                    f"SendGrid {resp.status_code} for lead_id={lead_id}: {resp.text[:300]}"
                )
                return False
        logger.info(f"Email checklist sent — lead_id={lead_id} to={borrower_email}")
        return True
    except Exception as exc:
        logger.error(f"Email send failed for lead_id={lead_id}: {exc}")
        return False


async def send_reminder_email(
    lead_id: str,
    borrower_name: str,
    borrower_email: str,
    company_name: str,
    pending_docs: list[str],
    day: int,
) -> bool:
    """Send a follow-up reminder email on days 1, 3, 5, 7."""
    if not settings.SENDGRID_API_KEY or not borrower_email:
        return False

    labels = {1: "Gentle Reminder", 3: "Reminder", 5: "Important Reminder", 7: "Final Reminder"}
    label = labels.get(day, "Reminder")
    doc_text = _fmt_text(pending_docs)

    subject = f"{label}: Documents Pending — {company_name} Loan Application"
    body = (
        f"Dear {borrower_name},\n\n"
        f"This is a {label.lower()} regarding your loan application for {company_name}.\n\n"
        f"We are still awaiting:\n\n{doc_text}\n\n"
        f"Please reply to this email with the documents attached.\n\n"
        f"Regards,\nGain AI Operations Team"
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                SENDGRID_SEND_URL,
                json={
                    "personalizations": [{"to": [{"email": borrower_email}]}],
                    "from": {"email": _FROM_EMAIL, "name": _FROM_NAME},
                    "subject": subject,
                    "content": [{"type": "text/plain", "value": body}],
                },
                headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"},
            )
            return resp.status_code in (200, 202)
    except Exception as exc:
        logger.error(f"Reminder email failed for lead_id={lead_id}: {exc}")
        return False


async def send_doc_status_update_email(
    lead_id: str,
    tenant_id: str,
) -> bool:
    """
    Send a document status update email to the borrower after each tier 1 validation.

    Queries all logical_docs for this lead and categorises them:
      ✅ Verified — TIER1_PASSED or TIER2_PASSED
      ❌ Issues found — TIER1_FAILED (includes specific failure reason)
      ⏳ Still required — not yet received

    Loops until all required docs are verified, then sends a final confirmation.
    """
    if not settings.SENDGRID_API_KEY:
        return False

    from app.database import get_db
    from bson import ObjectId

    db = get_db()

    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
    if not lead or not lead.get("email"):
        return False

    borrower_email = lead["email"]
    borrower_name = lead.get("name", "Borrower")
    company_name = lead.get("company_name") or borrower_name
    entity_type = lead.get("entity_type", "INDIVIDUAL")

    # Get required doc types from DOC_COLLECTION config
    from app.services.validation_rules import get_doc_collection_config
    doc_config = await get_doc_collection_config(db, tenant_id)
    entity_checklist = doc_config.get("doc_checklist_by_entity_type", {}).get(entity_type, {})
    required_types = set(entity_checklist.get("required", []))
    optional_types = set(entity_checklist.get("optional", []))
    all_expected = required_types | optional_types

    # Get current state of all docs for this lead
    verified, failed, missing = [], [], []
    received_types = set()

    cursor = db.logical_docs.find({"lead_id": lead_id, "tenant_id": tenant_id})
    async for doc in cursor:
        doc_type = doc.get("doc_type", "Document")
        label = doc_type.replace("_", " ").title()
        status = doc.get("status", "")
        received_types.add(doc_type)

        if status in ("TIER1_PASSED", "TIER2_PASSED", "HUMAN_APPROVED"):
            verified.append(f"{label}")
        elif status == "TIER1_FAILED":
            reasons = []
            for rr in doc.get("tier1_validation", {}).get("rule_results", []):
                if not rr.get("passed") and rr.get("message"):
                    reasons.append(rr["message"])
            reason_str = "; ".join(reasons) if reasons else "Document did not meet requirements"
            failed.append(f"{label}: {reason_str}")

    # Required docs not yet received
    for doc_type in sorted(required_types - received_types):
        missing.append(doc_type.replace("_", " ").title())

    all_done = len(failed) == 0 and len(missing) == 0 and len(verified) > 0

    # Build email content
    if all_done:
        subject = f"All Documents Received — {company_name} Application Under Review"
        plain = (
            f"Dear {borrower_name},\n\n"
            f"Great news! We have received and verified all required documents for your loan application.\n\n"
            f"✅ Verified Documents:\n" + "\n".join(f"  • {d}" for d in verified) + "\n\n"
            f"Your application is now under review. Our team will be in touch shortly.\n\n"
            f"Regards,\nGain AI Operations Team"
        )
        html = (
            f"<html><body style='font-family:sans-serif;max-width:600px'>"
            f"<p>Dear <strong>{borrower_name}</strong>,</p>"
            f"<p>Great news! We have received and verified all required documents for your loan application.</p>"
            f"<h3 style='color:#076653'>✅ Verified Documents</h3>"
            f"<ul>{''.join(f'<li>{d}</li>' for d in verified)}</ul>"
            f"<p>Your application is now under review. Our team will be in touch shortly.</p>"
            f"<p>Regards,<br><strong>Gain AI Operations Team</strong></p>"
            f"</body></html>"
        )
    else:
        sections_plain, sections_html = [], []

        if verified:
            sections_plain.append("✅ Received & Verified:\n" + "\n".join(f"  • {d}" for d in verified))
            sections_html.append(
                f"<h3 style='color:#076653'>✅ Received &amp; Verified</h3>"
                f"<ul>{''.join(f'<li>{d}</li>' for d in verified)}</ul>"
            )

        if failed:
            sections_plain.append("❌ Issues Found — Please Resubmit:\n" + "\n".join(f"  • {d}" for d in failed))
            sections_html.append(
                f"<h3 style='color:#d32f2f'>❌ Issues Found — Please Resubmit</h3>"
                f"<ul>{''.join(f'<li>{d}</li>' for d in failed)}</ul>"
            )

        if missing:
            sections_plain.append("⏳ Still Required:\n" + "\n".join(f"  • {d}" for d in missing))
            sections_html.append(
                f"<h3 style='color:#f57c00'>⏳ Still Required</h3>"
                f"<ul>{''.join(f'<li>{d}</li>' for d in missing)}</ul>"
            )

        action_count = len(failed) + len(missing)
        subject = f"Action Required: {action_count} Document(s) Needed — {company_name}"
        plain = (
            f"Dear {borrower_name},\n\n"
            f"Here is the current status of your loan application documents:\n\n"
            + "\n\n".join(sections_plain)
            + "\n\n"
            f"Please reply to this email with the corrected or missing documents attached.\n\n"
            f"Regards,\nGain AI Operations Team\n{_INBOUND_EMAIL}"
        )
        html = (
            f"<html><body style='font-family:sans-serif;max-width:600px'>"
            f"<p>Dear <strong>{borrower_name}</strong>,</p>"
            f"<p>Here is the current status of your loan application documents:</p>"
            + "".join(sections_html)
            + f"<p><strong>Please reply to this email</strong> with the corrected or missing documents attached.</p>"
            f"<p>Regards,<br><strong>Gain AI Operations Team</strong><br>{_INBOUND_EMAIL}</p>"
            f"</body></html>"
        )

    payload = {
        "personalizations": [{"to": [{"email": borrower_email, "name": borrower_name}]}],
        "from": {"email": _FROM_EMAIL, "name": _FROM_NAME},
        "reply_to": {"email": _INBOUND_EMAIL, "name": _FROM_NAME},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain},
            {"type": "text/html", "value": html},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                SENDGRID_SEND_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"},
            )
            if resp.status_code not in (200, 202):
                logger.error(f"SendGrid {resp.status_code} for doc status email lead_id={lead_id}: {resp.text[:200]}")
                return False

        logger.info(f"Doc status email sent — lead_id={lead_id} to={borrower_email} all_done={all_done}")

        # Log in activity feed
        db2 = get_db()
        await db2.activity_feed.insert_one({
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "event_type": "EMAIL_SENT",
            "message": subject,
            "created_at": datetime.now(timezone.utc),
        })
        return True

    except Exception as exc:
        logger.error(f"Doc status email failed for lead_id={lead_id}: {exc}")
        return False


async def send_status_update_email(
    lead_id: str,
    tenant_id: str,
    borrower_email: str,
    subject: str,
    body_html: str,
) -> bool:
    """Send a generic status update email with HTML body."""
    if not settings.SENDGRID_API_KEY:
        logger.warning("SENDGRID_API_KEY not set — skipping status email")
        return False

    payload = {
        "personalizations": [{"to": [{"email": borrower_email}]}],
        "from": {"email": _FROM_EMAIL, "name": _FROM_NAME},
        "reply_to": {"email": _INBOUND_EMAIL, "name": _FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/html", "value": f"<html><body>{body_html}</body></html>"}],
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                SENDGRID_SEND_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.SENDGRID_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code in (200, 201, 202):
                logger.info(f"Status update email sent to {borrower_email} for lead_id={lead_id}")
                return True
            else:
                logger.error(f"SendGrid error {resp.status_code}: {resp.text[:200]}")
                return False
    except Exception as exc:
        logger.error(f"Status update email failed for lead_id={lead_id}: {exc}")
        return False


async def log_outbound_email(
    db,
    lead_id: str,
    tenant_id: str,
    to_email: str,
    subject: str,
) -> None:
    """Record an outbound email in activity_feed for audit trail."""
    try:
        await db.activity_feed.insert_one({
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "event_type": "EMAIL_SENT",
            "message": f"Email sent to {to_email}: {subject[:80]}",
            "created_at": datetime.now(timezone.utc),
        })
    except Exception as exc:
        logger.error(f"Failed to log outbound email for lead_id={lead_id}: {exc}")
