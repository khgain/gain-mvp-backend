from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user, CurrentUser
from app.database import get_db
from app.models.campaign import CampaignCreate, CampaignUpdate, WORKFLOW_TEMPLATES
from app.utils.logging import get_logger

router = APIRouter(prefix="/campaigns", tags=["Campaigns"])
logger = get_logger("routes.campaigns")


def _success(data=None, message="Success"):
    return {"success": True, "data": data, "message": message}


def _serialize(doc: dict) -> dict:
    r = {**doc}
    r["id"] = str(doc["_id"])
    r.pop("_id", None)
    if r.get("tenant_id"):
        r["tenant_id"] = str(r["tenant_id"])
    return r


@router.get("/templates")
async def get_templates():
    """Pre-built workflow templates — no auth required (for demo)."""
    return _success(data=WORKFLOW_TEMPLATES, message=f"{len(WORKFLOW_TEMPLATES)} templates available")


@router.get("")
async def list_campaigns(current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    docs = await db.campaigns.find({"tenant_id": current_user.tenant_id}).sort("created_at", -1).to_list(200)
    return _success(data=[_serialize(d) for d in docs], message=f"{len(docs)} campaigns")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    now = datetime.now(timezone.utc)
    doc = {
        "tenant_id": current_user.tenant_id,
        "name": body.name,
        "use_case": body.use_case,
        "lender_product": body.lender_product,
        "status": "DRAFT",
        "workflow_graph": body.workflow_graph,
        "assigned_agents": body.assigned_agents,
        "lead_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.campaigns.insert_one(doc)
    created = await db.campaigns.find_one({"_id": result.inserted_id})
    logger.info(f"Campaign created — id={result.inserted_id} tenant={current_user.tenant_id}")
    return _success(data=_serialize(created), message="Campaign created")


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: str, current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid campaign ID")
    doc = await db.campaigns.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return _success(data=_serialize(doc))


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: str,
    body: CampaignUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid campaign ID")
    doc = await db.campaigns.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found")

    updates = body.model_dump(exclude_none=True)
    updates["updated_at"] = datetime.now(timezone.utc)
    await db.campaigns.update_one({"_id": oid, "tenant_id": current_user.tenant_id}, {"$set": updates})
    updated = await db.campaigns.find_one({"_id": oid})
    return _success(data=_serialize(updated), message="Campaign updated")


@router.post("/{campaign_id}/activate")
async def activate_campaign(campaign_id: str, current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid campaign ID")
    doc = await db.campaigns.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if doc.get("status") == "ACTIVE":
        raise HTTPException(status_code=400, detail="Campaign is already active")

    await db.campaigns.update_one(
        {"_id": oid, "tenant_id": current_user.tenant_id},
        {"$set": {"status": "ACTIVE", "updated_at": datetime.now(timezone.utc)}},
    )
    logger.info(f"Campaign activated — id={campaign_id}")
    return _success(message="Campaign activated")


@router.post("/{campaign_id}/pause")
async def pause_campaign(campaign_id: str, current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid campaign ID")
    doc = await db.campaigns.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found")

    await db.campaigns.update_one(
        {"_id": oid, "tenant_id": current_user.tenant_id},
        {"$set": {"status": "PAUSED", "updated_at": datetime.now(timezone.utc)}},
    )
    logger.info(f"Campaign paused — id={campaign_id}")
    return _success(message="Campaign paused")


@router.patch("/{campaign_id}/status")
async def update_campaign_status(
    campaign_id: str,
    body: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    PATCH /campaigns/:id/status — frontend-compatible status toggle.
    Accepts { "status": "ACTIVE" | "PAUSED" | "ARCHIVED" | "DRAFT" }
    Delegates to activate/pause logic.
    """
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid campaign ID")

    doc = await db.campaigns.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found")

    new_status = (body.get("status") or "").upper()
    allowed = {"ACTIVE", "PAUSED", "ARCHIVED", "DRAFT"}
    if new_status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of {allowed}")

    await db.campaigns.update_one(
        {"_id": oid, "tenant_id": current_user.tenant_id},
        {"$set": {"status": new_status, "updated_at": datetime.now(timezone.utc)}},
    )
    updated = await db.campaigns.find_one({"_id": oid})
    logger.info(f"Campaign status updated — id={campaign_id} status={new_status}")
    return _success(data=_serialize(updated), message=f"Campaign {new_status.lower()}")
