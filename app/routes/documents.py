"""
Documents API — physical files and logical docs management for a lead.

All endpoints are scoped to the authenticated user's tenant.

Endpoints:
  GET  /leads/{id}/documents                          — all phys_files + logical_docs
  GET  /leads/{id}/physical-files                     — list physical files only
  GET  /leads/{id}/physical-files/{file_id}/view-url — presigned S3 view URL
  POST /leads/{id}/physical-files/{file_id}/review   — human review (confirm/split/partial)
  POST /leads/{id}/physical-files/{file_id}/reprocess — re-enqueue classification
  GET  /leads/{id}/logical-docs                       — list logical docs with extraction + T1
  GET  /leads/{id}/logical-docs/{doc_id}              — single logical doc (full data)
  POST /leads/{id}/logical-docs/group                 — group physical files into one logical doc
  POST /leads/{id}/logical-docs/{doc_id}/reject       — reject a logical doc
  GET  /leads/{id}/download-zip                       — stream all approved docs as ZIP
  POST /leads/{id}/upload                             — get presigned PUT URL for browser upload
"""
import time
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response

from app.auth import get_current_user, CurrentUser, require_role
from app.database import get_db
from app.models.document import (
    ReviewSubmitRequest,
    GroupFilesRequest,
    RejectDocRequest,
    PhysicalFileResponse,
    LogicalDocResponse,
)
from app.utils.logging import get_logger

router = APIRouter(prefix="/leads", tags=["Documents"])
logger = get_logger("routes.documents")


def _success(data=None, message="Success"):
    return {"success": True, "data": data, "message": message}


def _not_found(what: str = "Document"):
    raise HTTPException(status_code=404, detail=f"{what} not found")


async def _get_lead_or_404(db, lead_id: str, tenant_id: str) -> dict:
    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


def _serialize_phys_file(doc: dict) -> dict:
    d = {**doc}
    d["id"] = str(doc["_id"])
    d.pop("_id", None)
    return d


def _serialize_logical_doc(doc: dict) -> dict:
    d = {**doc}
    d["id"] = str(doc["_id"])
    d.pop("_id", None)
    return d


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/doc-tracker — full checklist with status per doc
# ---------------------------------------------------------------------------

@router.get("/{lead_id}/doc-tracker")
async def get_doc_tracker(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Return the full document checklist with receive/validation status for each item.
    Used by the Documents tab to show required vs received vs validated.
    """
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    from app.services.doc_tracker import get_doc_status
    tracker = await get_doc_status(db, lead_id, current_user.tenant_id)
    return _success(tracker)


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/activity-timeline — activity feed + next planned action
# ---------------------------------------------------------------------------

@router.get("/{lead_id}/activity-timeline")
async def get_activity_timeline(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Return activity timeline for a lead with all events and next planned action.
    """
    db = get_db()
    lead = await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    # Fetch all activity events (deduped by event_type + message within 60s)
    events = []
    seen_keys = {}  # (event_type, message) -> latest created_at timestamp
    for ev in await db.activity_feed.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id},
        sort=[("created_at", -1)],
    ).to_list(200):
        evt_type = ev.get("event_type", "")
        msg = ev.get("message", "")
        created = ev.get("created_at")
        dedup_key = (evt_type, msg)

        # Skip duplicate if same event_type + message within 60 seconds
        if dedup_key in seen_keys and created and hasattr(created, "timestamp"):
            prev_ts = seen_keys[dedup_key]
            if abs(prev_ts.timestamp() - created.timestamp()) < 60:
                continue

        if created and hasattr(created, "timestamp"):
            seen_keys[dedup_key] = created

        ts_str = ""
        if hasattr(created, "isoformat"):
            ts_str = created.isoformat()
            if not ts_str.endswith("Z") and "+" not in ts_str:
                ts_str += "Z"
        else:
            ts_str = str(created or "")

        events.append({
            "id": str(ev["_id"]),
            "event_type": evt_type,
            "message": msg,
            "created_at": ts_str,
            "created_by": ev.get("created_by"),
            "metadata": ev.get("metadata") or ev.get("detail"),
        })

    # Compute next planned action based on lead status and doc state
    from app.services.doc_tracker import get_doc_status
    tracker = await get_doc_status(db, lead_id, current_user.tenant_id)
    summary = tracker.get("summary", {})
    lead_status = lead.get("status", "NEW")

    next_action = _compute_next_action(lead_status, summary, lead)

    return _success({
        "events": events,
        "total": len(events),
        "next_action": next_action,
    })


