"""
Voice Service — ElevenLabs Conversational AI outbound call management.

Responsibilities:
  - enqueue_qualification_call: dispatch Celery voice task from FastAPI routes
  - trigger_outbound_call:       async call to ElevenLabs /outbound-call API
  - should_retry_call:           decide whether a failed call should be retried
  - process_call_completed:      ElevenLabs post-call webhook -> extract data -> update lead
  - process_call_status_update:  real-time status updates from ElevenLabs
  - _extract_from_transcript:    Claude-powered extraction of key loan data
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.database import get_db
from app.utils.logging import get_logger

logger = get_logger("services.voice")

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"


# ---------------------------------------------------------------------------
# Enqueue (FastAPI-facing) -- called from leads route
# ---------------------------------------------------------------------------

async def enqueue_qualification_call(lead_id: str, tenant_id: str) -> None:
    """
    Enqueue a Celery voice-call task for a lead.
    Called from POST /leads/{lead_id}/trigger-agent.
    """
    from app.workers.voice_worker import place_voice_call
    place_voice_call.apply_async(
        kwargs={"lead_id": lead_id, "tenant_id": tenant_id},
        queue="voice",
    )
    logger.info(f"[VOICE] Enqueued qualification call -- lead_id={lead_id}")

    # Mark lead status as CALL_SCHEDULED
    db = get_db()
    now = datetime.now(timezone.utc)
    from bson import ObjectId
    await db.leads.update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"status": "CALL_SCHEDULED", "updated_at": now}},
    )
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "CALL_SCHEDULED",
        "message": "Qualification call scheduled via ElevenLabs",
        "created_at": now,
    })


# ---------------------------------------------------------------------------
# Outbound call placement -- called from Celery worker (asyncio.run)
# ---------------------------------------------------------------------------

async def trigger_outbound_call(lead_id: str, tenant_id: str) -> Optional[str]:
    """
    Place an outbound ElevenLabs Conversational AI call for the given lead.
    Returns the ElevenLabs conversation_id if successful, None otherwise.
    """
    if not settings.ELEVENLABS_API_KEY or not settings.ELEVENLABS_AGENT_ID:
        logger.warning("[VOICE] ElevenLabs not configured -- skipping outbound call")
        return None

    db = get_db()
    from bson import ObjectId
    lead = await db.leads.find_one({"_id": ObjectId(lead_id)})
    if not lead:
        logger.error(f"[VOICE] Lead not found -- lead_id={lead_id}")
        return None

    mobile_raw = await _get_decrypted_mobile(lead)
    if not mobile_raw:
        logger.error(f"[VOICE] No mobile number for lead -- lead_id={lead_id}")
        return None

    to_number = _format_e164(mobile_raw)
    borrower_name = lead.get("name", "Business Owner")
    company_name = lead.get("company_name", "your business")
    loan_amount = lead.get("loan_amount_requested", 0)
    loan_amount_str = f"Rs.{loan_amount // 100:,}" if loan_amount else "the requested amount"
    loan_type = lead.get("loan_type", "Business Loan")

    payload = {
        "agent_id": settings.ELEVENLABS_AGENT_ID,
        "agent_phone_number_id": settings.ELEVENLABS_PHONE_NUMBER_ID,
        "to_number": to_number,
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "borrower_name": borrower_name,
                "company_name": company_name,
                "loan_type": loan_type,
                "loan_amount": loan_amount_str,
                "lead_id": lead_id,
                "tenant_id": tenant_id,
            },
        },
        "metadata": {
            "lead_id": lead_id,
            "tenant_id": tenant_id,
        },
    }

    logger.info(
        f"[VOICE] Outbound call payload -- to={to_number} "
        f"borrower={borrower_name} loan_type={loan_type} amount={loan_amount_str}"
    )

    headers = {
        "xi-api-key": settings.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{ELEVENLABS_BASE_URL}/convai/twilio/outbound-call",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        conversation_id = data.get("conversation_id") or data.get("conversationId")
        if not conversation_id:
            logger.error(f"[VOICE] ElevenLabs response missing conversation_id: {data}")
            return None

        logger.info(f"[VOICE] Call placed -- conversation_id={conversation_id}")
        now = datetime.now(timezone.utc)

        call_record = {
            "lead_id": lead_id,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "to_number": to_number,
            "elevenlabs_agent_id": settings.ELEVENLABS_AGENT_ID,
            "elevenlabs_call_result": "pending",
            "status": "INITIATED",
            "initiated_at": now,
            "transcript": [],
            "transcript_raw": "",
            "extracted_data": {},
            "qualification_outcome": None,
            "duration_seconds": 0,
        }
        await db.call_records.insert_one(call_record)

        await db.leads.update_one(
            {"_id": ObjectId(lead_id)},
            {"$set": {"status": "CALL_SCHEDULED", "updated_at": now}},
        )
        await db.activity_feed.insert_one({
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "event_type": "CALL_INITIATED",
            "message": f"Outbound qualification call initiated (conv: {conversation_id})",
            "created_at": now,
        })

        return conversation_id

    except httpx.HTTPStatusError as exc:
        logger.error(
            f"[VOICE] ElevenLabs API error: {exc.response.status_code} -- {exc.response.text}"
        )
        raise
    except Exception as exc:
        logger.error(f"[VOICE] trigger_outbound_call failed: {exc}")
        raise


def should_retry_call(lead_id: str, attempt_number: int) -> bool:
    """Retry up to 2 additional times (3 total)."""
    return attempt_number < 3


# ---------------------------------------------------------------------------
# Webhook handlers -- called from /webhooks/elevenlabs routes
# ---------------------------------------------------------------------------

async def process_call_completed(payload: dict) -> None:
    """
    Handle ElevenLabs post-call webhook.
    Parses transcript, extracts qualification data via Claude, updates lead status,
    and triggers doc checklist if QUALIFIED.
    """
    db = get_db()
    now = datetime.now(timezone.utc)

    conversation_id = (
        payload.get("conversation_id")
        or payload.get("data", {}).get("conversation_id")
    )
    data = payload.get("data", payload)

    if not conversation_id:
        logger.warning("[VOICE] process_call_completed: no conversation_id in payload")
        return

    # Log full payload structure for debugging
    logger.info(
        f"[VOICE] Processing completed call -- conversation_id={conversation_id} "
        f"payload_keys={list(payload.keys())} data_keys={list(data.keys() if isinstance(data, dict) else [])}"
    )

    call_record = await db.call_records.find_one({"conversation_id": conversation_id})
    metadata = data.get("metadata", {}) or {}
    lead_id = metadata.get("lead_id") or (call_record and call_record.get("lead_id"))
    tenant_id = metadata.get("tenant_id") or (call_record and call_record.get("tenant_id"))

    if not lead_id:
        logger.error(f"[VOICE] Cannot find lead_id for conversation_id={conversation_id}")
        return

    raw_transcript = data.get("transcript", [])
    duration_seconds = int(data.get("duration", 0) or 0)
    elevenlabs_status = data.get("status", "completed")

    # Normalise ElevenLabs status — they use both "completed" and "done"
    COMPLETED_STATUSES = {"completed", "done", "success"}

    logger.info(
        f"[VOICE] Call details -- lead_id={lead_id} elevenlabs_status={elevenlabs_status!r} "
        f"duration_seconds={duration_seconds} transcript_entries={len(raw_transcript)}"
    )

    # Normalize transcript
    normalized_transcript = []
    transcript_text_lines = []
    for entry in raw_transcript:
        role = entry.get("role", "unknown")
        message = entry.get("message", "")
        time_in_call = entry.get("time_in_call_secs", 0)
        mm = time_in_call // 60
        ss = time_in_call % 60
        ts = f"{mm:02d}:{ss:02d}"
        normalized_transcript.append({"role": role, "message": message, "timestamp": ts})
        transcript_text_lines.append(f"{role.upper()}: {message}")

    transcript_raw = "\n".join(transcript_text_lines)

    # Extract data via Claude
    extracted_data = {}
    qualification_outcome = "INCOMPLETE"

    if transcript_raw:
        try:
            extracted_data, qualification_outcome = await _extract_from_transcript(transcript_raw)
        except Exception as exc:
            logger.error(f"[VOICE] Transcript extraction failed: {exc}")

    # Supplement with ElevenLabs analysis if Claude returned nothing
    analysis = data.get("analysis", {}) or {}
    el_data_collection = analysis.get("data_collection_results", {}) or {}
    if el_data_collection and not extracted_data:
        extracted_data = {
            k: v.get("value") for k, v in el_data_collection.items() if isinstance(v, dict)
        }

    if elevenlabs_status in ("no_answer", "failed", "busy", "error"):
        qualification_outcome = "INCOMPLETE"

    if elevenlabs_status in COMPLETED_STATUSES and qualification_outcome == "QUALIFIED":
        new_lead_status = "QUALIFIED"
    elif qualification_outcome in ("NOT_QUALIFIED", "REJECTED"):
        new_lead_status = "NOT_QUALIFIED"
    else:
        new_lead_status = "INCOMPLETE"

    ai_summary = extracted_data.get("callSummary") or extracted_data.get("call_summary") or ""

    call_update = {
        "elevenlabs_call_result": elevenlabs_status,
        "status": "COMPLETED" if elevenlabs_status in COMPLETED_STATUSES else "FAILED",
        "transcript": normalized_transcript,
        "transcript_raw": transcript_raw,
        "extracted_data": extracted_data,
        "qualification_outcome": qualification_outcome,
        "duration_seconds": duration_seconds,
        "completed_at": now,
        "ai_summary": ai_summary,
    }

    if call_record:
        await db.call_records.update_one(
            {"conversation_id": conversation_id},
            {"$set": call_update},
        )
    else:
        await db.call_records.insert_one({
            "lead_id": lead_id,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "initiated_at": now,
            **call_update,
        })

    from bson import ObjectId
    await db.leads.update_one(
        {"_id": ObjectId(lead_id)},
        {
            "$set": {
                "status": new_lead_status,
                "updated_at": now,
                "qualification_result": {
                    "outcome": qualification_outcome,
                    "call_transcript": transcript_raw,
                    "key_data": extracted_data,
                },
            }
        },
    )

    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "CALL_COMPLETED",
        "message": (
            f"Qualification call completed -- outcome: {qualification_outcome} "
            f"(duration: {duration_seconds}s)"
        ),
        "created_at": now,
    })

    logger.info(
        f"[VOICE] Call processed -- lead_id={lead_id} "
        f"outcome={qualification_outcome} status={new_lead_status}"
    )

    # Trigger next workflow step (doc collection if QUALIFIED)
    if new_lead_status in ("QUALIFIED", "NOT_QUALIFIED"):
        logger.info(f"[VOICE] Call processed — invoking workflow engine for lead_id={lead_id} status={new_lead_status}")
        try:
            from app.services.workflow_engine import process_lead
            await process_lead(lead_id, tenant_id)
        except Exception as exc:
            logger.error(f"[VOICE] Workflow engine failed for lead_id={lead_id}: {exc}")


async def process_call_status_update(payload: dict) -> None:
    """Handle real-time status updates from ElevenLabs."""
    db = get_db()
    now = datetime.now(timezone.utc)

    conversation_id = (
        payload.get("conversation_id")
        or payload.get("data", {}).get("conversation_id")
    )
    status = payload.get("status") or payload.get("data", {}).get("status", "")

    if not conversation_id:
        return

    logger.info(
        f"[VOICE] Call status update -- conversation_id={conversation_id} status={status}"
    )

    await db.call_records.update_one(
        {"conversation_id": conversation_id},
        {"$set": {"status": status.upper() if status else "UNKNOWN", "updated_at": now}},
    )


# ---------------------------------------------------------------------------
# Claude-powered transcript extraction
# ---------------------------------------------------------------------------

async def _extract_from_transcript(transcript_raw: str) -> tuple:
    """
    Use Claude to extract key loan qualification data from transcript text.
    Returns (extracted_data: dict, outcome: str)
    outcome: QUALIFIED | NOT_QUALIFIED | INCOMPLETE
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("[VOICE] ANTHROPIC_API_KEY not set -- skipping extraction")
        return {}, "INCOMPLETE"

    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    prompt = f"""You are analyzing a loan qualification call transcript for an Indian SME lending company.

Extract the following information and return a JSON object:
- declaredTurnover: annual business turnover mentioned by borrower (string, e.g. "Rs.4 Cr")
- businessVintage: how long the business has been operating (string, e.g. "6 years")
- existingEmis: existing loan EMIs per month (string, e.g. "Rs.50,000/mo" or "None")
- consentGiven: did borrower explicitly consent to credit bureau check (boolean)
- loanPurpose: purpose of the loan if mentioned (string or null)
- callSummary: 1-2 sentence summary of the call outcome and borrower intent

Also determine the qualification outcome:
- QUALIFIED: borrower answered all key questions and gave consent
- NOT_QUALIFIED: borrower explicitly declined or does not meet stated criteria
- INCOMPLETE: call was cut short, borrower did not answer key questions, or data is insufficient

Return ONLY valid JSON in this format:
{{
  "declaredTurnover": "...",
  "businessVintage": "...",
  "existingEmis": "...",
  "consentGiven": true,
  "loanPurpose": "...",
  "callSummary": "...",
  "outcome": "QUALIFIED"
}}

TRANSCRIPT:
{transcript_raw}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()

        # Strip markdown code blocks if present
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

        result = json.loads(response_text)
        outcome = result.pop("outcome", "INCOMPLETE")
        if outcome not in ("QUALIFIED", "NOT_QUALIFIED", "INCOMPLETE"):
            outcome = "INCOMPLETE"

        return result, outcome

    except json.JSONDecodeError as exc:
        logger.error(f"[VOICE] Claude returned non-JSON: {exc}")
        return {}, "INCOMPLETE"
    except Exception as exc:
        logger.error(f"[VOICE] Claude extraction error: {exc}")
        return {}, "INCOMPLETE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_decrypted_mobile(lead: dict) -> Optional[str]:
    """Decrypt and return the raw mobile number for an outbound call."""
    try:
        from app.utils.encryption import decrypt_field
        encrypted_mobile = lead.get("mobile_encrypted") or lead.get("mobile")
        if not encrypted_mobile:
            return None
        if "*" in str(encrypted_mobile):
            return None
        return decrypt_field(encrypted_mobile)
    except Exception as exc:
        logger.error(f"[VOICE] Mobile decryption failed: {exc}")
        raw = lead.get("mobile_raw") or lead.get("mobile")
        if raw and "*" not in str(raw):
            return raw
        return None


def _format_e164(mobile: str) -> str:
    """Format an Indian mobile number to E.164 (+91XXXXXXXXXX)."""
    digits = "".join(c for c in mobile if c.isdigit())
    # Already in full international format: +91XXXXXXXXXX (12 digits)
    if digits.startswith("91") and len(digits) == 12:
        return f"+{digits}"
    # Standard 10-digit Indian mobile (no country code)
    if len(digits) == 10:
        return f"+91{digits}"
    # 11-digit with leading 0 (e.g. 09876543210 → +919876543210)
    if digits.startswith("0") and len(digits) == 11:
        return f"+91{digits[1:]}"
    # Fallback — return as-is with + prefix
    logger.warning(f"[VOICE] Unexpected mobile number format: {digits!r} (len={len(digits)})")
    return f"+{digits}"


def _trigger_doc_collection(lead_id: str, tenant_id: str) -> None:
    """Enqueue WhatsApp doc checklist after successful qualification."""
    try:
        from app.workers.whatsapp_worker import send_doc_checklist_whatsapp
        send_doc_checklist_whatsapp.apply_async(
            kwargs={"lead_id": lead_id, "tenant_id": tenant_id},
            queue="whatsapp",
        )
        logger.info(f"[VOICE] Doc checklist enqueued for lead_id={lead_id}")
    except Exception as exc:
        logger.error(f"[VOICE] Failed to enqueue doc checklist: {exc}")
