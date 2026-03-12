"""
WhatsApp Worker — Celery tasks for outbound WhatsApp messages and inbound document processing.

Tasks:
  send_doc_checklist_whatsapp  — send initial doc checklist after lead is QUALIFIED
  send_doc_checklist_email     — send initial doc checklist via SendGrid
  process_whatsapp_document    — create PhysicalFile record and enqueue AI classification
"""
import asyncio
import logging
from datetime import datetime, timezone

from bson import ObjectId

from app.celery_app import celery_app

logger = logging.getLogger("gain.workers.whatsapp")


# ---------------------------------------------------------------------------
# Send WhatsApp doc checklist (triggered by workflow engine after QUALIFIED)
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.send_doc_checklist_whatsapp", queue="whatsapp", bind=True, max_retries=3)
def send_doc_checklist_whatsapp(self, lead_id: str, tenant_id: str) -> dict:
    """
    Send the initial document checklist to the borrower via WhatsApp.

    Steps:
      1. Fetch lead + DOC_COLLECTION agent config from DB
      2. Decrypt mobile; compute + store mobile_hash on lead
      3. Build checklist message from agent config template + entity_type doc list
      4. Send via WAHA
      5. Save to whatsapp_messages collection
    """
    logger.info(f"[WA WORKER] Sending checklist — lead_id={lead_id}")

    try:
        result = asyncio.run(_send_checklist_async(lead_id, tenant_id))
        return result
    except Exception as exc:
        logger.error(f"[WA WORKER] send_doc_checklist_whatsapp failed for lead_id={lead_id}: {exc}")
        raise self.retry(exc=exc, countdown=60 * 5)  # retry after 5 min


async def _send_checklist_async(lead_id: str, tenant_id: str) -> dict:
    from app.database import get_db
    from app.utils.encryption import decrypt_field
    from app.services.whatsapp_service import (
        send_text_message,
        compute_mobile_hash,
        _whatsapp_number,
    )
    from app.services.validation_rules import get_doc_collection_config

    db = get_db()

    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
    if not lead:
        logger.error(f"[WA WORKER] Lead not found: {lead_id}")
        return {"status": "error", "reason": "lead_not_found"}

    mobile_encrypted = lead.get("mobile", "")
    mobile_raw = decrypt_field(mobile_encrypted) if mobile_encrypted else ""
    if not mobile_raw:
        logger.warning(f"[WA WORKER] No mobile for lead_id={lead_id}")
        return {"status": "skipped", "reason": "no_mobile"}

    # Compute + persist mobile_hash (enables inbound WA matching)
    mobile_hash = compute_mobile_hash(mobile_raw)
    now = datetime.now(timezone.utc)
    await db.leads.update_one(
        {"_id": ObjectId(lead_id), "tenant_id": tenant_id},
        {"$set": {"mobile_hash": mobile_hash, "updated_at": now}},
    )

    # Get DOC_COLLECTION agent config
    config = await get_doc_collection_config(db, tenant_id)
    entity_type = lead.get("entity_type", "INDIVIDUAL")
    entity_checklist = config.get("doc_checklist_by_entity_type", {}).get(
        entity_type, config.get("doc_checklist_by_entity_type", {}).get("INDIVIDUAL", {})
    )
    required_docs = entity_checklist.get("required", [])
    optional_docs = entity_checklist.get("optional", [])
    all_docs = required_docs + (optional_docs or [])

    # Build doc list string
    doc_list_str = "\n".join(f"  {i+1}. {doc}" for i, doc in enumerate(all_docs))
    borrower_name = lead.get("name", "there")
    company_name = lead.get("company_name", "")

    template = config.get(
        "whatsapp_checklist_template",
        "Hello {{borrower_name}} ji! Please send: {{doc_list}}",
    )
    message = (
        template
        .replace("{{borrower_name}}", borrower_name)
        .replace("{{company_name}}", company_name)
        .replace("{{doc_list}}", doc_list_str)
        .replace("{{loan_type}}", lead.get("loan_type", "").replace("_", " ").title())
    )

    # Send via WAHA
    sent = await send_text_message(lead_id, mobile_raw, message)

    # Save to whatsapp_messages
    await db.whatsapp_messages.insert_one({
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "direction": "OUTBOUND",
        "message_type": "TEXT",
        "content": message,
        "status": "SENT" if sent else "FAILED",
        "template_name": "DOC_CHECKLIST_INITIAL",
        "sent_at": now,
    })

    # Activity feed
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "WHATSAPP_CHECKLIST_SENT",
        "message": f"Document checklist sent to {borrower_name} via WhatsApp",
        "created_at": now,
    })

    status = "sent" if sent else "failed_waha_not_configured"
    logger.info(f"[WA WORKER] Checklist {status} for lead_id={lead_id}")
    return {"status": status, "lead_id": lead_id}


# ---------------------------------------------------------------------------
# Send Email doc checklist
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.send_doc_checklist_email", queue="email", bind=True, max_retries=3)
def send_doc_checklist_email(self, lead_id: str, tenant_id: str) -> dict:
    """Send the initial document checklist via SendGrid email."""
    logger.info(f"[EMAIL WORKER] Sending checklist — lead_id={lead_id}")
    try:
        result = asyncio.run(_send_email_checklist_async(lead_id, tenant_id))
        return result
    except Exception as exc:
        logger.error(f"[EMAIL WORKER] send_doc_checklist_email failed for lead_id={lead_id}: {exc}")
        raise self.retry(exc=exc, countdown=60 * 5)