def _compute_next_action(status: str, doc_summary: dict, lead: dict) -> dict:
    """Determine the next planned action for a lead."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    if status == "NEW":
        return {
            "type": "PAN_VERIFICATION",
            "label": "PAN verification pending",
            "description": "Verify PAN to proceed with qualification call",
            "scheduled_at": None,
        }
    elif status == "PAN_VERIFIED":
        return {
            "type": "QUALIFICATION_CALL",
            "label": "Qualification call scheduled",
            "description": "AI voice agent will call the borrower for qualification",
            "scheduled_at": (now + timedelta(minutes=5)).isoformat(),
        }
    elif status == "QUALIFIED":
        return {
            "type": "DOC_COLLECTION",
            "label": "Document collection starting",
            "description": "Sending document checklist to borrower",
            "scheduled_at": now.isoformat(),
        }
    elif status == "DOC_COLLECTION":
        pending = doc_summary.get("pending", 0)
        failed = doc_summary.get("failed", 0)
        if pending > 0 or failed > 0:
            # Calculate next follow-up based on last activity
            updated_at = lead.get("updated_at", now)
            days_since = (now - updated_at).days if hasattr(updated_at, "days") else 0
            next_followup_days = [1, 3, 5, 7]
            next_day = next(
                (d for d in next_followup_days if d > days_since), 7
            )
            followup_time = updated_at + timedelta(days=next_day) if hasattr(updated_at, "__add__") else now + timedelta(days=next_day)

            items = []
            if pending > 0:
                items.append(f"{pending} document(s) pending")
            if failed > 0:
                items.append(f"{failed} document(s) need resubmission")

            return {
                "type": "FOLLOW_UP",
                "label": f"Follow-up reminder (Day {next_day})",
                "description": f"Waiting for borrower: {', '.join(items)}",
                "scheduled_at": followup_time.isoformat() if hasattr(followup_time, "isoformat") else None,
            }
        else:
            return {
                "type": "VALIDATION",
                "label": "Document validation in progress",
                "description": "All documents received — running AI validation checks",
                "scheduled_at": None,
            }
    elif status == "READY_FOR_UNDERWRITING":
        return {
            "type": "UNDERWRITING",
            "label": "Ready for underwriting",
            "description": "All validations passed — lead is ready for credit decision",
            "scheduled_at": None,
        }
    elif status == "NOT_QUALIFIED":
        return {
            "type": "CLOSED",
            "label": "Lead not qualified",
            "description": "Lead did not meet qualification criteria",
            "scheduled_at": None,
        }
    else:
        return {
            "type": "UNKNOWN",
            "label": f"Status: {status}",
            "description": "No automatic action configured for this status",
            "scheduled_at": None,
        }


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/documents — combined overview
# ---------------------------------------------------------------------------

@router.get("/{lead_id}/documents")
async def get_lead_documents(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return all physical files and logical docs for a lead."""
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    phys_files = []
    async for doc in db.phys_files.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id},
        sort=[("created_at", -1)],
    ):
        phys_files.append(_serialize_phys_file(doc))

    logical_docs = []
    async for doc in db.logical_docs.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id},
        sort=[("created_at", 1)],
    ):
        logical_docs.append(_serialize_logical_doc(doc))

    return _success({
        "physical_files": phys_files,
        "logical_docs": logical_docs,
        "stats": {
            "total_files": len(phys_files),
            "total_logical_docs": len(logical_docs),
            "needs_review": sum(1 for f in phys_files if f.get("status") == "NEEDS_HUMAN_REVIEW"),
            "tier1_passed": sum(1 for d in logical_docs if d.get("status") == "TIER1_PASSED"),
            "tier1_failed": sum(1 for d in logical_docs if d.get("status") == "TIER1_FAILED"),
        },
    })


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/physical-files
# ---------------------------------------------------------------------------

