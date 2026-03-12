"""
Webhook endpoints — ElevenLabs (voice), WAHA (WhatsApp), SendGrid (email).

No JWT auth — verified by HMAC signature instead.

ElevenLabs signature format:
  Header: ElevenLabs-Signature: t=<unix_timestamp>,v0=<hmac_sha256_hex>
  HMAC input: "<timestamp>.<raw_body>"
  Secret: ELEVENLABS_WEBHOOK_SECRET

WAHA and SendGrid webhooks: no signature verification in MVP (add WEBHOOK_SECRET in prod).
"""
import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse

from app.config import settings
from app.utils.logging import get_logger

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
logger = get_logger("routes.webhooks")


def _ok(message: str = "Received") -> dict:
    return {"success": True, "data": None, "message": message}


# ---------------------------------------------------------------------------
# ElevenLabs signature verification
# ---------------------------------------------------------------------------

def _verify_elevenlabs_signature(
    body_bytes: bytes,
    signature_header: Optional[str],
    secret: Optional[str],
    tolerance_seconds: int = 300,
) -> None:
    if not secret:
        logger.warning(
            "ELEVENLABS_WEBHOOK_SECRET not set — skipping signature verification. "
            "Set it in .env before going to production!"
        )
        return

    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing ElevenLabs-Signature header")

    parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
    timestamp_str = parts.get("t")
    v0_signature = parts.get("v0")

    if not timestamp_str or not v0_signature:
        raise HTTPException(
            status_code=401,
            detail="Malformed ElevenLabs-Signature header — expected t=...,v0=..."
        )

    try:
        timestamp = int(timestamp_str)
        if abs(time.time() - timestamp) > tolerance_seconds:
            raise HTTPException(
                status_code=401,
                detail="ElevenLabs webhook timestamp too old (possible replay attack)"
            )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp in ElevenLabs-Signature")

    signed_payload = f"{timestamp_str}.".encode() + body_bytes
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, v0_signature):
        raise HTTPException(status_code=401, detail="ElevenLabs webhook signature invalid")


# ---------------------------------------------------------------------------
# ElevenLabs voice webhooks
# ---------------------------------------------------------------------------

@router.post("/elevenlabs/call-completed")
async def elevenlabs_call_completed(
    request: Request,
    elevenlabs_signature: Optional[str] = Header(default=None, alias="ElevenLabs-Signature"),
):
    """ElevenLabs posts here when a call ends with full transcript."""
    body_bytes = await request.body()
    _verify_elevenlabs_signature(body_bytes, elevenlabs_signature, settings.ELEVENLABS_WEBHOOK_SECRET)

    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    conversation_id = (
        payload.get("conversation_id")
        or payload.get("data", {}).get("conversation_id")
    )
    logger.info(f"ElevenLabs call-completed webhook — conversation_id={conversation_id}")

    try:
        from app.services.voice_service import process_call_completed
        await process_call_completed(payload)
    except Exception as exc:
        logger.error(f"Error processing ElevenLabs call-completed: {exc}", exc_info=True)

    return _ok("Call outcome processed")


@router.post("/elevenlabs/call-status")
async def elevenlabs_call_status(
    request: Request,
    elevenlabs_signature: Optional[str] = Header(default=None, alias="ElevenLabs-Signature"),
):
    """ElevenLabs posts real-time call status updates here."""
    body_bytes = await request.body()
    _verify_elevenlabs_signature(body_bytes, elevenlabs_signature, settings.ELEVENLABS_WEBHOOK_SECRET)

    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    conversation_id = (
        payload.get("conversation_id")
        or payload.get("data", {}).get("conversation_id")
    )
    status = payload.get("status") or payload.get("data", {}).get("status")
    logger.info(f"ElevenLabs call-status — conversation_id={conversation_id} status={status}")

    try:
        from app.services.voice_service import process_call_status_update
        await process_call_status_update(payload)
    except Exception as exc:
        logger.error(f"Error processing ElevenLabs call-status: {exc}", exc_info=True)

    return _ok("Status updated")