async def _send_email_checklist_async(lead_id: str, tenant_id: str) -> dict:
    from app.database import get_db
    from app.services.email_service import send_document_checklist_email, log_outbound_email
    from app.services.validation_rules import get_doc_collection_config

    db = get_db()
    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
    if not lead:
        return {"status": "error", "reason": "lead_not_found"}

    borrower_email = lead.get("email", "")
    if not borrower_email:
        return {"status": "skipped", "reason": "no_email"}

    config = await get_doc_collection_config(db, tenant_id)
    entity_type = lead.get("entity_type", "INDIVIDUAL")
    entity_checklist = config.get("doc_checklist_by_entity_type", {}).get(
        entity_type, config.get("doc_checklist_by_entity_type", {}).get("INDIVIDUAL", {})
    )
    required_docs = entity_checklist.get("required", [])
    optional_docs = [f"{d} (optional)" for d in entity_checklist.get("optional", [])]
    all_docs = required_docs + optional_docs

    borrower_name = lead.get("name", "")
    company_name = lead.get("company_name", "")
    loan_type = lead.get("loan_type", "")

    subject_template = config.get(
        "email_subject_template",
        "Documents Required — {{loan_type}} Application for {{company_name}}",
    )
    body_template = config.get(
        "email_body_template",
        "Dear {{borrower_name}},\n\nPlease send the following documents:\n\n{{doc_list}}\n\nRegards,\nGain AI",
    )

    sent = await send_document_checklist_email(
        lead_id=lead_id,
        tenant_id=tenant_id,
        borrower_name=borrower_name,
        borrower_email=borrower_email,
        company_name=company_name,
        loan_type=loan_type,
        doc_list=all_docs,
        subject_template=subject_template,
        body_template=body_template,
    )

    subject = subject_template.replace("{{company_name}}", company_name).replace("{{loan_type}}", loan_type)
    await log_outbound_email(db, lead_id, tenant_id, borrower_email, subject)

    return {"status": "sent" if sent else "failed", "lead_id": lead_id}


# ---------------------------------------------------------------------------
# Process incoming WhatsApp document
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.process_whatsapp_document", queue="whatsapp", bind=True, max_retries=3)
def process_whatsapp_document(
    self,
    file_s3_key: str,
    lead_id: str,
    tenant_id: str,
    original_filename: str,
    file_size_bytes: int = 0,
    waha_message_id: str = "",
) -> dict:
    """
    Create a PhysicalFile record for a document received via WhatsApp,
    then enqueue it for AI classification (or ZIP extraction if applicable).
    """
    logger.info(
        f"[WA WORKER] Processing document — lead_id={lead_id} "
        f"file={original_filename} size={file_size_bytes}"
    )
    try:
        result = asyncio.run(
            _process_doc_async(
                file_s3_key, lead_id, tenant_id, original_filename,
                file_size_bytes, waha_message_id
            )
        )
        return result
    except Exception as exc:
        logger.error(f"[WA WORKER] process_whatsapp_document failed: {exc}")
        raise self.retry(exc=exc, countdown=30)


async def _process_doc_async(
    file_s3_key: str,
    lead_id: str,
    tenant_id: str,
    original_filename: str,
    file_size_bytes: int,
    waha_message_id: str,
) -> dict:
    from app.database import get_db
    from app.services.validation_rules import get_extraction_agent_config

    db = get_db()
    now = datetime.now(timezone.utc)
    is_zip = original_filename.lower().endswith(".zip")

    # Detect file type
    ext = original_filename.lower().rsplit(".", 1)[-1] if "." in original_filename else "other"
    file_type_map = {"pdf": "PDF", "jpg": "JPG", "jpeg": "JPG", "png": "PNG", "zip": "ZIP"}
    file_type = file_type_map.get(ext, "OTHER")

    # Create PhysicalFile record
    phys_file_doc = {
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "original_filename": original_filename,
        "channel_received": "WHATSAPP",
        "s3_key": file_s3_key,
        "file_type": file_type,
        "file_size_bytes": file_size_bytes,
        "status": "EXTRACTING_ZIP" if is_zip else "RECEIVED",
        "waha_message_id": waha_message_id,
        "logical_doc_ids": [],
        "created_at": now,
        "updated_at": now,
    }
    result = await db.phys_files.insert_one(phys_file_doc)
    phys_file_id = str(result.inserted_id)

    # T1-007: reject tiny files immediately (< 10KB)
    if file_size_bytes > 0 and file_size_bytes < 10240:
        await db.phys_files.update_one(
            {"_id": result.inserted_id},
            {"$set": {"status": "NEEDS_HUMAN_REVIEW", "classification_reasoning": "File too small (<10KB)", "updated_at": now}},
        )
        logger.warning(f"[WA WORKER] File too small ({file_size_bytes}B) — flagged for review: {original_filename}")
        return {"status": "flagged_too_small", "phys_file_id": phys_file_id}

    # Activity feed
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "DOCUMENT_RECEIVED",
        "message": f"Document received via WhatsApp: {original_filename}",
        "created_at": now,
    })

    # Enqueue appropriate task
    if is_zip:
        from app.workers.ai_worker import extract_zip
        extract_zip.apply_async(
            kwargs={"phys_file_id": phys_file_id, "tenant_id": tenant_id},
            queue="zip",
        )
        logger.info(f"[WA WORKER] ZIP queued for extraction: phys_file_id={phys_file_id}")
        return {"status": "zip_queued", "phys_file_id": phys_file_id}
    else:
        from app.workers.ai_worker import classify_document
        classify_document.apply_async(
            kwargs={"phys_file_id": phys_file_id, "tenant_id": tenant_id},
            queue="ai-classification",
        )
        logger.info(f"[WA WORKER] Classification queued: phys_file_id={phys_file_id}")
        return {"status": "classification_queued", "phys_file_id": phys_file_id}