@router.get("/{lead_id}/physical-files")
async def list_physical_files(
    lead_id: str,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List all physical files received for a lead."""
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    query = {"lead_id": lead_id, "tenant_id": current_user.tenant_id}
    if status_filter:
        query["status"] = status_filter

    files = []
    async for doc in db.phys_files.find(query, sort=[("created_at", -1)]):
        files.append(_serialize_phys_file(doc))

    return _success({"files": files, "total": len(files)})


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/physical-files/{file_id}/view-url
# ---------------------------------------------------------------------------

@router.get("/{lead_id}/physical-files/{file_id}/view-url")
async def get_file_view_url(
    lead_id: str,
    file_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Generate a 1-hour presigned S3 URL to view a physical file."""
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    phys_file = await db.phys_files.find_one({
        "_id": ObjectId(file_id),
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
    })
    if not phys_file:
        _not_found("File")

    from app.services.storage_service import generate_view_url
    url = await generate_view_url(
        s3_key=phys_file["s3_key"],
        tenant_id=current_user.tenant_id,
        lead_tenant_id=phys_file["tenant_id"],
        expiry_seconds=3600,
    )
    return _success({"url": url, "expires_in_seconds": 3600})


# ---------------------------------------------------------------------------
# POST /leads/{lead_id}/physical-files/{file_id}/review — human review
# ---------------------------------------------------------------------------

@router.post("/{lead_id}/physical-files/{file_id}/review")
async def submit_review(
    lead_id: str,
    file_id: str,
    body: ReviewSubmitRequest,
    current_user: CurrentUser = Depends(require_role("TENANT_ADMIN", "CAMPAIGN_MANAGER")),
):
    """
    Submit a human review decision for an ambiguous physical file.

    Decisions:
      CONFIRM_SINGLE  — confirm this file is doc_type X, proceed to extraction
      SPLIT_BUNDLED   — this file has multiple docs; split by page range
      MARK_PARTIAL    — this is one part of a multi-page document
    """
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    phys_file = await db.phys_files.find_one({
        "_id": ObjectId(file_id),
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
    })
    if not phys_file:
        _not_found("File")

    now = datetime.now(timezone.utc)

    if body.decision.value == "CONFIRM_SINGLE":
        # Create a logical doc and enqueue extraction
        if not body.doc_type:
            raise HTTPException(status_code=400, detail="doc_type required for CONFIRM_SINGLE")

        result = await db.logical_docs.insert_one({
            "lead_id": lead_id,
            "tenant_id": current_user.tenant_id,
            "doc_type": body.doc_type.value,
            "assembly_type": "SINGLE",
            "physical_file_ids": [file_id],
            "completeness_status": "COMPLETE",
            "is_mandatory": True,
            "extracted_data": {},
            "status": "READY_FOR_EXTRACTION",
            "created_at": now,
            "updated_at": now,
        })
        logical_doc_id = str(result.inserted_id)

        # Update phys_file
        await db.phys_files.update_one(
            {"_id": ObjectId(file_id)},
            {"$set": {
                "status": "HUMAN_REVIEWED",
                "ambiguity_type": "NORMAL",
                "reviewer_id": current_user.id,
                "reviewed_at": now,
                "review_notes": body.notes,
                "updated_at": now,
            }, "$addToSet": {"logical_doc_ids": logical_doc_id}},
        )

        # Log to activity feed
        await db.activity_feed.insert_one({
            "lead_id": lead_id,
            "tenant_id": current_user.tenant_id,
            "event_type": "DOCUMENT_RECEIVED",
            "message": f"Document '{phys_file.get('original_filename', 'unknown')}' classified as {body.doc_type.value.replace('_', ' ').title()} by reviewer",
            "created_at": now,
            "created_by": current_user.id,
        })

        # Enqueue extraction
        try:
            from app.workers.ai_worker import extract_document_data
            extract_document_data.apply_async(
                kwargs={"logical_doc_id": logical_doc_id, "tenant_id": current_user.tenant_id},
                queue="ai-extraction",
            )
        except Exception as exc:
            logger.warning(f"Failed to enqueue extraction for logical_doc_id={logical_doc_id}: {exc}")

        return _success({"decision": "CONFIRM_SINGLE", "logical_doc_id": logical_doc_id}, "Review submitted")

    elif body.decision.value == "SPLIT_BUNDLED":
        # For each split definition, create a child PhysicalFile stub and LogicalDoc
        if not body.splits:
            raise HTTPException(status_code=400, detail="splits required for SPLIT_BUNDLED")

        logical_doc_ids = []
        for split in body.splits:
            child_result = await db.logical_docs.insert_one({
                "lead_id": lead_id,
                "tenant_id": current_user.tenant_id,
                "doc_type": split.doc_type.value,
                "assembly_type": "EXTRACTED",
                "physical_file_ids": [file_id],
                "completeness_status": "COMPLETE",
                "is_mandatory": True,
                "extracted_data": {},
                "page_range": split.page_range,  # stored for auditing
                "status": "READY_FOR_EXTRACTION",
                "created_at": now,
                "updated_at": now,
            })
            logical_doc_ids.append(str(child_result.inserted_id))

            from app.workers.ai_worker import extract_document_data
            extract_document_data.apply_async(
                kwargs={"logical_doc_id": str(child_result.inserted_id), "tenant_id": current_user.tenant_id},
                queue="ai-extraction",
            )

        await db.phys_files.update_one(
            {"_id": ObjectId(file_id)},
            {"$set": {
                "status": "HUMAN_REVIEWED",
                "ambiguity_type": "BUNDLED",
                "reviewer_id": current_user.id,
                "reviewed_at": now,
                "review_notes": body.notes,
                "updated_at": now,
            }, "$addToSet": {"logical_doc_ids": {"$each": logical_doc_ids}}},
        )
        return _success({"decision": "SPLIT_BUNDLED", "logical_doc_ids": logical_doc_ids}, "Review submitted")

    elif body.decision.value == "MARK_PARTIAL":
        await db.phys_files.update_one(
            {"_id": ObjectId(file_id)},
            {"$set": {
                "status": "HUMAN_REVIEWED",
                "ambiguity_type": "PARTIAL",
                "reviewer_id": current_user.id,
                "reviewed_at": now,
                "review_notes": body.notes,
                "updated_at": now,
            }},
        )
        return _success({"decision": "MARK_PARTIAL"}, "Marked as partial document")

    elif body.decision.value == "REJECT":
        # Mark phys file as rejected
        await db.phys_files.update_one(
            {"_id": ObjectId(file_id)},
            {"$set": {
                "status": "HUMAN_REVIEWED",
                "ambiguity_type": "NORMAL",
                "reviewer_id": current_user.id,
                "reviewed_at": now,
                "review_notes": body.notes or "Rejected by reviewer",
                "updated_at": now,
            }},
        )
        # Log to activity feed
        await db.activity_feed.insert_one({
            "lead_id": lead_id,
            "tenant_id": current_user.tenant_id,
            "event_type": "DOCUMENT_REJECTED",
            "message": f"Document '{phys_file.get('original_filename', 'unknown')}' rejected by reviewer"
                       + (f": {body.notes}" if body.notes else ""),
            "created_at": now,
            "created_by": current_user.id,
        })
        return _success({"decision": "REJECT"}, "Document rejected")

    raise HTTPException(status_code=400, detail="Unknown review decision")


# ---------------------------------------------------------------------------
# POST /leads/{lead_id}/physical-files/{file_id}/reprocess
# ---------------------------------------------------------------------------

@router.post("/{lead_id}/physical-files/{file_id}/reprocess")
async def reprocess_file(
    lead_id: str,
    file_id: str,
    current_user: CurrentUser = Depends(require_role("TENANT_ADMIN", "CAMPAIGN_MANAGER")),
):
    """Re-enqueue a physical file for classification (useful after manual fixes)."""
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    phys_file = await db.phys_files.find_one({
        "_id": ObjectId(file_id),
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
    })
    if not phys_file:
        _not_found("File")

    now = datetime.now(timezone.utc)
    await db.phys_files.update_one(
        {"_id": ObjectId(file_id)},
        {"$set": {"status": "RECEIVED", "updated_at": now}},
    )

    from app.workers.ai_worker import classify_document
    classify_document.apply_async(
        kwargs={"phys_file_id": file_id, "tenant_id": current_user.tenant_id},
        queue="ai-classification",
    )
    return _success({"status": "reprocessing"}, "File queued for re-classification")


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/logical-docs
# ---------------------------------------------------------------------------

@router.get("/{lead_id}/logical-docs")
async def list_logical_docs(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """List all logical documents with their extraction results and Tier 1 validation."""
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    docs = []
    async for doc in db.logical_docs.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id},
        sort=[("created_at", 1)],
    ):
        docs.append(_serialize_logical_doc(doc))

    return _success({"docs": docs, "total": len(docs)})


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/validation — Per-document grouped validation results
# ---------------------------------------------------------------------------