# ---------------------------------------------------------------------------
# WAHA — WhatsApp incoming messages and documents
# ---------------------------------------------------------------------------

@router.post("/whatsapp/incoming")
async def whatsapp_incoming(request: Request):
    """
    WAHA posts here on every incoming WhatsApp message or document.

    Pipeline:
      1. Extract sender phone from WAHA payload
      2. Match sender to a lead via mobile_hash index
      3. Save message to whatsapp_messages collection
      4. If media: download from WAHA, upload to S3, enqueue process_whatsapp_document
      5. If text "HELP": resend checklist (enqueue send_doc_checklist_whatsapp)
    """
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # WAHA payload structure: { event: "message", session: "default", payload: { from, body, hasMedia, ... } }
    event = payload.get("event", "")
    msg = payload.get("payload", payload)  # some WAHA versions flatten this

    sender_chat_id = msg.get("from") or msg.get("chatId") or payload.get("sender", "")
    has_media = msg.get("hasMedia", False)
    body_text = msg.get("body", "").strip()
    waha_message_id = msg.get("id", {}).get("id", "") if isinstance(msg.get("id"), dict) else str(msg.get("id", ""))

    logger.info(
        f"WhatsApp incoming — event={event} sender={sender_chat_id} "
        f"hasMedia={has_media} body={body_text[:50]!r}"
    )

    # Only handle message events
    if event and event not in ("message", "message.any"):
        return _ok("Event ignored")

    # Find the lead this sender belongs to
    from app.database import get_db
    from app.services.whatsapp_service import find_lead_by_whatsapp_number
    from datetime import datetime, timezone

    db = get_db()
    now = datetime.now(timezone.utc)

    # Tenant detection: WAHA session name can carry tenant_id, or we search all active tenants.
    # MVP: search across all tenants (acceptable for single-tenant deployment).
    lead = None
    if sender_chat_id:
        # Try each active tenant (in production, session name = tenant_id)
        session = payload.get("session", "default")
        # If session name encodes tenant_id (e.g. "tenant_69b..."), extract it
        tenant_id = _extract_tenant_from_session(session)

        if tenant_id:
            lead = await find_lead_by_whatsapp_number(db, tenant_id, sender_chat_id)
        else:
            # Search across all tenants (MVP fallback)
            async for tenant_doc in db.tenants.find({"is_active": True}):
                tid = str(tenant_doc["_id"])
                lead = await find_lead_by_whatsapp_number(db, tid, sender_chat_id)
                if lead:
                    tenant_id = tid
                    break

    if not lead:
        logger.info(f"WhatsApp: no lead found for sender={sender_chat_id} — ignoring")
        return _ok("Lead not found")

    lead_id = str(lead["_id"])
    tenant_id = lead["tenant_id"]

    # Save inbound message record
    msg_record = {
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "direction": "INBOUND",
        "message_type": "DOCUMENT" if has_media else "TEXT",
        "content": body_text,
        "waha_message_id": waha_message_id,
        "sender_phone": sender_chat_id.split("@")[0],
        "status": "RECEIVED",
        "sent_at": now,
    }
    await db.whatsapp_messages.insert_one(msg_record)

    # Handle HELP command — resend checklist
    if not has_media and body_text.upper() in ("HELP", "HELP ME", "CHECKLIST"):
        from app.workers.whatsapp_worker import send_doc_checklist_whatsapp
        send_doc_checklist_whatsapp.apply_async(
            kwargs={"lead_id": lead_id, "tenant_id": tenant_id},
            queue="whatsapp",
        )
        logger.info(f"HELP command received — re-queued checklist for lead_id={lead_id}")
        return _ok("Checklist resent")

    # Handle document (media)
    if has_media:
        await _process_incoming_media(db, payload, msg, lead_id, tenant_id, waha_message_id, now)

    return _ok("Received")


def _extract_tenant_from_session(session: str) -> Optional[str]:
    """Extract tenant_id from WAHA session name if encoded (e.g. 'tenant_abc123')."""
    if session.startswith("tenant_"):
        return session[7:]
    return None


