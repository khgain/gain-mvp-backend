"""
Leads CRUD routes — PRD Section 4.

GET    /leads                       — paginated list with filters
POST   /leads                       — create single lead
POST   /leads/bulk                  — bulk create up to 500 leads
GET    /leads/{id}                  — single lead detail
PATCH  /leads/{id}                  — partial update
POST   /leads/{id}/verify-pan       — PAN verification
POST   /leads/{id}/trigger-agent    — manually trigger an agent
POST   /leads/{id}/override         — ops override (force underwriting, drop, etc.)
POST   /leads/{id}/log-action       — log a manual action
GET    /leads/{id}/timeline         — workflow run history for a lead
GET    /leads/{id}/validation       — Tier 1 + Tier 2 validation results
"""
import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import get_current_user, CurrentUser
from app.database import get_db
from app.models.lead import (
    LeadCreate,
    LeadUpdate,
    BulkLeadCreate,
    OverrideRequest,
    LogActionRequest,
)
from app.utils.encryption import encrypt_field, decrypt_field
from app.utils.logging import get_logger

router = APIRouter(prefix="/leads", tags=["Leads"])
logger = get_logger("routes.leads")


def _success(data=None, message="Success"):
    return {"success": True, "data": data, "message": message}


def _mask_pan(pan: str) -> str:
    if not pan or len(pan) < 10:
        return pan
    return pan[:5] + "****" + pan[-1]


def _hash_pan(pan: str) -> str:
    """Deterministic SHA-256 hash of normalised PAN for deduplication.
    Stored alongside encrypted PAN; enables unique-index check without decryption.
    """
    return sha256(pan.strip().upper().encode()).hexdigest()


def _mask_mobile(mobile: str) -> str:
    if not mobile or len(mobile) < 4:
        return mobile
    # Show first 2 digits + masked middle + last 4 digits  e.g. "91****0175"
    if len(mobile) >= 10:
        return mobile[:2] + "*" * (len(mobile) - 6) + mobile[-4:]
    return "*" * (len(mobile) - 4) + mobile[-4:]


def _serialize_lead(doc: dict) -> dict:
    result = {**doc}
    result["id"] = str(doc["_id"])
    result.pop("_id", None)
    pan_raw = result.pop("pan", None)
    mobile_raw = result.pop("mobile", None)
    if pan_raw:
        try:
            result["pan_masked"] = _mask_pan(decrypt_field(pan_raw))
        except Exception:
            result["pan_masked"] = "****"
    if mobile_raw:
        try:
            result["mobile_masked"] = _mask_mobile(decrypt_field(mobile_raw))
        except Exception:
            result["mobile_masked"] = "******"
    return result


