"""
Tenant management — Gain Super Admin only.
Regular tenant users cannot access these endpoints.
"""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import require_super_admin, CurrentUser
from app.database import get_db
from app.models.tenant import TenantCreate, TenantUpdate, UserCreate
from app.auth import hash_password
from app.utils.logging import get_logger

router = APIRouter(prefix="/tenants", tags=["Tenants (Super Admin)"])
logger = get_logger("routes.tenants")


def _success(data=None, message="Success"):
    return {"success": True, "data": data, "message": message}


def _serialize(doc: dict) -> dict:
    r = {**doc}
    r["id"] = str(doc["_id"])
    r.pop("_id", None)
    return r


@router.get("")
async def list_tenants(current_user: CurrentUser = Depends(require_super_admin)):
    db = get_db()
    docs = await db.tenants.find({}).sort("created_at", -1).to_list(500)
    return _success(data=[_serialize(d) for d in docs], message=f"{len(docs)} tenants")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    current_user: CurrentUser = Depends(require_super_admin),
):
    db = get_db()
    now = datetime.now(timezone.utc)
    doc = {
        "name": body.name,
        "type": body.type,
        "products": body.products,
        "config": body.config,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.tenants.insert_one(doc)
    created = await db.tenants.find_one({"_id": result.inserted_id})
    logger.info(f"Tenant created — id={result.inserted_id} name={body.name}")
    return _success(data=_serialize(created), message=f"Tenant '{body.name}' created")


@router.get("/{tenant_id}")
async def get_tenant(tenant_id: str, current_user: CurrentUser = Depends(require_super_admin)):
    db = get_db()
    try:
        oid = ObjectId(tenant_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid tenant ID")
    doc = await db.tenants.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _success(data=_serialize(doc))


@router.patch("/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    current_user: CurrentUser = Depends(require_super_admin),
):
    db = get_db()
    try:
        oid = ObjectId(tenant_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid tenant ID")
    doc = await db.tenants.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Tenant not found")

    updates = body.model_dump(exclude_none=True)
    updates["updated_at"] = datetime.now(timezone.utc)
    await db.tenants.update_one({"_id": oid}, {"$set": updates})
    updated = await db.tenants.find_one({"_id": oid})
    return _success(data=_serialize(updated), message="Tenant updated")


# ---------------------------------------------------------------------------
# User management within a tenant (admin can add team members)
# ---------------------------------------------------------------------------

@router.post("/{tenant_id}/users", status_code=status.HTTP_201_CREATED)
async def create_user_in_tenant(
    tenant_id: str,
    body: UserCreate,
    current_user: CurrentUser = Depends(require_super_admin),
):
    db = get_db()
    existing = await db.users.find_one({"email": body.email.lower()})
    if existing:
        raise HTTPException(status_code=400, detail="A user with this email already exists")

    now = datetime.now(timezone.utc)
    doc = {
        "tenant_id": tenant_id,
        "name": body.name,
        "email": body.email.lower(),
        "phone": body.phone,
        "role": body.role,
        "password_hash": hash_password(body.password),
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.users.insert_one(doc)
    logger.info(f"User created — id={result.inserted_id} email={body.email} role={body.role}")
    return _success(
        data={"id": str(result.inserted_id), "email": body.email, "role": body.role},
        message=f"User created for tenant {tenant_id}",
    )