# Mapping from human-readable checklist names → doc_type enum values
_CHECKLIST_NAME_TO_DOC_TYPE = {
    "aadhaar": "AADHAAR",
    "aadhaar card": "AADHAAR",
    "aadhaar card (front & back)": "AADHAAR",
    "aadhaar card of all partners": "AADHAAR",
    "aadhaar + pan of all directors": "AADHAAR",
    "pan card": "PAN_CARD",
    "pan card of firm and partners": "PAN_CARD",
    "bank statement": "BANK_STATEMENT",
    "bank statement (last 12 months)": "BANK_STATEMENT",
    "itr": "ITR",
    "latest itr": "ITR",
    "latest itr with computation": "ITR",
    "latest 2 years itr / audited p&l": "ITR",
    "gst certificate": "GST_CERT",
    "gst returns": "GST_RETURN",
    "gst returns (last 6 months)": "GST_RETURN",
    "udyam registration certificate": "UDYAM",
    "office address proof": "ADDRESS_PROOF",
    "address proof": "ADDRESS_PROOF",
    "partnership deed": "PARTNERSHIP_DEED",
    "certificate of incorporation (coi)": "COI",
    "moa + aoa": "MOA",
    "audited p&l + balance sheet (2 years)": "AUDITED_PL",
}


def _checklist_name_to_doc_type(name: str) -> str:
    """Convert a human-readable checklist name to a doc_type enum value."""
    return _CHECKLIST_NAME_TO_DOC_TYPE.get(name.lower().strip(), name.upper().replace(" ", "_"))


