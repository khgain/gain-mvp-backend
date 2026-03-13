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
    return "******" + mobile[-4:]


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
    return _success(data=_serialize_lead(lead))


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