async def _process_incoming_media(
    db, payload: dict, msg: dict, lead_id: str, tenant_id: str,
    waha_message_id: str, now
) -> None:
    """Download a media file from WAHA, upload to S3, and enqueue processing."""
    import time
    import httpx
    from app.services.storage_service import upload_file, build_s3_key

    if not settings.WAHA_BASE_URL or not settings.WAHA_API_KEY:
        logger.warning("WAHA not configured — cannot download media")
        return

    # WAHA media info
    media_info = msg.get("_data", {}) or msg.get("media", {}) or {}
    original_filename = (
        media_info.get("filename")
        or msg.get("filename")
        or f"document_{int(time.time())}.pdf"
    )
    mimetype = media_info.get("mimetype", "") or msg.get("mimetype", "")

    # Get message ID for download URL
    msg_id = waha_message_id or msg.get("id", "")
    if not msg_id:
        logger.warning(f"No message ID for media download — lead_id={lead_id}")
        return

    try:
        # Download from WAHA: GET /api/messages/{session}/download/{messageId}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{settings.WAHA_BASE_URL.rstrip('/')}/api/messages/default/download/{msg_id}",
                headers={"X-Api-Key": settings.WAHA_API_KEY},
            )
            resp.raise_for_status()
            file_bytes = resp.content

        logger.info(f"Downloaded media from WAHA: {original_filename} ({len(file_bytes)} bytes)")
    except Exception as exc:
        logger.error(f"WAHA media download failed for lead_id={lead_id}: {exc}")
        return

    # Upload to S3
    s3_key = build_s3_key(tenant_id, lead_id, original_filename)
    content_type = mimetype or "application/octet-stream"
    try:
        await upload_file(file_bytes, s3_key, content_type)
    except Exception as exc:
        logger.error(f"S3 upload failed for lead_id={lead_id}: {exc}")
        return

    # Update the whatsapp_message record with S3 key
    await db.whatsapp_messages.update_one(
        {"lead_id": lead_id, "waha_message_id": waha_message_id},
        {"$set": {"media_s3_key": s3_key, "media_filename": original_filename}},
    )

    # Enqueue document processing
    from app.workers.whatsapp_worker import process_whatsapp_document
    process_whatsapp_document.apply_async(
        kwargs={
            "file_s3_key": s3_key,
            "lead_id": lead_id,
            "tenant_id": tenant_id,
            "original_filename": original_filename,
            "file_size_bytes": len(file_bytes),
            "waha_message_id": waha_message_id,
        },
        queue="whatsapp",
    )
    logger.info(f"Media queued for processing: {original_filename} for lead_id={lead_id}")


# ---------------------------------------------------------------------------
# SendGrid — inbound email with attachments
# ---------------------------------------------------------------------------