def _pretty_label(doc_type: str) -> str:
    """PAN_CARD → Pan Card, GST_CERT → GST Certificate, etc."""
    nice = {
        "AADHAAR": "Aadhaar Card",
        "PAN_CARD": "PAN Card",
        "BANK_STATEMENT": "Bank Statement",
        "ITR": "ITR",
        "GST_CERT": "GST Certificate",
        "GST_RETURN": "GST Return",
        "UDYAM": "Udyam Certificate",
        "ADDRESS_PROOF": "Address Proof",
        "PARTNERSHIP_DEED": "Partnership Deed",
        "COI": "Certificate of Incorporation",
        "MOA": "MOA / AOA",
        "AOA": "AOA",
        "AUDITED_PL": "Audited P&L / Balance Sheet",
        "ELECTRICITY_BILL": "Electricity Bill",
        "PASSPORT_PHOTO": "Passport Photo",
    }
    return nice.get(doc_type, doc_type.replace("_", " ").title())


# Priority for picking the "best" logical doc when multiple exist for same type
_STATUS_PRIORITY = {
    "TIER1_PASSED": 0, "HUMAN_REVIEWED": 1, "TIER2_PASSED": 0,
    "TIER1_FAILED": 2, "EXTRACTED": 3, "EXTRACTING": 4,
    "READY_FOR_EXTRACTION": 5, "ASSEMBLING": 6, "PENDING": 7,
    "REJECTED": 8, "NEEDS_HUMAN_REVIEW": 2,
}


