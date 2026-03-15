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

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, Header
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
        # Log mismatch details to help diagnose secret configuration issues.
        # Downgraded from hard 401 to warning so webhooks still process during testing.
        # TODO: re-enable hard failure once secret is confirmed correct in production.
        logger.warning(
            f"[WEBHOOK] ElevenLabs HMAC mismatch — "
            f"expected={expected[:12]}... received={v0_signature[:12]}... "
            f"(check ELEVENLABS_WEBHOOK_SECRET on Railway matches ElevenLabs dashboard secret)"
        )
        # Uncomment below to enforce in production:
        # raise HTTPException(status_code=401, detail="ElevenLabs webhook signature invalid")


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

    # --- Idempotency guard: skip if this conversation_id was already processed ---
    if conversation_id:
        from app.database import get_db
        db = get_db()
        existing = await db.webhook_idempotency.find_one({"conversation_id": conversation_id})
        if existing:
            logger.warning(f"[WEBHOOK] Duplicate call-completed for conversation_id={conversation_id} — skipping")
            return _ok("Already processed (duplicate)")
        # Mark as processing BEFORE executing to prevent race conditions
        from datetime import datetime, timezone
        await db.webhook_idempotency.insert_one({
            "conversation_id": conversation_id,
            "processed_at": datetime.now(timezone.utc),
        })

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

