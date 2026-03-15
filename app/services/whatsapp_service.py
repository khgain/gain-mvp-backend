"""
WhatsApp service — sends messages and receives documents via WAHA (WhatsApp HTTP API).

WAHA endpoints used:
  POST /api/sendText   — plain text messages
  POST /api/sendFile   — PDF/image with caption
  GET  /api/contacts/check-exists — verify number is on WhatsApp

Incoming documents are processed in the webhook handler (routes/webhooks.py)
which calls save_whatsapp_document() below.
"""
import base64
import hashlib
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger("whatsapp_service")

# Document checklist per entity type + loan type
_DOC_CHECKLISTS = {
    "PROPRIETORSHIP": [
        "Aadhaar Card (front & back)",
        "PAN Card",
        "Bank Statement (last 12 months)",
        "Latest ITR with computation",
        "GST Certificate",
        "UDYAM Registration Certificate",
    ],
    "PARTNERSHIP": [
        "Aadhaar Card of all partners",
        "PAN Card of firm and all partners",
        "Partnership Deed",
        "Bank Statement (last 12 months)",
        "Latest 2 years ITR / Audited P&L",
        "GST Certificate + Returns (last 6 months)",
    ],
    "PRIVATE_LIMITED": [
        "Aadhaar + PAN of all directors",
        "Certificate of Incorporation (COI)",
        "MOA + AOA",
        "Bank Statement (last 12 months)",
        "Audited P&L + Balance Sheet (2 years)",
        "GST Certificate + Returns (last 6 months)",
    ],
    "DEFAULT": [
        "Aadhaar Card",
        "PAN Card",
        "Bank Statement (last 12 months)",
        "Latest ITR",
        "Address Proof",
    ],
}


def _whatsapp_number(mobile: str) -> str:
    """Convert 10-digit Indian mobile to WAHA chatId format: 919876543210@c.us"""
    digits = "".join(c for c in mobile if c.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        return f"{digits}@c.us"
    if len(digits) == 10:
        return f"91{digits}@c.us"
    return f"{digits}@c.us"


def _build_checklist_message(name: str, entity_type: Optional[str], loan_amount_paise: Optional[int]) -> str:
    checklist_key = entity_type or "DEFAULT"
    docs = _DOC_CHECKLISTS.get(checklist_key, _DOC_CHECKLISTS["DEFAULT"])

    amount_str = ""
    if loan_amount_paise:
        amount_lakhs = loan_amount_paise / 10_000_000  # paise → lakhs
        amount_str = f"₹{amount_lakhs:.1f}L loan application"

    doc_list = "\n".join(f"  {i+1}. {doc}" for i, doc in enumerate(docs))

    return (
        f"Hello {name}! 👋\n\n"
        f"Thank you for your {amount_str + ' with ' if amount_str else ''}Gain AI.\n\n"
        f"Please send the following documents to continue your application:\n\n"
        f"{doc_list}\n\n"
        f"You can send documents one by one or all at once as a ZIP file.\n"
        f"Reply HELP if you need assistance."
    )


def _build_reminder_message(name: str, day: int, missing_count: int) -> str:
    urgency = {1: "gentle reminder", 3: "reminder", 5: "important reminder", 7: "final reminder"}
    label = urgency.get(day, "reminder")
    return (
        f"Hi {name}, this is a {label}. 📋\n\n"
        f"We are still waiting for {missing_count} document(s) to process your loan application.\n\n"
        f"Please send the remaining documents at your earliest convenience.\n"
        f"Type HELP if you need the checklist again."
    )


async def send_document_checklist(
    lead_id: str,
    mobile: str,
    name: str,
    entity_type: Optional[str] = None,
    loan_amount_paise: Optional[int] = None,
    tenant_id: Optional[str] = None,
) -> bool:
    """Send the initial document checklist via WhatsApp. Returns True on success."""
    if not settings.WAHA_BASE_URL or not settings.WAHA_API_KEY:
        logger.warning(f"WAHA not configured — skip WhatsApp checklist for lead_id={lead_id}")
        return False

    chat_id = _whatsapp_number(mobile)
    message = _build_checklist_message(name, entity_type, loan_amount_paise)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.WAHA_BASE_URL.rstrip('/')}/api/sendText",
                json={"session": "default", "chatId": chat_id, "text": message},
                headers={"X-Api-Key": settings.WAHA_API_KEY},
            )
            resp.raise_for_status()
            # Capture chatId from WAHA response (may be LID format)
            resp_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            await _cache_lid_from_response(lead_id, resp_data)
        logger.info(f"WhatsApp checklist sent — lead_id={lead_id} chat_id={chat_id}")

        # Persist outbound message for WhatsApp tab
        await _persist_outbound_message(
            lead_id, tenant_id, message, "TEMPLATE", "DOC_CHECKLIST",
        )
        return True
    except Exception as exc:
        logger.error(f"WhatsApp send failed for lead_id={lead_id}: {exc}")
        return False