@router.post("/email/incoming")
async def email_incoming(request: Request):
    """
    SendGrid Inbound Parse posts here when an email with attachments arrives.

    Pipeline:
      1. Parse multipart form (SendGrid format)
      2. Match sender email to a lead record
      3. Extract each attachment: upload to S3, create PhysicalFile record
      4. Enqueue each attachment for AI classification
    """
    try:
        form = await request.form()
    except Exception as exc:
        logger.warning(f"Email incoming: could not parse form — {exc}")
        return _ok("Received")

    sender_email = form.get("from", "")
    subject = form.get("subject", "")
    # SendGrid sets attachments count and attachment content as attachment{N} fields
    attachments_count = int(form.get("attachments", "0") or "0")

    logger.info(
        f"Email incoming — from={sender_email} subject={subject!r} attachments={attachments_count}"
    )

    if not sender_email:
        return _ok("No sender")

    # Parse sender email (can be "Name <email@domain.com>")
    raw_email = sender_email
    if "<" in sender_email:
        raw_email = sender_email.split("<")[-1].rstrip(">").strip()

    # Find lead by email
    from app.database import get_db
    from datetime import datetime, timezone

    db = get_db()
    now = datetime.now(timezone.utc)

    lead = await db.leads.find_one({"email": raw_email})
    if not lead:
        logger.info(f"Email: no lead found for sender={raw_email}")
        return _ok("Lead not found")

    lead_id = str(lead["_id"])
    tenant_id = lead["tenant_id"]

    # Log inbound email in activity feed
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "EMAIL_RECEIVED",
        "message": f"Email received from {raw_email}: {subject[:80]}",
        "created_at": now,
    })

    if attachments_count == 0:
        logger.info(f"Email from {raw_email} has no attachments — ignoring")
        return _ok("No attachments")

    # Process attachments
    from app.services.storage_service import upload_file, build_s3_key
    from app.workers.whatsapp_worker import process_whatsapp_document
    import time

    processed = 0
    for i in range(1, attachments_count + 1):
        attachment_key = f"attachment{i}"
        file_field = form.get(attachment_key)
        if not file_field:
            continue

        # SendGrid provides UploadFile-like object
        try:
            if hasattr(file_field, "read"):
                file_bytes = await file_field.read()
                filename = getattr(file_field, "filename", None) or f"attachment_{i}.pdf"
            elif isinstance(file_field, str):
                # SendGrid may send base64 or raw bytes as string
                file_bytes = file_field.encode("latin-1")
                filename = f"email_attachment_{i}.pdf"
            else:
                continue

            s3_key = build_s3_key(tenant_id, lead_id, filename)
            await upload_file(file_bytes, s3_key)

            # Enqueue document processing (reuse whatsapp worker)
            process_whatsapp_document.apply_async(
                kwargs={
                    "file_s3_key": s3_key,
                    "lead_id": lead_id,
                    "tenant_id": tenant_id,
                    "original_filename": filename,
                    "file_size_bytes": len(file_bytes),
                    "waha_message_id": "",
                },
                queue="whatsapp",
            )
            processed += 1
            logger.info(f"Email attachment queued: {filename} for lead_id={lead_id}")

        except Exception as exc:
            logger.error(f"Failed to process email attachment {i} for lead_id={lead_id}: {exc}")
            continue

    logger.info(f"Email processed: {processed}/{attachments_count} attachments for lead_id={lead_id}")
    return _ok(f"Processed {processed} attachments")


# ---------------------------------------------------------------------------
# Gmail Pub/Sub Push Notification
# ---------------------------------------------------------------------------

@router.post("/gmail-push")
async def gmail_push_notification(request: Request):
    """
    Google Cloud Pub/Sub pushes here instantly when a new email arrives
    at the Gmail inbox (demo.docs@unlockgain.com).

    Pub/Sub payload format:
    {
      "message": {
        "data": "<base64-encoded JSON: {emailAddress, historyId}>",
        "messageId": "...",
        "publishTime": "..."
      },
      "subscription": "projects/.../subscriptions/..."
    }
    """
    try:
        body = await request.json()
    except Exception:
        return _ok("Invalid JSON")

    try:
        message = body.get("message", {})
        data_b64 = message.get("data", "")
        if not data_b64:
            return _ok("No data")

        # Decode the Pub/Sub message payload
        data_json = base64.b64decode(data_b64).decode("utf-8")
        data = json.loads(data_json)

        history_id = str(data.get("historyId", ""))
        email_address = data.get("emailAddress", "")

        logger.info(f"Gmail push received — emailAddress={email_address} historyId={history_id}")

        if not history_id:
            return _ok("No historyId")

        # Process asynchronously — don't block the Pub/Sub acknowledgement
        import asyncio
        from app.services.gmail_service import process_new_messages
        asyncio.create_task(process_new_messages(history_id))

        # Must return 200 quickly to acknowledge the Pub/Sub message
        return _ok("Acknowledged")

    except Exception as exc:
        logger.error(f"Gmail push handler error: {exc}")
        # Still return 200 to prevent Pub/Sub from retrying indefinitely
        return _ok("Error handled")
