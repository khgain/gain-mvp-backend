from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Optional

from app.auth import get_current_user, CurrentUser
from app.database import get_db
from app.models.agent import AgentCreate, AgentUpdate
from app.utils.logging import get_logger

router = APIRouter(prefix="/agents", tags=["Agents"])
logger = get_logger("routes.agents")


def _success(data=None, message="Success"):
    return {"success": True, "data": data, "message": message}


def _serialize(doc: dict) -> dict:
    r = {**doc}
    r["id"] = str(doc["_id"])
    r.pop("_id", None)
    if r.get("tenant_id"):
        r["tenant_id"] = str(r["tenant_id"])
    return r


@router.get("")
async def list_agents(
    type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    query: dict = {"tenant_id": current_user.tenant_id}
    if type:
        query["type"] = type
    if status:
        query["status"] = status

    docs = await db.agents.find(query).sort("created_at", -1).to_list(200)
    return _success(data=[_serialize(d) for d in docs], message=f"{len(docs)} agents")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    now = datetime.now(timezone.utc)
    doc = {
        "tenant_id": current_user.tenant_id,
        "name": body.name,
        "type": body.type,
        "status": "DRAFT",
        "config": body.config,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.agents.insert_one(doc)
    created = await db.agents.find_one({"_id": result.inserted_id})
    logger.info(f"Agent created — id={result.inserted_id} type={body.type}")
    return _success(data=_serialize(created), message="Agent created")


@router.get("/{agent_id}")
async def get_agent(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    try:
        oid = ObjectId(agent_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid agent ID")
    doc = await db.agents.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _success(data=_serialize(doc))


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(agent_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid agent ID")
    doc = await db.agents.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Agent not found")

    updates = body.model_dump(exclude_none=True)
    updates["updated_at"] = datetime.now(timezone.utc)
    await db.agents.update_one({"_id": oid, "tenant_id": current_user.tenant_id}, {"$set": updates})
    updated = await db.agents.find_one({"_id": oid})
    return _success(data=_serialize(updated), message="Agent updated")


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    try:
        oid = ObjectId(agent_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid agent ID")
    doc = await db.agents.find_one({"_id": oid, "tenant_id": current_user.tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Soft delete — check if used in active campaigns first
    active_campaigns_using = await db.campaigns.count_documents(
        {
            "tenant_id": current_user.tenant_id,
            "status": "ACTIVE",
            "assigned_agents": agent_id,
        }
    )
    if active_campaigns_using > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete agent used in {active_campaigns_using} active campaign(s). Pause the campaign(s) first.",
        )

    await db.agents.update_one(
        {"_id": oid, "tenant_id": current_user.tenant_id},
        {"$set": {"status": "INACTIVE", "updated_at": datetime.now(timezone.utc)}},
    )
    logger.info(f"Agent soft-deleted — id={agent_id}")
    return _success(message="Agent deactivated")