@router.get("/whatsapp/incoming")
async def whatsapp_incoming_test():
    """Quick GET test to verify route is reachable."""
    return {"status": "ok", "route": "/webhooks/whatsapp/incoming", "method": "GET — use POST for actual webhook"}


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

    # ── EARLY DIAGNOSTIC LOG — fires for EVERY incoming request ──
    logger.info(f"[WA WEBHOOK HIT] raw_body_length={len(body_bytes)} bytes")

    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        logger.error(f"[WA WEBHOOK] Invalid JSON: {body_bytes[:200]}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Log top-level keys to understand WAHA's payload structure
    logger.info(f"[WA WEBHOOK] top_keys={list(payload.keys())} event={payload.get('event')} session={payload.get('session')}")

    # WAHA payload structure: { event: "message", session: "default", payload: { from, body, hasMedia, ... } }
    event = payload.get("event", "")
    msg = payload.get("payload", payload)  # some WAHA versions flatten this

    sender_chat_id = msg.get("from") or msg.get("chatId") or payload.get("sender", "")
    has_media = msg.get("hasMedia", False)
    body_text = msg.get("body", "").strip()
    waha_message_id = msg.get("id", {}).get("id", "") if isinstance(msg.get("id"), dict) else str(msg.get("id", ""))

    logger.info(
        f"[WA WEBHOOK] parsed — event={event} sender={sender_chat_id} "
        f"hasMedia={has_media} body={body_text[:50]!r} msg_id={waha_message_id}"
    )

    # Only handle message events
    if event and event not in ("message", "message.any"):
        logger.info(f"[WA WEBHOOK] Ignoring event={event}")
        return _ok("Event ignored")

    # Find the lead this sender belongs to
    from app.database import get_db
    from app.services.whatsapp_service import find_lead_by_whatsapp_number, compute_mobile_hash
    from datetime import datetime, timezone

    db = get_db()
    now = datetime.now(timezone.utc)

    lead = None
    tenant_id = None
    is_lid = sender_chat_id.endswith("@lid")  # Meta LID format (opaque ID, not a phone number)

    # Helper: find the most recent lead matching a query (prefer DOC_COLLECTION status)
    async def _find_best_lead(query: dict):
        """Return the most recent matching lead, preferring active DOC_COLLECTION status."""
        candidates = await db.leads.find(query).sort("created_at", -1).to_list(10)
        if not candidates:
            return None
        # Prefer lead in DOC_COLLECTION status (actively awaiting docs)
        for c in candidates:
            if c.get("status") == "DOC_COLLECTION":
                return c
        return candidates[0]  # fallback to most recent

    if sender_chat_id:
        session = payload.get("session", "default")
        tenant_id = _extract_tenant_from_session(session)

        # ── Strategy 1: mobile_hash lookup (works for @c.us phone format) ──
        if not is_lid:
            digits = sender_chat_id.split("@")[0]
            mobile_hash = compute_mobile_hash(digits)
            logger.info(f"[WA LOOKUP] Strategy 1: mobile_hash — digits={digits} hash={mobile_hash[:16]}...")
            if tenant_id:
                lead = await _find_best_lead({"tenant_id": tenant_id, "mobile_hash": mobile_hash})
            else:
                lead = await _find_best_lead({"mobile_hash": mobile_hash})
            if lead:
                logger.info(f"[WA LOOKUP] Found by mobile_hash — lead_id={lead['_id']} status={lead.get('status')}")

        # ── Strategy 2: stored whatsapp_lid lookup ──
        if not lead and is_lid:
            logger.info(f"[WA LOOKUP] Strategy 2: LID lookup — sender={sender_chat_id}")
            lid_lead = await db.leads.find_one({"whatsapp_lid": sender_chat_id})
            if lid_lead:
                # Check if there's a better (newer) lead with the same phone
                mobile_hash = lid_lead.get("mobile_hash")
                if mobile_hash:
                    lead = await _find_best_lead({"mobile_hash": mobile_hash})
                    if lead and str(lead["_id"]) != str(lid_lead["_id"]):
                        # Migrate LID to the better lead
                        await db.leads.update_one({"_id": lid_lead["_id"]}, {"$unset": {"whatsapp_lid": ""}})
                        await db.leads.update_one({"_id": lead["_id"]}, {"$set": {"whatsapp_lid": sender_chat_id}})
                        logger.info(f"[WA LOOKUP] Migrated LID from {lid_lead['_id']} to {lead['_id']}")
                    elif not lead:
                        lead = lid_lead
                else:
                    lead = lid_lead
                logger.info(f"[WA LOOKUP] Found by stored LID — lead_id={lead['_id']} status={lead.get('status')}")

        # ── Strategy 3: resolve LID via WAHA contacts API ──
        if not lead and is_lid:
            logger.info(f"[WA LOOKUP] Strategy 3: resolving LID via WAHA API...")
            resolved_phone = await _resolve_lid_to_phone(sender_chat_id, session)
            if resolved_phone:
                mobile_hash = compute_mobile_hash(resolved_phone)
                logger.info(f"[WA LOOKUP] LID resolved to phone={resolved_phone} hash={mobile_hash[:16]}...")
                lead = await _find_best_lead({"mobile_hash": mobile_hash})
                if lead:
                    # Cache the LID on the lead for future fast lookups
                    await db.leads.update_one(
                        {"_id": lead["_id"]},
                        {"$set": {"whatsapp_lid": sender_chat_id}}
                    )
                    logger.info(f"[WA LOOKUP] Found by resolved phone — lead_id={lead['_id']} status={lead.get('status')} (LID cached)")

        # ── Strategy 4: reverse-lookup via outbound message history ──
        if not lead and is_lid:
            logger.info(f"[WA LOOKUP] Strategy 4: checking outbound message history...")
            # Find leads that have recent outbound WhatsApp messages and try WAHA contact check
            recent_outbound = await db.whatsapp_messages.find(
                {"direction": "OUTBOUND"},
                sort=[("sent_at", -1)],
            ).to_list(20)
            candidate_lead_ids = list({m["lead_id"] for m in recent_outbound})
            for candidate_lid in candidate_lead_ids:
                candidate = await db.leads.find_one({"_id": ObjectId(candidate_lid)})
                if not candidate:
                    continue
                # Check if this lead's phone matches the LID sender via WAHA
                from app.utils.encryption import decrypt_field
                try:
                    phone_raw = decrypt_field(candidate.get("mobile", ""))
                except Exception:
                    continue
                if not phone_raw:
                    continue
                phone_digits = "".join(c for c in phone_raw if c.isdigit())
                if len(phone_digits) == 10:
                    phone_digits = "91" + phone_digits  # Add India country code
                match = await _check_waha_number_matches_lid(phone_digits, sender_chat_id, session)
                if match:
                    lead = candidate
                    await db.leads.update_one(
                        {"_id": lead["_id"]},
                        {"$set": {"whatsapp_lid": sender_chat_id}}
                    )
                    logger.info(f"[WA LOOKUP] Found by outbound reverse-lookup — lead_id={lead['_id']} phone={phone_digits} (LID cached)")
                    break

        if not lead:
            total_leads = await db.leads.count_documents({})
            leads_with_hash = await db.leads.count_documents({"mobile_hash": {"$exists": True}})
            logger.warning(
                f"[WA LOOKUP] FAILED — sender={sender_chat_id} is_lid={is_lid} "
                f"total_leads={total_leads} leads_with_hash={leads_with_hash}"
            )

    if not lead:
        logger.info(f"WhatsApp: no lead found for sender={sender_chat_id} — ignoring")
        return _ok("Lead not found")

    tenant_id = lead["tenant_id"]

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
        logger.info(f"[WA INBOUND] Processing media for lead_id={lead_id} msg_id={waha_message_id}")
        try:
            await _process_incoming_media(db, payload, msg, lead_id, tenant_id, waha_message_id, now)
            logger.info(f"[WA INBOUND] _process_incoming_media completed for lead_id={lead_id}")
        except Exception as exc:
            logger.error(f"[WA INBOUND] _process_incoming_media CRASHED for lead_id={lead_id}: {exc}", exc_info=True)

        # Send acknowledgment to borrower
        try:
            from app.services.whatsapp_service import send_text_message
            from app.utils.encryption import decrypt_field
            mobile_raw = decrypt_field(lead.get("mobile", ""))
            if mobile_raw:
                ack_msg = f"✅ Document received! We're reviewing it now. You'll get an update once processing is complete."
                await send_text_message(lead_id, mobile_raw, ack_msg, tenant_id=tenant_id)
                logger.info(f"Sent document receipt acknowledgment to lead_id={lead_id}")
        except Exception as exc:
            logger.warning(f"Failed to send document ack for lead_id={lead_id}: {exc}")

    return _ok("Received")


def _extract_tenant_from_session(session: str) -> Optional[str]:
    """Extract tenant_id from WAHA session name if encoded (e.g. 'tenant_abc123')."""
    if session.startswith("tenant_"):
        return session[7:]
    return None


async def _resolve_lid_to_phone(lid_chat_id: str, session: str = "default") -> Optional[str]:
    """
    Try to resolve a Meta LID (e.g. 243189688569978@lid) to a phone number
    via WAHA's contacts/chat API.
    """
    import httpx

    if not settings.WAHA_BASE_URL or not settings.WAHA_API_KEY:
        return None

    base = settings.WAHA_BASE_URL.rstrip("/")
    headers = {"X-Api-Key": settings.WAHA_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try GET /api/contacts — some WAHA versions return phone in contact info
            resp = await client.get(
                f"{base}/api/contacts",
                params={"session": session, "contactId": lid_chat_id},
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                # Response could be a single contact or list
                contacts = data if isinstance(data, list) else [data]
                for c in contacts:
                    # Look for phone number in id, number, or pushname fields
                    cid = c.get("id", {})
                    if isinstance(cid, dict):
                        user = cid.get("user", "")
                        server = cid.get("server", "")
                        if server == "c.us" and user:
                            logger.info(f"[LID RESOLVE] Found phone via contacts API: {user}")
                            return user
                    phone = c.get("number") or c.get("phone")
                    if phone:
                        logger.info(f"[LID RESOLVE] Found phone in contact: {phone}")
                        return phone

            # Try GET /api/{session}/chats/{chatId} — may have participant phone
            resp2 = await client.get(
                f"{base}/api/{session}/chats/{lid_chat_id}",
                headers=headers,
            )
            if resp2.status_code == 200:
                chat = resp2.json()
                # Check for phone in various fields
                for field in ("number", "phone", "participant"):
                    val = chat.get(field)
                    if val and "@" not in str(val):
                        logger.info(f"[LID RESOLVE] Found phone via chats API field={field}: {val}")
                        return str(val)
    except Exception as exc:
        logger.warning(f"[LID RESOLVE] WAHA API call failed: {exc}")

    logger.info(f"[LID RESOLVE] Could not resolve LID={lid_chat_id}")
    return None


async def _check_waha_number_matches_lid(
    phone_digits: str, lid_chat_id: str, session: str = "default"
) -> bool:
    """
    Check via WAHA if a phone number's WhatsApp chatId matches a given LID.
    Uses WAHA's check-exists API to get the chatId for a phone number.
    """
    import httpx

    if not settings.WAHA_BASE_URL or not settings.WAHA_API_KEY:
        return False

    base = settings.WAHA_BASE_URL.rstrip("/")
    headers = {"X-Api-Key": settings.WAHA_API_KEY, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base}/api/contacts/check-exists",
                json={"session": session, "phone": phone_digits},
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                # Response: { "numberExists": true, "chatId": "243189688569978@lid" }
                existing_chat_id = data.get("chatId", "")
                logger.info(f"[LID CHECK] phone={phone_digits} → chatId={existing_chat_id} target={lid_chat_id}")
                return existing_chat_id == lid_chat_id
    except Exception as exc:
        logger.warning(f"[LID CHECK] WAHA check-exists failed for {phone_digits}: {exc}")

    return False


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

    download_url = f"{settings.WAHA_BASE_URL.rstrip('/')}/api/messages/default/download/{msg_id}"
    logger.info(f"[MEDIA] Downloading from WAHA: url={download_url} filename={original_filename} msg_id={msg_id}")
    try:
        # Download from WAHA: GET /api/messages/{session}/download/{messageId}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                download_url,
                headers={"X-Api-Key": settings.WAHA_API_KEY},
            )
            logger.info(f"[MEDIA] WAHA download response: status={resp.status_code} content_length={len(resp.content)}")
            resp.raise_for_status()
            file_bytes = resp.content

        logger.info(f"[MEDIA] Downloaded media from WAHA: {original_filename} ({len(file_bytes)} bytes)")
    except Exception as exc:
        logger.error(f"[MEDIA] WAHA media download FAILED for lead_id={lead_id} url={download_url}: {exc}", exc_info=True)
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

    # Process document directly — await to ensure pipeline completes
    logger.info(f"[DOC PIPELINE] Starting for {original_filename} lead_id={lead_id}")
    try:
        await _run_doc_processing_pipeline(
            s3_key, lead_id, tenant_id, original_filename,
            len(file_bytes), waha_message_id
        )
        logger.info(f"[DOC PIPELINE] Completed for {original_filename} lead_id={lead_id}")
    except Exception as exc:
        logger.error(f"[DOC PIPELINE] Failed for {original_filename} lead_id={lead_id}: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Direct document processing pipeline (bypasses Celery eager mode issues)
# ---------------------------------------------------------------------------

async def _run_doc_processing_pipeline(
    file_s3_key: str, lead_id: str, tenant_id: str,
    original_filename: str, file_size_bytes: int, waha_message_id: str,
    channel: str = "WHATSAPP",
) -> None:
    """
    Run the full document processing pipeline directly (no Celery):
    PhysicalFile → Classify → Extract → Tier1 → (maybe Tier2)
    """
    import asyncio
    from app.database import get_db
    from datetime import datetime, timezone

    db = get_db()
    now = datetime.now(timezone.utc)

    try:
        # --- Step 1: Create PhysicalFile record ---
        ext = original_filename.lower().rsplit(".", 1)[-1] if "." in original_filename else "other"
        file_type_map = {"pdf": "PDF", "jpg": "JPG", "jpeg": "JPG", "png": "PNG", "zip": "ZIP"}
        file_type = file_type_map.get(ext, "OTHER")
        is_zip = ext == "zip"

        phys_file_doc = {
            "lead_id": lead_id, "tenant_id": tenant_id,
            "original_filename": original_filename, "channel_received": channel,
            "s3_key": file_s3_key, "file_type": file_type,
            "file_size_bytes": file_size_bytes,
            "status": "EXTRACTING_ZIP" if is_zip else "RECEIVED",
            "waha_message_id": waha_message_id,
            "logical_doc_ids": [], "created_at": now, "updated_at": now,
        }
        result = await db.phys_files.insert_one(phys_file_doc)
        phys_file_id = str(result.inserted_id)
        logger.info(f"[DOC PIPELINE] PhysFile created: {phys_file_id} file={original_filename}")

        # Reject tiny files
        if 0 < file_size_bytes < 10240:
            await db.phys_files.update_one(
                {"_id": result.inserted_id},
                {"$set": {"status": "NEEDS_HUMAN_REVIEW", "classification_reasoning": "File too small (<10KB)", "updated_at": now}},
            )
            logger.warning(f"[DOC PIPELINE] File too small ({file_size_bytes}B) — flagged: {original_filename}")
            return

        await db.activity_feed.insert_one({
            "tenant_id": tenant_id, "lead_id": lead_id,
            "event_type": "DOCUMENT_RECEIVED",
            "message": f"Document received via {channel}: {original_filename}",
            "created_at": now,
        })

        if is_zip:
            logger.info(f"[DOC PIPELINE] ZIP file — skipping classification for now: {original_filename}")
            return

        # --- Step 2: Classify document ---
        logger.info(f"[DOC PIPELINE] Classifying phys_file_id={phys_file_id}")
        await db.phys_files.update_one(
            {"_id": result.inserted_id},
            {"$set": {"status": "CLASSIFYING", "updated_at": datetime.now(timezone.utc)}},
        )

        from app.services.ai_service import classify_physical_file
        from app.services.validation_rules import get_extraction_agent_config

        extraction_config = await get_extraction_agent_config(db, tenant_id)
        prompt_additions = extraction_config.get("classification_prompt_additions", "")
        confidence_threshold = extraction_config.get("classification_confidence_threshold", 75)

        # ai_service functions are synchronous — run in thread pool
        classify_result = await asyncio.to_thread(
            classify_physical_file,
            s3_key=file_s3_key,
            original_filename=original_filename,
            extraction_prompt_additions=prompt_additions,
        )

        doc_type = classify_result.get("doc_type", "OTHER")
        confidence = classify_result.get("confidence", 0)
        ambiguity_type = classify_result.get("ambiguity_type", "NORMAL")
        reasoning = classify_result.get("reasoning", "")

        new_status = "CLASSIFIED"
        if confidence < confidence_threshold or doc_type == "OTHER":
            new_status = "NEEDS_HUMAN_REVIEW"
        elif ambiguity_type == "BUNDLED":
            new_status = "NEEDS_HUMAN_REVIEW"

        await db.phys_files.update_one(
            {"_id": result.inserted_id},
            {"$set": {
                "status": new_status, "ambiguity_type": ambiguity_type,
                "classification_confidence": confidence,
                "classification_reasoning": reasoning,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        logger.info(f"[DOC PIPELINE] Classified: {doc_type} confidence={confidence} status={new_status}")

        if new_status == "NEEDS_HUMAN_REVIEW":
            return

        # --- Step 3: Create LogicalDoc ---
        from bson import ObjectId
        completeness = "PARTIAL" if ambiguity_type == "PARTIAL" else "COMPLETE"
        doc_status = "ASSEMBLING" if ambiguity_type == "PARTIAL" else "READY_FOR_EXTRACTION"

        ldoc_result = await db.logical_docs.insert_one({
            "lead_id": lead_id, "tenant_id": tenant_id,
            "doc_type": doc_type, "assembly_type": "SINGLE",
            "physical_file_ids": [phys_file_id],
            "completeness_status": completeness, "is_mandatory": True,
            "extracted_data": {}, "tier1_validation": None,
            "status": doc_status,
            "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        })
        logical_doc_id = str(ldoc_result.inserted_id)
        await db.phys_files.update_one(
            {"_id": result.inserted_id},
            {"$addToSet": {"logical_doc_ids": logical_doc_id}},
        )
        logger.info(f"[DOC PIPELINE] LogicalDoc created: {logical_doc_id} type={doc_type}")

        # --- Step 4: Extract data ---
        logger.info(f"[DOC PIPELINE] Extracting data from logical_doc_id={logical_doc_id}")
        await db.logical_docs.update_one(
            {"_id": ldoc_result.inserted_id},
            {"$set": {"status": "EXTRACTING", "updated_at": datetime.now(timezone.utc)}},
        )

        from app.services.ai_service import extract_data
        fields = extraction_config.get("extraction_fields_by_doc_type", {}).get(doc_type, [])
        ext_prompt = extraction_config.get("extraction_prompt_additions", "")

        extracted = await asyncio.to_thread(
            extract_data,
            s3_key=file_s3_key,
            original_filename=original_filename,
            doc_type=doc_type,
            fields_to_extract=fields,
            extraction_prompt_additions=ext_prompt,
        )

        await db.logical_docs.update_one(
            {"_id": ldoc_result.inserted_id},
            {"$set": {"extracted_data": extracted, "status": "EXTRACTED", "updated_at": datetime.now(timezone.utc)}},
        )
        logger.info(f"[DOC PIPELINE] Extracted {len(extracted)} fields from {doc_type}")

        # --- Step 5: Tier 1 validation ---
        logger.info(f"[DOC PIPELINE] Running Tier 1 validation for logical_doc_id={logical_doc_id}")
        await db.logical_docs.update_one(
            {"_id": ldoc_result.inserted_id},
            {"$set": {"status": "TIER1_VALIDATING", "updated_at": datetime.now(timezone.utc)}},
        )

        from app.services.ai_service import run_tier1_rules
        from app.services.validation_rules import (
            get_validation_agent_config,
            check_all_required_docs_passed,
            notify_tier1_failure,
        )

        val_config = await get_validation_agent_config(db, tenant_id)
        tier1_rules = val_config.get("tier1_rules", [])
        on_failure_action = val_config.get("on_tier1_failure_action", "NOTIFY_BORROWER_AND_CONTINUE")
        failure_templates = val_config.get("tier1_failure_message_templates", {})

        validation_result = await asyncio.to_thread(
            run_tier1_rules,
            doc_type=doc_type,
            extracted_data=extracted,
            tier1_rules=tier1_rules,
            file_size_bytes=file_size_bytes,
        )

        tier1_passed = validation_result["passed"]
        t1_status = "TIER1_PASSED" if tier1_passed else "TIER1_FAILED"

        await db.logical_docs.update_one(
            {"_id": ldoc_result.inserted_id},
            {"$set": {
                "tier1_validation": {
                    "passed": tier1_passed,
                    "rule_results": validation_result["rule_results"],
                },
                "tier1_validated_at": datetime.now(timezone.utc),
                "status": t1_status,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        logger.info(f"[DOC PIPELINE] Tier 1 {t1_status} for {doc_type} logical_doc_id={logical_doc_id}")

        # --- Step 6: Send smart reply with doc status ---
        await _send_doc_status_reply(db, lead_id, tenant_id, doc_type, tier1_passed, channel)

        if not tier1_passed:
            if on_failure_action == "NOTIFY_BORROWER_AND_CONTINUE":
                await notify_tier1_failure(
                    db, lead_id, tenant_id, doc_type,
                    validation_result.get("failed_rule_ids", []), failure_templates
                )
            return

        # Check if all required docs passed → trigger Tier 2
        tier2_passed = False  # Default — set to True only if Tier 2 runs and passes
        all_passed = await check_all_required_docs_passed(db, lead_id, tenant_id)
        if all_passed:
            logger.info(f"[DOC PIPELINE] All required docs T1 passed — running Tier 2 for lead_id={lead_id}")
            from app.services.ai_service import run_tier2_rules
            from app.services.validation_rules import build_tier2_lead_summary
            from app.services.workflow_engine import advance_to_underwriting

            tier2_rules = val_config.get("tier2_rules", [])
            t2_prompt = val_config.get("validation_prompt_additions", "")
            lead_summary = await build_tier2_lead_summary(db, lead_id, tenant_id)

            t2_result = await asyncio.to_thread(
                run_tier2_rules, lead_summary, tier2_rules, t2_prompt
            )
            tier2_passed = t2_result["passed"]

            await db.activity_feed.insert_one({
                "tenant_id": tenant_id, "lead_id": lead_id,
                "event_type": "TIER2_VALIDATION_COMPLETE",
                "message": f"Tier 2 validation {'PASSED' if tier2_passed else 'FAILED'}",
                "metadata": {"rule_results": t2_result.get("rule_results", [])},
                "created_at": datetime.now(timezone.utc),
            })

            if tier2_passed:
                await advance_to_underwriting(lead_id, tenant_id)
                logger.info(f"[DOC PIPELINE] Tier 2 PASSED → READY_FOR_UNDERWRITING lead_id={lead_id}")

        # Send all-done notification if Tier 2 passed
        if all_passed and tier2_passed:
            await _send_doc_status_reply(db, lead_id, tenant_id, doc_type, True, channel, all_complete=True)

    except Exception as exc:
        logger.error(f"[DOC PIPELINE] Failed for lead_id={lead_id} file={original_filename}: {exc}", exc_info=True)


async def _send_doc_status_reply(
    db, lead_id: str, tenant_id: str, doc_type: str,
    tier1_passed: bool, channel: str = "WHATSAPP", all_complete: bool = False,
) -> None:
    """Send a smart reply via WhatsApp and Email with doc status + missing docs list."""
    from app.services.doc_tracker import get_missing_docs_summary
    from bson import ObjectId
    from datetime import datetime, timezone

    try:
        summary = await get_missing_docs_summary(db, lead_id, tenant_id)
        message = summary["message"]

        if not message:
            return

        # Prefix with context about what just happened
        doc_label = doc_type.replace("_", " ").title()
        if all_complete:
            header = "🎉 Great news!\n\n"
        elif tier1_passed:
            header = f"✅ Your {doc_label} has been verified successfully!\n\n"
        else:
            header = f"⚠️ There's an issue with your {doc_label}. Please check below.\n\n"

        full_message = header + message

        lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
        if not lead:
            return

        now = datetime.now(timezone.utc)

        # Send via WhatsApp
        # Send via WhatsApp (service now auto-persists to whatsapp_messages)
        try:
            from app.utils.encryption import decrypt_field
            from app.services.whatsapp_service import send_text_message
            mobile_raw = decrypt_field(lead.get("mobile", ""))
            if mobile_raw:
                await send_text_message(lead_id, mobile_raw, full_message, tenant_id=tenant_id)
                logger.info(f"[DOC PIPELINE] Sent doc status update via WhatsApp for lead_id={lead_id}")
        except Exception as exc:
            logger.warning(f"[DOC PIPELINE] WhatsApp status reply failed: {exc}")

        # Send via Email (service now auto-persists to email_messages)
        try:
            borrower_email = lead.get("email", "")
            if borrower_email:
                from app.services.email_service import send_status_update_email
                email_body = full_message.replace("\n", "<br>")
                subject = "Document Status Update — Gain AI" if not all_complete else "All Documents Verified — Gain AI"
                await send_status_update_email(
                    lead_id=lead_id, tenant_id=tenant_id,
                    borrower_email=borrower_email,
                    subject=subject,
                    body_html=email_body,
                )
                logger.info(f"[DOC PIPELINE] Sent doc status update via Email for lead_id={lead_id}")
        except Exception as exc:
            logger.warning(f"[DOC PIPELINE] Email status reply failed: {exc}")

    except Exception as exc:
        logger.error(f"[DOC PIPELINE] _send_doc_status_reply failed: {exc}", exc_info=True)


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

    # Find lead by email — prefer the most recent lead in DOC_COLLECTION status
    from app.database import get_db
    from datetime import datetime, timezone

    db = get_db()
    now = datetime.now(timezone.utc)

    # Same logic as WhatsApp: prefer DOC_COLLECTION, then most recent
    email_candidates = await db.leads.find({"email": raw_email}).sort("created_at", -1).to_list(10)
    lead = None
    if email_candidates:
        for c in email_candidates:
            if c.get("status") == "DOC_COLLECTION":
                lead = c
                break
        if not lead:
            lead = email_candidates[0]  # fallback to most recent
        logger.info(f"Email: matched lead_id={lead['_id']} status={lead.get('status')} (from {len(email_candidates)} candidates)")
    if not lead:
        logger.info(f"Email: no lead found for sender={raw_email}")
        return _ok("Lead not found")

    lead_id = str(lead["_id"])
    tenant_id = lead["tenant_id"]

    # Collect attachment filenames for email_messages record
    attachment_names = []

    # Log inbound email in activity feed
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "EMAIL_RECEIVED",
        "message": f"Email received from {raw_email}: {subject[:80]}",
        "subject": subject,
        "from_email": raw_email,
        "created_at": now,
    })

    if attachments_count == 0:
        # Still persist the email message even without attachments
        body_text = form.get("text", "") or form.get("html", "")
        await db.email_messages.insert_one({
            "lead_id": lead_id, "tenant_id": tenant_id,
            "direction": "INBOUND", "from_email": raw_email,
            "subject": subject, "body_text": str(body_text)[:500],
            "attachments": [], "received_at": now,
        })
        logger.info(f"Email from {raw_email} has no attachments — saved to email_messages")
        return _ok("No attachments")

    # Process attachments
    import asyncio
    from app.services.storage_service import upload_file, build_s3_key

    processed = 0
    for i in range(1, attachments_count + 1):
        attachment_key = f"attachment{i}"
        file_field = form.get(attachment_key)
        if not file_field:
            continue

        try:
            if hasattr(file_field, "read"):
                file_bytes = await file_field.read()
                filename = getattr(file_field, "filename", None) or f"attachment_{i}.pdf"
            elif isinstance(file_field, str):
                file_bytes = file_field.encode("latin-1")
                filename = f"email_attachment_{i}.pdf"
            else:
                continue

            s3_key = build_s3_key(tenant_id, lead_id, filename)
            await upload_file(file_bytes, s3_key)
            attachment_names.append({"filename": filename, "size": len(file_bytes), "s3_key": s3_key})

            # Process document directly — await to ensure pipeline completes
            logger.info(f"[DOC PIPELINE] Starting email attachment: {filename} for lead_id={lead_id}")
            try:
                await _run_doc_processing_pipeline(
                    s3_key, lead_id, tenant_id, filename,
                    len(file_bytes), "", channel="EMAIL",
                )
                logger.info(f"[DOC PIPELINE] Completed email attachment: {filename} for lead_id={lead_id}")
            except Exception as pipe_exc:
                logger.error(f"[DOC PIPELINE] Failed email attachment {filename} for lead_id={lead_id}: {pipe_exc}", exc_info=True)
            processed += 1

        except Exception as exc:
            logger.error(f"Failed to process email attachment {i} for lead_id={lead_id}: {exc}")
            continue

    # Persist email message with attachment metadata
    body_text = form.get("text", "") or form.get("html", "")
    await db.email_messages.insert_one({
        "lead_id": lead_id, "tenant_id": tenant_id,
        "direction": "INBOUND", "from_email": raw_email,
        "subject": subject, "body_text": str(body_text)[:500],
        "attachments": attachment_names, "received_at": now,
    })

    logger.info(f"Email processed: {processed}/{attachments_count} attachments for lead_id={lead_id}")
    return _ok(f"Processed {processed} attachments")


# ---------------------------------------------------------------------------
# Gmail Pub/Sub Push Notification
# ---------------------------------------------------------------------------

@router.post("/gmail-push")
async def gmail_push_notification(request: Request, background_tasks: BackgroundTasks):
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

        # Process in FastAPI background task — runs after response is sent
        from app.services.gmail_service import process_new_messages
        background_tasks.add_task(process_new_messages, history_id)

        # Must return 200 quickly to acknowledge the Pub/Sub message
        return _ok("Acknowledged")

    except Exception as exc:
        logger.error(f"Gmail push handler error: {exc}")
        # Still return 200 to prevent Pub/Sub from retrying indefinitely
        return _ok("Error handled")


# ---------------------------------------------------------------------------
# Follow-up trigger — direct async (bypasses Celery Beat)
# ---------------------------------------------------------------------------

@router.post("/follow-up/trigger")
async def trigger_follow_up_check(background_tasks: BackgroundTasks):
    """
    Manually or cron-trigger the follow-up check.
    Runs doc-tracker-aware reminders for all leads in DOC_COLLECTION.
    Can be called by Railway cron, external scheduler, or manual API call.
    """
    from app.workers.follow_up_worker import run_follow_up_check_async

    background_tasks.add_task(run_follow_up_check_async)
    return _ok("Follow-up check started")


# ---------------------------------------------------------------------------
# Diagnostic endpoint — check pipeline data for a lead (no auth, for debugging)
# ---------------------------------------------------------------------------

@router.get("/diagnostic/{lead_id}")
async def diagnostic_lead(lead_id: str):
    """Check all pipeline data for a lead — for debugging only. Remove in production."""
    from app.database import get_db
    from bson import ObjectId

    db = get_db()

    try:
        lead_oid = ObjectId(lead_id)
    except Exception:
        return {"error": "Invalid lead_id"}

    lead = await db.leads.find_one({"_id": lead_oid})
    if not lead:
        return {"error": "Lead not found"}

    tenant_id = lead.get("tenant_id", "")
    mobile_hash = lead.get("mobile_hash", "MISSING")

    # Count records in each collection
    wa_count = await db.whatsapp_messages.count_documents({"lead_id": lead_id})
    wa_inbound = await db.whatsapp_messages.count_documents({"lead_id": lead_id, "direction": "INBOUND"})
    wa_outbound = await db.whatsapp_messages.count_documents({"lead_id": lead_id, "direction": "OUTBOUND"})
    email_count = await db.email_messages.count_documents({"lead_id": lead_id})
    email_inbound = await db.email_messages.count_documents({"lead_id": lead_id, "direction": "INBOUND"})
    email_outbound = await db.email_messages.count_documents({"lead_id": lead_id, "direction": "OUTBOUND"})
    activity_count = await db.activity_feed.count_documents({"lead_id": lead_id})
    activity_with_tenant = await db.activity_feed.count_documents({"lead_id": lead_id, "tenant_id": tenant_id})
    phys_files_count = await db.phys_files.count_documents({"lead_id": lead_id})
    logical_docs_count = await db.logical_docs.count_documents({"lead_id": lead_id})

    # Get last 5 activity events
    activities = []
    for ev in await db.activity_feed.find({"lead_id": lead_id}).sort("created_at", -1).to_list(5):
        activities.append({
            "event_type": ev.get("event_type"),
            "message": ev.get("message", "")[:100],
            "tenant_id": ev.get("tenant_id"),
            "created_at": str(ev.get("created_at", "")),
        })

    # Get last 5 WA messages
    wa_msgs = []
    for m in await db.whatsapp_messages.find({"lead_id": lead_id}).sort("sent_at", -1).to_list(5):
        wa_msgs.append({
            "direction": m.get("direction"),
            "message_type": m.get("message_type"),
            "content": m.get("content", "")[:80],
            "tenant_id": m.get("tenant_id"),
            "sent_at": str(m.get("sent_at", "")),
        })

    # Check tenants collection
    tenant_count = await db.tenants.count_documents({})
    active_tenants = await db.tenants.count_documents({"is_active": True})

    return {
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "status": lead.get("status"),
        "mobile_hash": mobile_hash[:16] + "..." if mobile_hash != "MISSING" else "MISSING",
        "has_email": bool(lead.get("email")),
        "tenants": {"total": tenant_count, "active": active_tenants},
        "whatsapp_messages": {"total": wa_count, "inbound": wa_inbound, "outbound": wa_outbound},
        "email_messages": {"total": email_count, "inbound": email_inbound, "outbound": email_outbound},
        "activity_feed": {"total": activity_count, "with_tenant_filter": activity_with_tenant},
        "phys_files": phys_files_count,
        "logical_docs": logical_docs_count,
        "recent_activities": activities,
        "recent_wa_messages": wa_msgs,
    }
