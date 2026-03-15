"""
Calls and WhatsApp messages routes — PRD Section 6.2.

GET /leads/:id/calls             — all call records, newest first
GET /leads/:id/calls/:call_id    — one call with full transcript + extracted_fields
GET /leads/:id/calls/:call_id/audio — proxy ElevenLabs recording audio
GET /leads/:id/whatsapp-messages — full WhatsApp thread, oldest first (for chat window)

All queries are scoped by tenant_id from JWT. Every record must belong to the
requesting tenant before it is returned.
"""
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from typing import Optional

from app.auth import get_current_user, CurrentUser
from app.database import get_db
from app.utils.logging import get_logger

router = APIRouter(tags=["Calls & Messages"])
logger = get_logger("routes.calls")


def _success(data=None, message="Success"):
    return {"success": True, "data": data, "message": message}


def _serialize_call(doc: dict, include_transcript: bool = True) -> dict:
    """Convert a calls collection document to a serializable dict."""
    result = {**doc}
    result["id"] = str(doc["_id"])
    result.pop("_id", None)
    if result.get("tenant_id"):
        result["tenant_id"] = str(result["tenant_id"])

    # Serialize transcript timestamps
    if not include_transcript:
        result.pop("transcript", None)
        result.pop("transcript_raw", None)

    # Datetime fields → ISO strings
    for dt_field in ("initiated_at", "completed_at", "created_at", "updated_at"):
        if dt_field in result and result[dt_field] is not None:
            result[dt_field] = _isoformat(result[dt_field])

    # Generate recording_url from conversation_id if ElevenLabs call
    conv_id = result.get("conversation_id") or result.get("elevenlabs_conversation_id")
    lead_id = result.get("lead_id", "")
    call_id = result.get("id", "")
    if conv_id and not result.get("recording_url"):
        result["recording_url"] = f"/api/v1/leads/{lead_id}/calls/{call_id}/audio"

    return result


def _isoformat(val) -> str:
    """Safely convert datetime/any to ISO string with Z suffix for UTC."""
    if hasattr(val, "isoformat"):
        s = val.isoformat()
        # Motor strips tzinfo from UTC datetimes; append Z so browsers parse as UTC
        if not s.endswith("Z") and "+" not in s and s[-1].isdigit():
            s += "Z"
        return s
    return str(val) if val else ""


def _serialize_message(doc: dict) -> dict:
    """Convert a whatsapp_messages collection document to a JSON-serializable dict."""
    result = {**doc}
    result["id"] = str(doc["_id"])
    result.pop("_id", None)
    if result.get("tenant_id"):
        result["tenant_id"] = str(result["tenant_id"])
    if result.get("physical_file_id"):
        result["physical_file_id"] = str(result["physical_file_id"])
    # Datetime fields → ISO strings (json.dumps can't handle raw datetime)
    for dt_field in ("sent_at", "created_at", "updated_at", "received_at"):
        if dt_field in result and result[dt_field] is not None:
            result[dt_field] = _isoformat(result[dt_field])
    return result


# ---------------------------------------------------------------------------
# GET /leads/:id/calls  — all call attempts for a lead
# ---------------------------------------------------------------------------