@router.get("")
async def list_leads(
    campaign_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    assigned_to: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    query: dict = {"tenant_id": current_user.tenant_id}
    if campaign_id:
        query["campaign_id"] = campaign_id
    if status:
        query["status"] = status
    if assigned_to:
        query["assigned_to"] = assigned_to
    if date_from or date_to:
        date_filter: dict = {}
        if date_from:
            try:
                date_filter["$gte"] = datetime.fromisoformat(date_from)
            except ValueError:
                pass
        if date_to:
            try:
                date_filter["$lte"] = datetime.fromisoformat(date_to)
            except ValueError:
                pass
        if date_filter:
            query["created_at"] = date_filter

    total = await db.leads.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.leads.find(query).sort("created_at", -1).skip(skip).limit(page_size)
    leads = [_serialize_lead(doc) async for doc in cursor]

    # Enrich with campaign names
    campaign_ids = list(set(l.get("campaign_id") for l in leads if l.get("campaign_id")))
    campaign_map: dict = {}
    if campaign_ids:
        try:
            oid_list = [ObjectId(cid) for cid in campaign_ids if ObjectId.is_valid(cid)]
            if oid_list:
                async for camp in db.campaigns.find({"_id": {"$in": oid_list}}, {"name": 1}):
                    campaign_map[str(camp["_id"])] = camp.get("name", "")
        except Exception:
            pass
    for lead in leads:
        cid = lead.get("campaign_id")
        if cid and cid in campaign_map:
            lead["campaign_name"] = campaign_map[cid]

    return _success(data={
        "leads": leads,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
    })


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_lead(
    body: LeadCreate,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    now = datetime.now(timezone.utc)
    doc: dict = {
        "tenant_id": current_user.tenant_id,
        "campaign_id": body.campaign_id,
        "name": body.name,
        "company_name": body.company_name,
        "email": body.email,
        "loan_type": body.loan_type.value if body.loan_type else None,
        "entity_type": body.entity_type.value if body.entity_type else None,
        "loan_amount_requested": body.loan_amount_requested,
        "source": body.source.value if body.source else "DIRECT",
        "assigned_to": body.assigned_to,
        "status": "NEW",
        "pan_verified_by": None,
        "pan_verified_at": None,
        "qualification_result": None,
        "validation_flags": [],
        "metadata": body.metadata or {},
        "created_at": now,
        "updated_at": now,
    }
    if body.pan:
        pan_hash = _hash_pan(body.pan)
        existing = await db.leads.find_one(
            {"tenant_id": current_user.tenant_id, "pan_hash": pan_hash}
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "duplicate_pan",
                    "message": f"A lead with PAN {_mask_pan(body.pan)} already exists.",
                    "existing_lead_id": str(existing["_id"]),
                },
            )
        doc["pan"] = encrypt_field(body.pan)
        doc["pan_hash"] = pan_hash

    if body.mobile:
        doc["mobile"] = encrypt_field(body.mobile)
        # Compute mobile_hash at creation time (enables inbound WhatsApp matching)
        try:
            from app.services.whatsapp_service import compute_mobile_hash
            doc["mobile_hash"] = compute_mobile_hash(body.mobile)
        except Exception:
            pass

    result = await db.leads.insert_one(doc)
    lead_id = str(result.inserted_id)

    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": "LEAD_CREATED",
        "message": f"Lead created: {body.name}",
        "created_by": current_user.user_id,
        "created_at": now,
    })

    created = await db.leads.find_one({"_id": result.inserted_id})
    logger.info(f"Lead created — lead_id={lead_id} tenant={current_user.tenant_id}")
    return _success(data=_serialize_lead(created), message="Lead created")