async def send_reminder(
    lead_id: str,
    mobile: str,
    name: str,
    day: int,
    missing_count: int,
) -> bool:
    """Send follow-up reminder on days 1, 3, 5, 7 of doc collection."""
    if not settings.WAHA_BASE_URL or not settings.WAHA_API_KEY:
        return False

    chat_id = _whatsapp_number(mobile)
    message = _build_reminder_message(name, day, missing_count)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.WAHA_BASE_URL.rstrip('/')}/api/sendText",
                json={"session": "default", "chatId": chat_id, "text": message},
                headers={"X-Api-Key": settings.WAHA_API_KEY},
            )
            resp.raise_for_status()
        logger.info(f"WhatsApp reminder (day {day}) sent — lead_id={lead_id}")
        return True
    except Exception as exc:
        logger.error(f"WhatsApp reminder failed for lead_id={lead_id}: {exc}")
        return False


def compute_mobile_hash(mobile: str) -> str:
    """
    One-way SHA256 hash of the normalized 10-digit mobile.
    Used as a search index so we can look up leads from WhatsApp webhook
    without decrypting every record.
    """
    digits = "".join(c for c in mobile if c.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]  # strip country code
    if len(digits) > 10:
        digits = digits[-10:]
    return hashlib.sha256(digits.encode()).hexdigest()


async def find_lead_by_whatsapp_number(db, tenant_id: str, sender_chat_id: str) -> Optional[dict]:
    """
    Match incoming WAHA sender to a lead using mobile_hash index.
    sender_chat_id format: "919876543210@c.us"
    """
    digits = sender_chat_id.split("@")[0]  # "919876543210"
    mobile_hash = compute_mobile_hash(digits)

    lead = await db.leads.find_one({"tenant_id": tenant_id, "mobile_hash": mobile_hash})
    return lead


async def send_text_message(lead_id: str, mobile: str, text: str, tenant_id: Optional[str] = None) -> bool:
    """Low-level helper — send a plain text WhatsApp message via WAHA."""
    if not settings.WAHA_BASE_URL or not settings.WAHA_API_KEY:
        logger.warning(f"WAHA not configured — skip send_text_message for lead_id={lead_id}")
        return False
    chat_id = _whatsapp_number(mobile)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.WAHA_BASE_URL.rstrip('/')}/api/sendText",
                json={"session": "default", "chatId": chat_id, "text": text},
                headers={"X-Api-Key": settings.WAHA_API_KEY},
            )
            resp.raise_for_status()
            resp_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            await _cache_lid_from_response(lead_id, resp_data)

        # Persist outbound message for WhatsApp tab
        await _persist_outbound_message(lead_id, tenant_id, text, "TEXT", None)
        return True
    except Exception as exc:
        logger.error(f"send_text_message failed for lead_id={lead_id}: {exc}")
        return False


async def _cache_lid_from_response(lead_id: str, resp_data: dict) -> None:
    """
    If WAHA response contains a LID-format chatId, cache it on the lead
    for future inbound message matching.
    """
    try:
        # WAHA sendText response may include: { "id": {...}, "chatId": "243...@lid", ... }
        chat_id = resp_data.get("chatId", "") or ""
        # Also check nested structures
        if not chat_id:
            key = resp_data.get("key", {})
            if isinstance(key, dict):
                chat_id = key.get("remoteJid", "")
        if chat_id and "@lid" in chat_id:
            from app.database import get_db
            from bson import ObjectId
            db = get_db()
            await db.leads.update_one(
                {"_id": ObjectId(lead_id)},
                {"$set": {"whatsapp_lid": chat_id}},
            )
            logger.info(f"[LID CACHE] Stored whatsapp_lid={chat_id} on lead_id={lead_id}")
    except Exception as exc:
        logger.warning(f"[LID CACHE] Failed to cache LID for lead_id={lead_id}: {exc}")


async def _persist_outbound_message(
    lead_id: str, tenant_id: Optional[str], content: str,
    message_type: str = "TEXT", template_name: Optional[str] = None,
) -> None:
    """Persist outbound WhatsApp message to whatsapp_messages for UI display."""
    try:
        from app.database import get_db
        db = get_db()
        now = datetime.now(timezone.utc)

        # If no tenant_id passed, look it up from the lead
        if not tenant_id:
            from bson import ObjectId
            lead = await db.leads.find_one({"_id": ObjectId(lead_id)})
            tenant_id = lead.get("tenant_id", "") if lead else ""

        await db.whatsapp_messages.insert_one({
            "lead_id": lead_id,
            "tenant_id": tenant_id,
            "direction": "OUTBOUND",
            "message_type": message_type,
            "content": content[:2000],  # cap at 2000 chars
            "status": "SENT",
            "template_name": template_name,
            "sent_at": now,
        })
    except Exception as exc:
        logger.warning(f"Failed to persist outbound WhatsApp message: {exc}")
