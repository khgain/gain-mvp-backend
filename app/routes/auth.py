from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, EmailStr
from datetime import datetime, timezone

from app.auth import (
    verify_password,
    create_access_token,
    create_refresh_token,
    validate_and_rotate_refresh_token,
    revoke_refresh_tokens_for_user,
    get_current_user,
    CurrentUser,
)
from app.database import get_db
from app.utils.logging import get_logger

router = APIRouter(prefix="/auth", tags=["Authentication"])
logger = get_logger("routes.auth")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str


def _success(data=None, message="Success"):
    return {"success": True, "data": data, "message": message}


@router.post("/login")
async def login(body: LoginRequest):
    db = get_db()

    user = await db.users.find_one({"email": body.email.lower()})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Contact your administrator.",
        )

    user_id = str(user["_id"])
    tenant_id = str(user["tenant_id"])
    role = user["role"]

    access_token = create_access_token(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=user["email"],
    )
    refresh_token = await create_refresh_token(user_id, tenant_id)

    logger.info(f"Login successful — user_id={user_id} role={role}")

    from app.config import settings
    return _success(
        data=LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.JWT_EXPIRE_HOURS * 3600,
            user={
                "id": user_id,
                "name": user["name"],
                "email": user["email"],
                "role": role,
                "tenant_id": tenant_id,
            },
        ).model_dump(),
        message="Login successful",
    )


@router.post("/refresh")
async def refresh_token(body: RefreshRequest):
    record = await validate_and_rotate_refresh_token(body.refresh_token)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is invalid or expired",
        )

    db = get_db()
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(record["user_id"])})
    if not user or not user.get("is_active", True):
        raise HTTPException(status_code=401, detail="User not found or deactivated")

    user_id = str(user["_id"])
    tenant_id = str(user["tenant_id"])

    new_access = create_access_token(
        user_id=user_id,
        tenant_id=tenant_id,
        role=user["role"],
        email=user["email"],
    )
    new_refresh = await create_refresh_token(user_id, tenant_id)

    from app.config import settings
    return _success(
        data={
            "access_token": new_access,
            "refresh_token": new_refresh,
            "token_type": "bearer",
            "expires_in": settings.JWT_EXPIRE_HOURS * 3600,
        },
        message="Token refreshed",
    )


@router.post("/logout")
async def logout(current_user: CurrentUser = Depends(get_current_user)):
    await revoke_refresh_tokens_for_user(current_user.user_id)
    logger.info(f"User logged out — user_id={current_user.user_id}")
    return _success(message="Logged out successfully")
