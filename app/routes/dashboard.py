"""
Dashboard & Monitor endpoints.
All queries are scoped to current_user.tenant_id.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query, Request

from app.auth import get_current_user, CurrentUser
from app.database import get_db
from app.utils.logging import get_logger

router = APIRouter(tags=["Dashboard & Monitor"])
logger = get_logger("routes.dashboard")

TERMINAL_STATUSES = {"READY_FOR_UNDERWRITING", "DROPPED", "NOT_QUALIFIED"}
ACTIVE_STATUSES = {
    "NEW", "PAN_VERIFIED", "CALL_SCHEDULED", "CALL_COMPLETED",
    "QUALIFIED", "DOC_COLLECTION", "DOCS_COMPLETE",
    "VALIDATION_IN_PROGRESS", "TIER1_ISSUES", "TIER2_ISSUES",
}


def _success(data=None, message="Success"):
    return {"success": True, "data": data, "message": message}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard/stats")
async def get_dashboard_stats(current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    tid = current_user.tenant_id

    # Run counts in parallel using MongoDB aggregation
    pipeline = [
        {"$match": {"tenant_id": tid}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    status_counts_cursor = db.leads.aggregate(pipeline)
    status_counts: dict[str, int] = {}
    async for doc in status_counts_cursor:
        status_counts[doc["_id"]] = doc["count"]

    active_leads = sum(status_counts.get(s, 0) for s in ACTIVE_STATUSES)
    total_leads = sum(status_counts.values())

    active_campaigns = await db.campaigns.count_documents(
        {"tenant_id": tid, "status": "ACTIVE"}
    )

    # Doc completion rate: leads in DOC_COLLECTION that have at least one doc
    doc_collection_leads = status_counts.get("DOC_COLLECTION", 0)
    leads_with_docs = await db.phys_files.distinct(
        "lead_id", {"tenant_id": tid}
    )
    doc_completion_rate = (
        round(len(leads_with_docs) / doc_collection_leads * 100, 1)
        if doc_collection_leads > 0
        else 0
    )

    # SLA breaches: leads in DOC_COLLECTION with no activity for > 7 days
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    sla_breaches = await db.leads.count_documents(
        {
            "tenant_id": tid,
            "status": "DOC_COLLECTION",
            "updated_at": {"$lt": seven_days_ago},
        }
    )

    return _success(
        data={
            "active_campaigns": active_campaigns,
            "total_leads": total_leads,
            "active_leads": active_leads,
            "leads_in_flight": active_leads,
            "ready_for_underwriting": status_counts.get("READY_FOR_UNDERWRITING", 0),
            "doc_completion_rate": doc_completion_rate,
            "sla_breaches": sla_breaches,
            "status_breakdown": status_counts,
        }
    )


@router.get("/dashboard/activity-feed")
async def get_activity_feed(
    limit: int = Query(default=50, ge=1, le=200),
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    events = await db.activity_feed.find(
        {"tenant_id": current_user.tenant_id}
    ).sort("created_at", -1).limit(limit).to_list(limit)

    serialized = [
        {
            "id": str(e["_id"]),
            "lead_id": e.get("lead_id"),
            "event_type": e.get("event_type"),
            "message": e.get("message"),
            "created_by": e.get("created_by"),
            "created_at": e.get("created_at"),
        }
        for e in events
    ]

    return _success(data={"events": serialized, "total": len(serialized)})


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

@router.get("/monitor/leads")
async def monitor_leads(
    campaign_id: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    query: dict = {"tenant_id": current_user.tenant_id}
    if campaign_id:
        query["campaign_id"] = campaign_id

    total = await db.leads.count_documents(query)
    skip = (page - 1) * page_size
    docs = await db.leads.find(query).sort("updated_at", -1).skip(skip).limit(page_size).to_list(page_size)

    leads = [
        {
            "id": str(d["_id"]),
            "name": d.get("name"),
            "company_name": d.get("company_name"),
            "status": d.get("status"),
            "campaign_id": str(d["campaign_id"]) if d.get("campaign_id") else None,
            "assigned_to": str(d["assigned_to"]) if d.get("assigned_to") else None,
            "loan_type": d.get("loan_type"),
            "entity_type": d.get("entity_type"),
            "updated_at": d.get("updated_at"),
            "created_at": d.get("created_at"),
        }
        for d in docs
    ]

    return _success(data={"leads": leads, "total": total, "page": page, "page_size": page_size})


@router.get("/monitor/campaign-health")
async def campaign_health(current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    tid = current_user.tenant_id

    campaigns = await db.campaigns.find({"tenant_id": tid}).to_list(100)

    health = []
    for campaign in campaigns:
        cid = str(campaign["_id"])
        pipeline = [
            {"$match": {"tenant_id": tid, "campaign_id": cid}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
        status_counts: dict[str, int] = {}
        async for doc in db.leads.aggregate(pipeline):
            status_counts[doc["_id"]] = doc["count"]

        total = sum(status_counts.values())
        health.append(
            {
                "campaign_id": cid,
                "campaign_name": campaign.get("name"),
                "status": campaign.get("status"),
                "use_case": campaign.get("use_case"),
                "total_leads": total,
                "active_leads": sum(status_counts.get(s, 0) for s in ACTIVE_STATUSES),
                "ready_for_underwriting": status_counts.get("READY_FOR_UNDERWRITING", 0),
                "dropped": status_counts.get("DROPPED", 0),
                "status_breakdown": status_counts,
            }
        )

    return _success(data={"campaigns": health})


# ---------------------------------------------------------------------------
# Global Document Review Queue (cross-lead, tenant-scoped)
# ---------------------------------------------------------------------------

@router.get("/documents/review-queue")
async def get_review_queue(
    campaign_id: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    GET /documents/review-queue
    Returns all physical files across all leads that need human review
    (status = NEEDS_HUMAN_REVIEW), scoped to the tenant.
    Frontend calls this for the Review Queue page.
    """
    db = get_db()
    tid = current_user.tenant_id

    query: dict = {"tenant_id": tid, "status": "NEEDS_HUMAN_REVIEW"}
    if campaign_id:
        # Filter by leads in this campaign
        lead_ids_in_campaign = await db.leads.distinct(
            "_id", {"tenant_id": tid, "campaign_id": campaign_id}
        )
        query["lead_id"] = {"$in": [str(lid) for lid in lead_ids_in_campaign]}

    total = await db.phys_files.count_documents(query)
    skip = (page - 1) * page_size
    files = await db.phys_files.find(query).sort("created_at", -1).skip(skip).limit(page_size).to_list(page_size)

    result = []
    for f in files:
        lead_id = f.get("lead_id")
        lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tid}) if lead_id else None
        campaign = None
        if lead and lead.get("campaign_id"):
            campaign = await db.campaigns.find_one({"_id": ObjectId(str(lead["campaign_id"])), "tenant_id": tid})
        # Format timestamp with Z suffix
        created = f.get("created_at")
        ts_str = ""
        if hasattr(created, "isoformat"):
            ts_str = created.isoformat()
            if not ts_str.endswith("Z") and "+" not in ts_str:
                ts_str += "Z"
        else:
            ts_str = str(created or "")

        # Extract AI guess from classification_reasoning
        reasoning_obj = f.get("classification_reasoning")
        ai_guess = None
        ai_reasoning = None
        if isinstance(reasoning_obj, dict):
            ai_guess = reasoning_obj.get("doc_type")
            ai_reasoning = reasoning_obj.get("reasoning") or reasoning_obj.get("explanation")
        elif isinstance(reasoning_obj, str):
            ai_reasoning = reasoning_obj

        # Get lead-specific checklist doc names
        checklist_docs = []
        if lead:
            try:
                from app.services.validation_rules import get_doc_collection_config
                config = await get_doc_collection_config(db, tid)
                entity_type = lead.get("entity_type", "INDIVIDUAL")
                entity_checklist = config.get("doc_checklist_by_entity_type", {}).get(
                    entity_type, config.get("doc_checklist_by_entity_type", {}).get("INDIVIDUAL", {})
                )
                for doc_name in entity_checklist.get("required", []):
                    checklist_docs.append(doc_name)
                for doc_name in entity_checklist.get("optional", []):
                    checklist_docs.append(doc_name)
            except Exception:
                pass

        result.append({
            "id": str(f["_id"]),
            "fileId": str(f["_id"]),
            "leadId": lead_id,
            "borrower": lead.get("name") if lead else None,
            "company": lead.get("company_name") if lead else None,
            "campaignName": campaign.get("name") if campaign else None,
            "fileName": f.get("original_filename"),
            "channel": f.get("channel_received"),
            "receivedAt": ts_str,
            "aiGuess": ai_guess,
            "confidence": f.get("classification_confidence"),
            "aiReasoning": ai_reasoning,
            "fileType": f.get("file_type"),
            "s3Key": f.get("s3_key"),
            "checklistDocs": checklist_docs,
        })

    return _success(data=result, message=f"{total} files pending review")