@router.post("/bulk", status_code=status.HTTP_201_CREATED)
async def bulk_create_leads(
    body: BulkLeadCreate,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    now = datetime.now(timezone.utc)

    # Pre-compute PAN hashes and check existing in one DB query
    incoming_pan_hashes = {
        _hash_pan(lead.pan): lead.pan
        for lead in body.leads if lead.pan
    }
    existing_pan_hashes: set = set()
    if incoming_pan_hashes:
        cursor = db.leads.find(
            {"tenant_id": current_user.tenant_id, "pan_hash": {"$in": list(incoming_pan_hashes.keys())}},
            {"pan_hash": 1},
        )
        existing_pan_hashes = {doc["pan_hash"] async for doc in cursor}

    docs = []
    skipped = []
    for lead in body.leads:
        pan_hash = _hash_pan(lead.pan) if lead.pan else None
        if pan_hash and pan_hash in existing_pan_hashes:
            skipped.append({
                "name": lead.name,
                "pan_masked": _mask_pan(lead.pan),
                "reason": "duplicate_pan",
            })
            continue

        doc: dict = {
            "tenant_id": current_user.tenant_id,
            "campaign_id": body.campaign_id or lead.campaign_id,
            "name": lead.name,
            "company_name": lead.company_name,
            "email": lead.email,
            "loan_type": lead.loan_type.value if lead.loan_type else None,
            "entity_type": lead.entity_type.value if lead.entity_type else None,
            "loan_amount_requested": lead.loan_amount_requested,
            "source": lead.source.value if lead.source else "DIRECT",
            "assigned_to": lead.assigned_to,
            "status": "NEW",
            "pan_verified_by": None,
            "pan_verified_at": None,
            "qualification_result": None,
            "validation_flags": [],
            "metadata": lead.metadata or {},
            "created_at": now,
            "updated_at": now,
        }
        if lead.pan:
            doc["pan"] = encrypt_field(lead.pan)
            doc["pan_hash"] = pan_hash
            # Track hash so within-batch duplicates are also caught
            existing_pan_hashes.add(pan_hash)
        if lead.mobile:
            doc["mobile"] = encrypt_field(lead.mobile)
            try:
                from app.services.whatsapp_service import compute_mobile_hash
                doc["mobile_hash"] = compute_mobile_hash(lead.mobile)
            except Exception:
                pass
        docs.append(doc)

    count = 0
    if docs:
        result = await db.leads.insert_many(docs)
        count = len(result.inserted_ids)

    logger.info(
        f"Bulk created {count} leads, skipped {len(skipped)} duplicates — tenant={current_user.tenant_id}"
    )
    return _success(
        data={"created": count, "skipped": len(skipped), "skipped_details": skipped},
        message=f"{count} leads created, {len(skipped)} skipped (duplicate PAN)",
    )


@router.get("/{lead_id}")
async def get_lead(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    result = _serialize_lead(lead)
    # Enrich with campaign name
    cid = result.get("campaign_id")
    if cid and ObjectId.is_valid(cid):
        try:
            camp = await db.campaigns.find_one({"_id": ObjectId(cid)}, {"name": 1})
            if camp:
                result["campaign_name"] = camp.get("name", "")
        except Exception:
            pass
    return _success(data=result)


@router.patch("/{lead_id}")
async def update_lead(
    lead_id: str,
    body: LeadUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    update: dict = {"updated_at": datetime.now(timezone.utc)}
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        if value is not None:
            update[field] = value.value if hasattr(value, "value") else value
    await db.leads.update_one({"_id": oid}, {"$set": update})
    updated = await db.leads.find_one({"_id": oid})
    return _success(data=_serialize_lead(updated), message="Lead updated")


@router.post("/{lead_id}/verify-pan")
async def verify_pan(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    now = datetime.now(timezone.utc)
    pan_result = {"verified": False, "name_match": False, "name_on_pan": None}
    if lead.get("pan"):
        try:
            from app.services.pan_service import verify_pan as _verify_pan
            pan_raw = decrypt_field(lead["pan"])
            pan_result = await _verify_pan(pan_raw, lead.get("name", ""))
        except Exception as exc:
            logger.error(f"PAN verification failed for lead_id={lead_id}: {exc}")

    update: dict = {
        "updated_at": now,
        "pan_verified_by": current_user.user_id,
        "pan_verified_at": now,
    }
    if pan_result.get("verified"):
        update["status"] = "PAN_VERIFIED"

    await db.leads.update_one({"_id": oid}, {"$set": update})
    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": "PAN_VERIFIED",
        "message": f"PAN verification {'passed' if pan_result.get('verified') else 'attempted'}",
        "created_by": current_user.user_id,
        "created_at": now,
    })
    return _success(data=pan_result, message="PAN verification complete")


@router.post("/{lead_id}/trigger-agent")
async def trigger_agent(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    now = datetime.now(timezone.utc)
    try:
        from app.services.voice_service import trigger_outbound_call
        conv_id = await trigger_outbound_call(lead_id, current_user.tenant_id)
        message = f"Qualification call initiated (conv_id={conv_id})" if conv_id else "Call skipped (ElevenLabs not configured)"
    except Exception as exc:
        logger.error(f"Failed to trigger agent for lead_id={lead_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to trigger agent: {exc}")

    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": "AGENT_TRIGGERED",
        "message": f"Qualification call triggered by {current_user.email}",
        "created_by": current_user.user_id,
        "created_at": now,
    })
    return _success(message=message)


def _build_follow_up_wa_message(
    name: str, company: str,
    pending: list, received: list, failed: list, all_done: bool,
) -> str:
    """Build a WhatsApp follow-up message using real doc-tracker data."""
    if all_done:
        return (
            f"Hi {name}! ✅\n\n"
            f"Great news — all required documents for your"
            f"{' ' + company if company else ''} loan application have been "
            f"received and verified.\n\n"
            f"Your application is now being processed. We'll update you shortly!"
        )

    parts = [f"Hi {name}, here's an update on your{' ' + company if company else ''} loan application:\n"]

    if received:
        parts.append(f"✅ *Received & Verified ({len(received)}):*")
        for doc in received:
            parts.append(f"   • {doc}")
        parts.append("")

    if failed:
        parts.append(f"❌ *Needs Resubmission ({len(failed)}):*")
        for f_doc in failed:
            reason = f_doc.get("reason", "Validation failed") if isinstance(f_doc, dict) else "Validation failed"
            doc_name = f_doc.get("name", f_doc) if isinstance(f_doc, dict) else f_doc
            parts.append(f"   • {doc_name} — {reason}")
        parts.append("")

    if pending:
        parts.append(f"📋 *Still Needed ({len(pending)}):*")
        for i, doc in enumerate(pending, 1):
            parts.append(f"   {i}. {doc}")
        parts.append("")

    parts.append("Please send the remaining documents to continue your application.")
    return "\n".join(parts)


def _build_follow_up_email_html(
    name: str, company: str,
    pending: list, received: list, failed: list, all_done: bool,
) -> str:
    """Build HTML email body using real doc-tracker data."""
    sections = [f"<p>Dear {name},</p>"]

    if all_done:
        sections.append(
            f"<p>Great news — all required documents for your"
            f"{' ' + company if company else ''} loan application have been "
            f"received and verified. Your application is now being processed.</p>"
        )
        sections.append("<p>Best regards,<br/>Gain AI Lending Team</p>")
        return "\n".join(sections)

    sections.append(
        f"<p>Here is the current status of documents for your"
        f"{' ' + company if company else ''} loan application:</p>"
    )

    if received:
        sections.append("<p><strong>✅ Received & Verified:</strong></p><ul>")
        for doc in received:
            sections.append(f"<li>{doc}</li>")
        sections.append("</ul>")

    if failed:
        sections.append("<p><strong>❌ Needs Resubmission:</strong></p><ul>")
        for f_doc in failed:
            reason = f_doc.get("reason", "Validation failed") if isinstance(f_doc, dict) else "Validation failed"
            doc_name = f_doc.get("name", f_doc) if isinstance(f_doc, dict) else f_doc
            sections.append(f"<li>{doc_name} — {reason}</li>")
        sections.append("</ul>")

    if pending:
        sections.append("<p><strong>📋 Still Needed:</strong></p><ul>")
        for doc in pending:
            sections.append(f"<li>{doc}</li>")
        sections.append("</ul>")

    sections.append(
        "<p>Please reply to this email with the pending documents attached.</p>"
        "<p>Best regards,<br/>Gain AI Lending Team</p>"
    )
    return "\n".join(sections)


@router.post("/{lead_id}/trigger-follow-up")
async def trigger_follow_up(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Manually trigger follow-up reminders using real doc-tracker status."""
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    now = datetime.now(timezone.utc)
    results = {"whatsapp": False, "email": False}

    # ── Fetch real doc status from doc_tracker ──
    from app.services.doc_tracker import get_missing_docs_summary
    doc_summary = await get_missing_docs_summary(db, lead_id, current_user.tenant_id)
    pending_docs = doc_summary.get("pending_docs", [])
    received_docs = doc_summary.get("received_docs", [])
    failed_docs = doc_summary.get("failed_docs", [])
    all_done = doc_summary.get("all_done", False)

    logger.info(
        f"Follow-up doc status for lead_id={lead_id}: "
        f"pending={len(pending_docs)} received={len(received_docs)} "
        f"failed={len(failed_docs)} all_done={all_done}"
    )

    borrower_name = lead.get("name", "Borrower")
    company_name = lead.get("company_name", borrower_name)

    # Send WhatsApp reminder with real doc status
    try:
        from app.utils.encryption import decrypt_field
        mobile_raw = decrypt_field(lead.get("mobile", ""))

        if mobile_raw:
            from app.services.whatsapp_service import send_text_message
            wa_message = _build_follow_up_wa_message(
                borrower_name, company_name, pending_docs, received_docs, failed_docs, all_done,
            )
            await send_text_message(lead_id, mobile_raw, wa_message, tenant_id=current_user.tenant_id)
            results["whatsapp"] = True
            logger.info(f"WhatsApp follow-up sent — lead_id={lead_id}")
    except Exception as exc:
        logger.warning(f"Manual follow-up WhatsApp failed for lead_id={lead_id}: {exc}")

    # Send email reminder with real doc status
    try:
        borrower_email = lead.get("email", "")
        if borrower_email:
            from app.services.email_service import send_status_update_email
            html_body = _build_follow_up_email_html(
                borrower_name, company_name, pending_docs, received_docs, failed_docs, all_done,
            )
            subject = f"Document Status Update — {company_name} Loan Application"
            results["email"] = await send_status_update_email(
                lead_id=lead_id,
                tenant_id=current_user.tenant_id,
                borrower_email=borrower_email,
                subject=subject,
                body_html=html_body,
            )
    except Exception as exc:
        logger.warning(f"Manual follow-up email failed for lead_id={lead_id}: {exc}")

    # Log activity
    channels = []
    if results["whatsapp"]:
        channels.append("WhatsApp")
    if results["email"]:
        channels.append("Email")
    channel_str = " + ".join(channels) if channels else "None (services not configured)"

    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": "FOLLOW_UP_TRIGGERED",
        "message": (
            f"Manual follow-up triggered by {current_user.email} via {channel_str}. "
            f"{len(pending_docs)} pending, {len(received_docs)} received, {len(failed_docs)} failed."
        ),
        "detail": {
            "pending_count": len(pending_docs),
            "received_count": len(received_docs),
            "failed_count": len(failed_docs),
            "all_done": all_done,
        },
        "created_by": current_user.user_id,
        "created_at": now,
    })

    return _success(
        data=results,
        message=f"Follow-up sent via {channel_str}",
    )


@router.get("/debug/connectivity")
async def debug_connectivity():
    """Diagnostic endpoint — test WAHA and SendGrid connectivity."""
    from app.config import settings
    import httpx

    results = {
        "waha_base_url": settings.WAHA_BASE_URL or "(not set)",
        "waha_api_key_set": bool(settings.WAHA_API_KEY),
        "sendgrid_api_key_set": bool(settings.SENDGRID_API_KEY),
        "anthropic_api_key_set": bool(settings.ANTHROPIC_API_KEY),
        "celery_eager": os.getenv("CELERY_TASK_ALWAYS_EAGER", "false"),
        "waha_status": "not_configured",
        "sendgrid_status": "not_configured",
    }

    # Test WAHA connectivity
    if settings.WAHA_BASE_URL and settings.WAHA_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{settings.WAHA_BASE_URL.rstrip('/')}/api/sessions",
                    headers={"X-Api-Key": settings.WAHA_API_KEY},
                )
                results["waha_status"] = f"HTTP {resp.status_code}"
                results["waha_sessions"] = resp.json() if resp.status_code == 200 else resp.text[:200]
        except Exception as exc:
            results["waha_status"] = f"error: {exc}"

    # Test SendGrid (just verify API key works)
    if settings.SENDGRID_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.sendgrid.com/v3/user/profile",
                    headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"},
                )
                results["sendgrid_status"] = f"HTTP {resp.status_code}"
        except Exception as exc:
            results["sendgrid_status"] = f"error: {exc}"

    return _success(data=results, message="Connectivity check complete")


@router.post("/{lead_id}/override")
async def override_lead(
    lead_id: str,
    body: OverrideRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    now = datetime.now(timezone.utc)
    update: dict = {"updated_at": now}
    if body.action == "FORCE_UNDERWRITING":
        update["status"] = "READY_FOR_UNDERWRITING"
    elif body.action == "DROP":
        update["status"] = "DROPPED"
    elif body.action == "CHANGE_FOLLOW_UP_FREQUENCY":
        if body.follow_up_frequency_days:
            update["follow_up_frequency_days"] = body.follow_up_frequency_days
    elif body.action == "advance_to_doc_collection":
        update["status"] = "DOC_COLLECTION"
        # Best-effort: enqueue WhatsApp doc checklist
        try:
            from app.workers.whatsapp_worker import send_doc_checklist_whatsapp
            send_doc_checklist_whatsapp.apply_async(
                kwargs={"lead_id": lead_id, "tenant_id": current_user.tenant_id},
                queue="whatsapp",
            )
            logger.info(f"Doc checklist enqueued for lead_id={lead_id} via manual advance")
        except Exception as exc:
            logger.warning(f"WhatsApp checklist enqueue failed on manual advance for lead_id={lead_id}: {exc}")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")

    await db.leads.update_one({"_id": oid}, {"$set": update})
    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": "LEAD_OVERRIDE",
        "message": f"Override: {body.action} — {body.reason}",
        "created_by": current_user.user_id,
        "created_at": now,
    })
    updated = await db.leads.find_one({"_id": oid})
    return _success(data=_serialize_lead(updated), message=f"Lead {body.action.lower().replace('_', ' ')}")


@router.post("/{lead_id}/log-action")
async def log_manual_action(
    lead_id: str,
    body: LogActionRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    now = datetime.now(timezone.utc)
    await db.manual_actions.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "action_type": body.action_type,
        "notes": body.notes,
        "outcome": body.outcome,
        "created_by": current_user.user_id,
        "created_at": now,
    })
    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": body.action_type,
        "message": body.notes,
        "created_by": current_user.user_id,
        "created_at": now,
    })
    return _success(message="Action logged")


@router.get("/{lead_id}/timeline")
async def get_lead_timeline(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    runs = await db.workflow_runs.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id}
    ).sort("executed_at", 1).to_list(200)

    timeline = [
        {
            "id": str(r["_id"]),
            "node_type": r.get("node_type"),
            "status": r.get("status"),
            "triggered_by": r.get("triggered_by"),
            "executed_at": r.get("executed_at"),
            "input_data": r.get("input_data", {}),
            "output_data": r.get("output_data", {}),
        }
        for r in runs
    ]
    return _success(data={"lead_id": lead_id, "events": timeline})


@router.get("/{lead_id}/validation")
async def get_lead_validation(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(lead_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid lead ID")
    lead = await db.leads.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    tier1_results = []
    async for doc in db.logical_docs.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id},
        sort=[("created_at", 1)],
    ):
        doc_type = doc.get("doc_type", "Unknown")
        t1 = doc.get("tier1_validation") or {}
        rule_results = t1.get("rule_results", [])
        for rr in rule_results:
            tier1_results.append({
                "document": doc_type.replace("_", " ").title(),
                "rule": rr.get("rule_name") or rr.get("rule_id", ""),
                "status": "passed" if rr.get("passed") else "failed",
                "details": rr.get("reason") or rr.get("message") or "",
            })
        if not rule_results:
            tier1_results.append({
                "document": doc_type.replace("_", " ").title(),
                "rule": "Validation pending",
                "status": "pending",
                "details": f"Document status: {doc.get('status', 'unknown')}",
            })

    tier2_results = []
    tier2_event = await db.activity_feed.find_one(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id, "event_type": "TIER2_VALIDATION_COMPLETE"},
        sort=[("created_at", -1)],
    )
    if tier2_event:
        for rr in (tier2_event.get("metadata") or {}).get("rule_results", []):
            tier2_results.append({
                "rule": rr.get("rule_name") or rr.get("rule_id", ""),
                "status": "passed" if rr.get("passed") else "failed",
                "details": rr.get("reason") or rr.get("message") or "",
            })

    return _success(data={
        "tier1": tier1_results,
        "tier2": tier2_results,
        "tier2_run": tier2_event is not None,
    })