@router.get("/{lead_id}/validation")
async def get_lead_validation(
    lead_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Return per-document grouped validation results for the Validation tab.

    Response includes every required + optional document from the checklist,
    with tier1 check details for received docs and "AWAITING" for unreceived.
    Tier 2 cross-document results shown separately.
    """
    db = get_db()
    lead = await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    entity_type = lead.get("entity_type", "INDIVIDUAL")
    logger.info(f"Validation tab — lead_id={lead_id} entity_type={entity_type}")

    # 1. Get doc checklist config
    from app.services.validation_rules import get_doc_collection_config
    doc_config = await get_doc_collection_config(db, current_user.tenant_id)
    checklist_by_entity = doc_config.get("doc_checklist_by_entity_type", {})
    logger.info(f"Validation tab — checklist entity types: {list(checklist_by_entity.keys())}")

    # Try exact match first, then case-insensitive, then default to INDIVIDUAL
    entity_checklist = checklist_by_entity.get(entity_type, {})
    if not entity_checklist:
        # Case-insensitive fallback
        for key, val in checklist_by_entity.items():
            if key.upper() == entity_type.upper():
                entity_checklist = val
                break
    if not entity_checklist:
        # Fall back to INDIVIDUAL or first available
        entity_checklist = checklist_by_entity.get("INDIVIDUAL", {})
        if not entity_checklist and checklist_by_entity:
            entity_checklist = next(iter(checklist_by_entity.values()), {})
        logger.warning(f"Validation tab — no checklist for entity_type={entity_type}, using fallback")

    required_names = entity_checklist.get("required", [])
    optional_names = entity_checklist.get("optional", [])
    logger.info(f"Validation tab — required={len(required_names)} optional={len(optional_names)}")

    # Build ordered list of (doc_type, label, required)
    checklist_items = []   # list of (doc_type, label, required)
    seen_types = set()
    for name in required_names:
        dt = _checklist_name_to_doc_type(name)
        if dt not in seen_types:
            checklist_items.append((dt, name, True))
            seen_types.add(dt)
    for name in optional_names:
        dt = _checklist_name_to_doc_type(name)
        if dt not in seen_types:
            checklist_items.append((dt, name, False))
            seen_types.add(dt)

    logger.info(f"Validation tab — checklist_items={[(c[0], c[2]) for c in checklist_items]}")

    # 2. Fetch all logical_docs for this lead
    logical_docs_by_type = {}  # doc_type → list of logical_doc dicts
    async for doc in db.logical_docs.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id},
        sort=[("created_at", -1)],
    ):
        dt = doc.get("doc_type", "UNKNOWN")
        logical_docs_by_type.setdefault(dt, []).append(doc)

    logger.info(f"Validation tab — logical_docs types: {list(logical_docs_by_type.keys())} total={sum(len(v) for v in logical_docs_by_type.values())}")

    # Also add any doc types found in DB but not in checklist
    for dt in logical_docs_by_type:
        if dt not in seen_types and dt != "UNKNOWN":
            checklist_items.append((dt, _pretty_label(dt), False))
            seen_types.add(dt)

    # 3. Build per-document response
    documents = []
    summary = {"total_docs": 0, "received": 0, "passed": 0, "failed": 0, "awaiting": 0}

    for doc_type, checklist_name, is_required in checklist_items:
        summary["total_docs"] += 1
        candidates = logical_docs_by_type.get(doc_type, [])

        if not candidates:
            # Document not yet received
            summary["awaiting"] += 1
            documents.append({
                "doc_type": doc_type,
                "label": _pretty_label(doc_type),
                "checklist_name": checklist_name,
                "required": is_required,
                "received": False,
                "status": "AWAITING",
                "checks_passed": 0,
                "checks_total": 0,
                "checks": [],
                "extracted_fields": {},
            })
            continue

        # Pick the best logical doc (prefer passed > failed > others)
        best = min(candidates, key=lambda d: _STATUS_PRIORITY.get(d.get("status", "PENDING"), 99))
        doc_status = best.get("status", "PENDING")
        t1 = best.get("tier1_validation") or {}
        rule_results = t1.get("rule_results", [])
        extracted = best.get("extracted_data") or {}

        checks = []
        passed_count = 0
        for rr in rule_results:
            is_passed = rr.get("passed", rr.get("status") == "PASS")
            if is_passed:
                passed_count += 1
            # Try to find the extracted value relevant to this check
            rule_name = rr.get("rule_name", rr.get("rule_id", ""))
            checks.append({
                "rule_name": rule_name,
                "passed": bool(is_passed),
                "message": rr.get("message", rr.get("detail", "")),
            })

        summary["received"] += 1
        if doc_status in ("TIER1_PASSED", "TIER2_PASSED", "HUMAN_REVIEWED"):
            summary["passed"] += 1
        elif doc_status == "TIER1_FAILED":
            summary["failed"] += 1

        documents.append({
            "doc_type": doc_type,
            "label": _pretty_label(doc_type),
            "checklist_name": checklist_name,
            "required": is_required,
            "received": True,
            "status": doc_status,
            "checks_passed": passed_count,
            "checks_total": len(rule_results),
            "checks": checks,
            "extracted_fields": {k: str(v) if v is not None else "" for k, v in extracted.items()},
        })

    # 4. Tier 2: cross-document results from activity_feed
    tier2 = []
    t2_events = await db.activity_feed.find(
        {"lead_id": lead_id, "tenant_id": current_user.tenant_id, "event_type": "TIER2_VALIDATION_COMPLETE"},
        sort=[("created_at", -1)],
    ).to_list(10)

    for event in t2_events:
        metadata = event.get("metadata", {})
        for rr in metadata.get("rule_results", []):
            is_passed = rr.get("passed", rr.get("status") == "PASS")
            tier2.append({
                "rule_name": rr.get("rule_name", rr.get("rule_id", "—")),
                "passed": bool(is_passed),
                "message": rr.get("message", rr.get("detail", "")),
                "sources": rr.get("sources", []),
            })

    return _success({
        "documents": documents,
        "tier2": tier2,
        "summary": summary,
    })


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/logical-docs/{doc_id}
# ---------------------------------------------------------------------------

@router.get("/{lead_id}/logical-docs/{doc_id}")
async def get_logical_doc(
    lead_id: str,
    doc_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get a single logical document with full extracted data and validation results."""
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    doc = await db.logical_docs.find_one({
        "_id": ObjectId(doc_id),
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
    })
    if not doc:
        _not_found("Logical document")

    return _success(_serialize_logical_doc(doc))


# ---------------------------------------------------------------------------
# POST /leads/{lead_id}/logical-docs/group — group physical files
# ---------------------------------------------------------------------------

@router.post("/{lead_id}/logical-docs/group")
async def group_physical_files(
    lead_id: str,
    body: GroupFilesRequest,
    current_user: CurrentUser = Depends(require_role("TENANT_ADMIN", "CAMPAIGN_MANAGER")),
):
    """
    Group multiple physical files (e.g. pages 1-6 and 7-12) into a single logical doc.
    The ops team then confirms the doc_type via the review endpoint.
    """
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    file_ids = body.physical_file_ids
    now = datetime.now(timezone.utc)

    # Verify all files belong to this lead
    count = await db.phys_files.count_documents({
        "_id": {"$in": [ObjectId(fid) for fid in file_ids]},
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
    })
    if count != len(file_ids):
        raise HTTPException(status_code=400, detail="One or more files not found for this lead")

    # Create a grouped logical doc (type TBD by ops review)
    result = await db.logical_docs.insert_one({
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
        "doc_type": "OTHER",
        "assembly_type": "MULTI_FILE",
        "physical_file_ids": file_ids,
        "completeness_status": "COMPLETE",
        "is_mandatory": True,
        "extracted_data": {},
        "status": "NEEDS_HUMAN_REVIEW",
        "created_at": now,
        "updated_at": now,
    })
    logical_doc_id = str(result.inserted_id)

    # Link all phys_files to this logical_doc
    await db.phys_files.update_many(
        {"_id": {"$in": [ObjectId(fid) for fid in file_ids]}},
        {"$addToSet": {"logical_doc_ids": logical_doc_id}, "$set": {"updated_at": now}},
    )

    return _success({"logical_doc_id": logical_doc_id}, "Files grouped — please confirm doc type via review")


# ---------------------------------------------------------------------------
# POST /leads/{lead_id}/logical-docs/{doc_id}/reject
# ---------------------------------------------------------------------------

@router.post("/{lead_id}/logical-docs/{doc_id}/reject")
async def reject_logical_doc(
    lead_id: str,
    doc_id: str,
    body: RejectDocRequest,
    current_user: CurrentUser = Depends(require_role("TENANT_ADMIN", "CAMPAIGN_MANAGER")),
):
    """Reject a logical document (e.g. unreadable, wrong document submitted)."""
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    doc = await db.logical_docs.find_one({
        "_id": ObjectId(doc_id),
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
    })
    if not doc:
        _not_found("Logical document")

    now = datetime.now(timezone.utc)
    await db.logical_docs.update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": {
            "status": "REJECTED",
            "rejection_reason": body.reason,
            "updated_at": now,
        }},
    )

    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": "DOCUMENT_REJECTED",
        "message": f"Document {doc.get('doc_type', 'UNKNOWN')} rejected: {body.reason[:100]}",
        "created_by": current_user.user_id,
        "created_at": now,
    })

    return _success(None, "Document rejected")


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}/download-zip — download all approved docs as ZIP
# ---------------------------------------------------------------------------