@router.get("/documents/review-queue/stats")
async def get_review_queue_stats(current_user: CurrentUser = Depends(get_current_user)):
    """GET /documents/review-queue/stats — count of pending review items."""
    db = get_db()
    pending = await db.phys_files.count_documents(
        {"tenant_id": current_user.tenant_id, "status": "NEEDS_HUMAN_REVIEW"}
    )
    return _success(data={"pending": pending})


# ---------------------------------------------------------------------------
# POST /documents/{file_id}/review — convenience endpoint for Review Queue page
# Resolves lead_id from the phys_file automatically
# ---------------------------------------------------------------------------

@router.post("/documents/{file_id}/review")
async def queue_review_file(
    file_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Convenience wrapper for the review queue page.
    Accepts {decision, doc_type, notes} without requiring lead_id in the URL.
    """
    from app.models.document import ReviewDecision, DocType
    body = await request.json()
    db = get_db()

    phys_file = await db.phys_files.find_one({
        "_id": ObjectId(file_id),
        "tenant_id": current_user.tenant_id,
    })
    if not phys_file:
        return {"success": False, "message": "File not found"}

    lead_id = phys_file.get("lead_id")
    now = datetime.now(timezone.utc)
    decision = body.get("decision", "")
    doc_type_str = body.get("doc_type", "")
    notes = body.get("notes", "")

    if decision == "CONFIRM_SINGLE":
        if not doc_type_str:
            return {"success": False, "message": "doc_type required for CONFIRM_SINGLE"}

        # Normalise doc_type string to enum value
        dt_upper = doc_type_str.upper().replace(" ", "_").replace("-", "_")
        # Try matching DocType enum
        try:
            doc_type_enum = DocType(dt_upper)
        except ValueError:
            doc_type_enum = DocType.OTHER

        result = await db.logical_docs.insert_one({
            "lead_id": lead_id,
            "tenant_id": current_user.tenant_id,
            "doc_type": doc_type_enum.value,
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

        await db.phys_files.update_one(
            {"_id": ObjectId(file_id)},
            {"$set": {
                "status": "HUMAN_REVIEWED",
                "ambiguity_type": "NORMAL",
                "reviewer_id": current_user.id,
                "reviewed_at": now,
                "review_notes": notes,
                "updated_at": now,
            }, "$addToSet": {"logical_doc_ids": logical_doc_id}},
        )

        # Activity feed
        await db.activity_feed.insert_one({
            "lead_id": lead_id,
            "tenant_id": current_user.tenant_id,
            "event_type": "DOCUMENT_RECEIVED",
            "message": f"Document '{phys_file.get('original_filename', 'unknown')}' classified as {doc_type_enum.value.replace('_', ' ').title()} by reviewer"
                       + (f" (note: {notes})" if notes else ""),
            "created_at": now,
            "created_by": current_user.id,
        })

        # Enqueue extraction (non-fatal)
        try:
            from app.workers.ai_worker import extract_document_data
            extract_document_data.apply_async(
                kwargs={"logical_doc_id": logical_doc_id, "tenant_id": current_user.tenant_id},
                queue="ai-extraction",
            )
        except Exception as exc:
            logger.warning(f"Failed to enqueue extraction for {logical_doc_id}: {exc}")

        return _success({"decision": "CONFIRM_SINGLE", "logical_doc_id": logical_doc_id}, "Document classified")

    elif decision == "REJECT":
        await db.phys_files.update_one(
            {"_id": ObjectId(file_id)},
            {"$set": {
                "status": "HUMAN_REVIEWED",
                "ambiguity_type": "NORMAL",
                "reviewer_id": current_user.id,
                "reviewed_at": now,
                "review_notes": notes or "Rejected by reviewer",
                "updated_at": now,
            }},
        )
        await db.activity_feed.insert_one({
            "lead_id": lead_id,
            "tenant_id": current_user.tenant_id,
            "event_type": "DOCUMENT_REJECTED",
            "message": f"Document '{phys_file.get('original_filename', 'unknown')}' rejected by reviewer"
                       + (f": {notes}" if notes else ""),
            "created_at": now,
            "created_by": current_user.id,
        })
        return _success({"decision": "REJECT"}, "Document rejected")

    return {"success": False, "message": f"Unknown decision: {decision}"}


# ---------------------------------------------------------------------------
# Notifications (activity_feed formatted as notifications for header bell)
# ---------------------------------------------------------------------------

@router.get("/notifications")
async def get_notifications(
    limit: int = Query(default=10, ge=1, le=50),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    GET /notifications
    Returns latest activity feed items formatted as notifications for the header bell.
    """
    db = get_db()
    events = await db.activity_feed.find(
        {"tenant_id": current_user.tenant_id}
    ).sort("created_at", -1).limit(limit).to_list(limit)

    type_map = {
        "CALL_COMPLETED": "success",
        "CALL_FAILED": "error",
        "CALL_NO_ANSWER": "warning",
        "EMAIL_SENT": "info",
        "WHATSAPP_SENT": "info",
        "DOC_RECEIVED": "info",
        "DOC_CLASSIFIED": "info",
        "DOC_NEEDS_REVIEW": "warning",
        "TIER1_FAILED": "error",
        "TIER2_FAILED": "error",
        "READY_FOR_UNDERWRITING": "success",
    }

    notifications = []
    for e in events:
        event_type = e.get("event_type", "info")
        notifications.append({
            "id": str(e["_id"]),
            "type": type_map.get(event_type, "info"),
            "message": e.get("message", ""),
            "timestamp": e.get("created_at"),
            "lead_id": e.get("lead_id"),
            "link": f"/leads/{e.get('lead_id')}" if e.get("lead_id") else None,
        })

    return _success(data=notifications, message=f"{len(notifications)} notifications")