@router.get("/leads/{lead_id}/calls")
async def list_calls(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Returns all call records for a lead, sorted newest first.

    Each item includes: status, duration_seconds, qualification_outcome,
    attempt_number, initiated_at, completed_at, ai_summary.
    Transcript content is excluded from the list — use the detail endpoint.
    """
    db = get_db()

    # Verify lead exists and belongs to this tenant
    try:
        lead_oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")

    lead = await db.leads.find_one({"_id": lead_oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    calls = await db.call_records.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id}
    ).sort("initiated_at", -1).to_list(100)

    data = [_serialize_call(c, include_transcript=False) for c in calls]
    logger.info(f"List calls — lead_id={lead_id} count={len(data)}")
    return _success(data=data, message=f"{len(data)} call(s) found")


# ---------------------------------------------------------------------------
# GET /leads/:id/calls/:call_id  — single call with full transcript
# ---------------------------------------------------------------------------

@router.get("/leads/{lead_id}/calls/{call_id}")
async def get_call(
    lead_id: str,
    call_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Returns one call record with the full structured transcript array
    and Claude-extracted fields.

    Frontend uses this for the transcript viewer (chat-style bubble UI).
    If recording_url is present, show an audio player.
    Extracted data panel shows: qualification_outcome, extracted_fields, ai_summary.
    """
    db = get_db()

    try:
        call_oid = ObjectId(call_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid call ID")

    call = await db.call_records.find_one(
        {
            "_id": call_oid,
            "lead_id": lead_id,
            "tenant_id": current_user.tenant_id,
        }
    )
    if not call:
        raise HTTPException(status_code=404, detail="Call record not found")

    return _success(data=_serialize_call(call, include_transcript=True))


# ---------------------------------------------------------------------------
# GET /leads/:id/calls/:call_id/audio — proxy ElevenLabs recording
# ---------------------------------------------------------------------------

@router.get("/leads/{lead_id}/calls/{call_id}/audio")
async def get_call_audio(
    lead_id: str,
    call_id: str,
    token: Optional[str] = Query(default=None),
):
    """
    Stream the ElevenLabs call recording audio.
    Proxies GET /v1/convai/conversations/{conversation_id}/audio.
    Supports token as query param for <audio> elements that can't set headers.
    """
    import httpx
    from app.config import settings
    from app.auth import decode_access_token

    # Try Bearer header first, then query param token
    current_user = None
    if token:
        try:
            payload = decode_access_token(token)
            current_user = CurrentUser(
                user_id=payload["sub"], tenant_id=payload["tenant_id"],
                role=payload["role"], email=payload["email"],
            )
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required — pass ?token=<jwt>")

    db = get_db()
    try:
        call_oid = ObjectId(call_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid call ID")

    call = await db.call_records.find_one({
        "_id": call_oid,
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
    })
    if not call:
        raise HTTPException(status_code=404, detail="Call record not found")

    conv_id = call.get("conversation_id") or call.get("elevenlabs_conversation_id")
    if not conv_id:
        raise HTTPException(status_code=404, detail="No conversation ID — no recording available")

    if not settings.ELEVENLABS_API_KEY:
        raise HTTPException(status_code=503, detail="ElevenLabs not configured")

    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conv_id}/audio"
    headers = {"xi-api-key": settings.ELEVENLABS_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return StreamingResponse(
                iter([resp.content]),
                media_type=resp.headers.get("content-type", "audio/mpeg"),
                headers={"Content-Disposition": f'inline; filename="call_{call_id}.mp3"'},
            )
    except httpx.HTTPStatusError as exc:
        logger.warning(f"ElevenLabs audio fetch failed: {exc.response.status_code}")
        raise HTTPException(status_code=exc.response.status_code, detail="Recording not available")
    except Exception as exc:
        logger.error(f"Audio proxy error: {exc}")
        raise HTTPException(status_code=502, detail="Failed to fetch recording")


# ---------------------------------------------------------------------------
# GET /leads/:id/whatsapp-messages  — full WhatsApp thread for a lead
# ---------------------------------------------------------------------------

@router.get("/leads/{lead_id}/whatsapp-messages")
async def list_whatsapp_messages(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Returns the full WhatsApp message thread for a lead, sorted oldest first
    (so the frontend can render it as a chat window, with oldest messages at top).

    Includes all fields needed to render:
      - direction (INBOUND/OUTBOUND) — which side of the bubble
      - message_type (TEXT/DOCUMENT/IMAGE/TEMPLATE)
      - content — text body
      - media info (s3_key, filename, mime_type) — for file card inside bubble
      - template_name — shown as badge for template messages
      - status (SENT/DELIVERED/READ/FAILED/RECEIVED) — delivery status icon
      - sent_at — message timestamp

    Note: media files are NOT returned as content. Frontend calls
    GET /leads/:id/documents/:file_id/view to get a presigned URL for viewing.
    """
    db = get_db()

    try:
        lead_oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")

    lead = await db.leads.find_one({"_id": lead_oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    messages = await db.whatsapp_messages.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id}
    ).sort("sent_at", 1).to_list(500)

    data = [_serialize_message(m) for m in messages]
    logger.info(f"List WhatsApp messages — lead_id={lead_id} count={len(data)}")
    return _success(data=data, message=f"{len(data)} message(s) found")


# ---------------------------------------------------------------------------
# GET /leads/:id/emails  — email activity for a lead
# ---------------------------------------------------------------------------

@router.get("/leads/{lead_id}/emails")
async def list_email_messages(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Returns email exchanges for a lead:
    - Inbound emails from borrower (from activity_feed + email_messages collection)
    - Sorted newest first for timeline display
    """
    db = get_db()

    try:
        lead_oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")

    lead = await db.leads.find_one({"_id": lead_oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # Pull email-related activity feed events
    feed_events = await db.activity_feed.find(
        {
            "lead_id": lead_id,
            "tenant_id": current_user.tenant_id,
            "event_type": {"$in": ["EMAIL_RECEIVED", "EMAIL_SENT", "EMAIL_ATTACHMENT_PROCESSED"]},
        }
    ).sort("created_at", -1).to_list(200)

    # Pull from email_messages collection if it exists
    email_msgs = await db.email_messages.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id}
    ).sort("received_at", -1).to_list(200)

    # Normalize activity feed events
    emails = []
    for ev in feed_events:
        emails.append({
            "id": str(ev["_id"]),
            "direction": "INBOUND" if ev["event_type"] == "EMAIL_RECEIVED" else "OUTBOUND",
            "event_type": ev.get("event_type", ""),
            "subject": ev.get("subject", "(no subject)"),
            "from_email": ev.get("from_email", lead.get("email", "")),
            "body_preview": ev.get("message", ""),
            "attachments": ev.get("attachments", []),
            "timestamp": _isoformat(ev.get("created_at")),
            "source": "activity_feed",
        })

    # Normalize email_messages docs
    for em in email_msgs:
        emails.append({
            "id": str(em["_id"]),
            "direction": em.get("direction", "INBOUND"),
            "event_type": "EMAIL_RECEIVED",
            "subject": em.get("subject", "(no subject)"),
            "from_email": em.get("from_email", lead.get("email", "")),
            "body_preview": em.get("body_text", em.get("body_preview", "")),
            "attachments": em.get("attachments", []),
            "timestamp": _isoformat(em.get("received_at")),
            "source": "email_messages",
        })

    # Prefer email_messages over activity_feed.
    # If email_messages has records, use those and only add activity_feed
    # events that have no corresponding email_messages entry (by timestamp proximity).
    if email_msgs:
        # email_messages collection has the canonical records — use them all
        deduped = [e for e in emails if e.get("source") == "email_messages"]
    else:
        # Fallback: only activity_feed events exist
        deduped = [e for e in emails if e.get("source") == "activity_feed"]

    # Sort combined list by timestamp descending
    deduped.sort(key=lambda x: x["timestamp"], reverse=True)

    logger.info(f"List emails — lead_id={lead_id} count={len(deduped)}")
    return _success(data=deduped, message=f"{len(deduped)} email(s) found")