@router.get("/{lead_id}/download-zip")
async def download_documents_zip(
    lead_id: str,
    filter: str = Query(default="verified", description="Filter: verified, failed, or all"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Stream logical docs for this lead as a categorised ZIP.
    filter=verified (default) — only Tier1-passed / human-reviewed docs.
    filter=failed  — only Tier1-failed docs.
    filter=all     — every received doc regardless of status.
    """
    db = get_db()
    lead = await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    # Build status filter based on query param
    if filter == "failed":
        status_filter = {"$in": ["TIER1_FAILED", "REJECTED"]}
    elif filter == "all":
        status_filter = {"$exists": True}
    else:
        status_filter = {"$in": ["TIER1_PASSED", "HUMAN_REVIEWED"]}

    query = {
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
    }
    if filter != "all":
        query["status"] = status_filter

    # Collect logical docs
    files_to_zip = []
    async for doc in db.logical_docs.find(query):
        phys_file_ids = doc.get("physical_file_ids", [])
        for fid in phys_file_ids:
            pf = await db.phys_files.find_one({"_id": ObjectId(fid)})
            if pf:
                files_to_zip.append({
                    "s3_key": pf["s3_key"],
                    "doc_type": doc.get("doc_type", "OTHER"),
                    "filename": pf.get("original_filename", "document.pdf"),
                })

    if not files_to_zip:
        raise HTTPException(status_code=404, detail="No documents to download for the selected filter")

    from app.services.storage_service import stream_zip_from_s3_files
    zip_bytes = stream_zip_from_s3_files(files_to_zip)

    company_safe = (lead.get("company_name", "lead") or "lead").replace(" ", "_")[:30]
    filename = f"{company_safe}_documents.zip"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# POST /leads/{lead_id}/upload — get presigned PUT URL for browser direct upload
# ---------------------------------------------------------------------------

@router.post("/{lead_id}/upload")
async def get_upload_url(
    lead_id: str,
    filename: str = Query(..., description="Original filename of the file to upload"),
    content_type: str = Query(default="application/pdf"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Return a presigned S3 PUT URL so the browser can upload a document directly.
    After upload, call POST /leads/{id}/physical-files/confirm to register the file.
    """
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    from app.services.storage_service import build_s3_key, get_upload_presigned_url
    s3_key = build_s3_key(current_user.tenant_id, lead_id, filename)
    upload_url = await get_upload_presigned_url(s3_key, content_type)

    return _success({
        "upload_url": upload_url,
        "s3_key": s3_key,
        "filename": filename,
        "expires_in_seconds": 3600,
    })


# ---------------------------------------------------------------------------
# POST /leads/{lead_id}/upload-direct — upload file via backend (no CORS issues)
# ---------------------------------------------------------------------------

@router.post("/{lead_id}/upload-direct")
async def upload_direct(
    lead_id: str,
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Upload a file directly through the backend to S3, then trigger the
    doc processing pipeline. Avoids S3 CORS issues with presigned URLs.
    """
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    file_bytes = await file.read()
    filename = file.filename or f"document_{int(time.time())}.pdf"
    content_type = file.content_type or "application/octet-stream"

    from app.services.storage_service import upload_file, build_s3_key
    s3_key = build_s3_key(current_user.tenant_id, lead_id, filename)
    await upload_file(file_bytes, s3_key, content_type)
    logger.info(f"[PORTAL UPLOAD] Direct upload to S3: {s3_key} size={len(file_bytes)}")

    now = datetime.now(timezone.utc)
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "other"
    file_type_map = {"pdf": "PDF", "jpg": "JPG", "jpeg": "JPG", "png": "PNG", "zip": "ZIP"}
    file_type = file_type_map.get(ext, "OTHER")
    is_zip = file_type == "ZIP"

    result = await db.phys_files.insert_one({
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
        "original_filename": filename,
        "channel_received": "PORTAL_UPLOAD",
        "s3_key": s3_key,
        "file_type": file_type,
        "file_size_bytes": len(file_bytes),
        "status": "EXTRACTING_ZIP" if is_zip else "RECEIVED",
        "logical_doc_ids": [],
        "created_at": now,
        "updated_at": now,
    })
    phys_file_id = str(result.inserted_id)

    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": "DOCUMENT_UPLOADED",
        "message": f"Document uploaded via portal: {filename}",
        "created_by": current_user.user_id,
        "created_at": now,
    })

    if not is_zip:
        from app.routes.webhooks import _run_doc_processing_pipeline
        try:
            await _run_doc_processing_pipeline(
                file_s3_key=s3_key,
                lead_id=lead_id,
                tenant_id=current_user.tenant_id,
                original_filename=filename,
                file_size_bytes=len(file_bytes),
                waha_message_id="",
                channel="PORTAL_UPLOAD",
            )
            logger.info(f"[PORTAL UPLOAD] Pipeline completed for {filename} lead_id={lead_id}")
        except Exception as exc:
            logger.error(f"[PORTAL UPLOAD] Pipeline failed for {filename}: {exc}", exc_info=True)
    else:
        logger.info(f"[PORTAL UPLOAD] ZIP file uploaded — manual processing needed: {filename}")

    return _success({"phys_file_id": phys_file_id, "status": "processing"}, "File uploaded and processing")


# ---------------------------------------------------------------------------
# POST /leads/{lead_id}/physical-files/confirm — register a browser-uploaded file
# ---------------------------------------------------------------------------

@router.post("/{lead_id}/physical-files/confirm")
async def confirm_portal_upload(
    lead_id: str,
    s3_key: str = Query(...),
    filename: str = Query(...),
    file_size_bytes: int = Query(default=0),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    After the browser has uploaded directly to S3 via presigned URL,
    call this to register the file as a PhysicalFile record and trigger classification.
    """
    db = get_db()
    await _get_lead_or_404(db, lead_id, current_user.tenant_id)

    now = datetime.now(timezone.utc)
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "other"
    file_type_map = {"pdf": "PDF", "jpg": "JPG", "jpeg": "JPG", "png": "PNG", "zip": "ZIP"}
    file_type = file_type_map.get(ext, "OTHER")
    is_zip = file_type == "ZIP"

    result = await db.phys_files.insert_one({
        "lead_id": lead_id,
        "tenant_id": current_user.tenant_id,
        "original_filename": filename,
        "channel_received": "PORTAL_UPLOAD",
        "s3_key": s3_key,
        "file_type": file_type,
        "file_size_bytes": file_size_bytes,
        "status": "EXTRACTING_ZIP" if is_zip else "RECEIVED",
        "logical_doc_ids": [],
        "created_at": now,
        "updated_at": now,
    })
    phys_file_id = str(result.inserted_id)

    await db.activity_feed.insert_one({
        "tenant_id": current_user.tenant_id,
        "lead_id": lead_id,
        "event_type": "DOCUMENT_UPLOADED",
        "message": f"Document uploaded via portal: {filename}",
        "created_by": current_user.user_id,
        "created_at": now,
    })

    # Run doc processing pipeline directly (Celery/SQS may not be available)
    if not is_zip:
        from app.routes.webhooks import _run_doc_processing_pipeline
        try:
            await _run_doc_processing_pipeline(
                file_s3_key=s3_key,
                lead_id=lead_id,
                tenant_id=current_user.tenant_id,
                original_filename=filename,
                file_size_bytes=file_size_bytes,
                waha_message_id="",
                channel="PORTAL_UPLOAD",
            )
            logger.info(f"[PORTAL UPLOAD] Pipeline completed for {filename} lead_id={lead_id}")
        except Exception as exc:
            logger.error(f"[PORTAL UPLOAD] Pipeline failed for {filename}: {exc}", exc_info=True)
    else:
        logger.info(f"[PORTAL UPLOAD] ZIP file uploaded — manual processing needed: {filename}")

    return _success({"phys_file_id": phys_file_id, "status": "processing"}, "File registered and processing")
