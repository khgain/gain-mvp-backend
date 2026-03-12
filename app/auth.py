"""
JWT authentication — creation, verification, and FastAPI dependency injection.
Passwords are bcrypt-hashed with SHA-256 pre-hashing (handles bcrypt's 72-char limit).
Refresh tokens are stored in MongoDB with a TTL index for auto-expiry.
"""
import hashlib
import base64
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from bson import ObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.config import settings
from app.database import get_db
from app.utils.logging import get_logger

logger = get_logger("auth")
bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """SHA-256 pre-hash then bcrypt. Handles passwords longer than 72 chars."""
    sha256 = hashlib.sha256(password.encode()).digest()
    pre_hashed = base64.b64encode(sha256).decode()
    return bcrypt.hashpw(pre_hashed.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    sha256 = hashlib.sha256(plain.encode()).digest()
    pre_hashed = base64.b64encode(sha256).decode()
    return bcrypt.checkpw(pre_hashed.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# JWT — access tokens
# ---------------------------------------------------------------------------

def create_access_token(
    user_id: str,
    tenant_id: str,
    role: str,
    email: str,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "email": email,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(hours=settings.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------------------
# Refresh tokens — stored in MongoDB
# ---------------------------------------------------------------------------

def _hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def create_refresh_token(user_id: str, tenant_id: str) -> str:
    """Generate a secure random refresh token and persist it to MongoDB."""
    raw = secrets.token_urlsafe(64)
    token_hash = _hash_refresh_token(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS)

    db = get_db()
    await db.refresh_tokens.insert_one(
        {
            "token_hash": token_hash,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc),
        }
    )
    logger.info(f"Refresh token created for user_id={user_id}")
    return raw


async def validate_and_rotate_refresh_token(
    raw_token: str,
) -> Optional[dict]:
    """
    Validate a refresh token and rotate it (delete old, issue new).
    Returns the stored record if valid, None if invalid/expired.
    """
    db = get_db()
    token_hash = _hash_refresh_token(raw_token)
    record = await db.refresh_tokens.find_one({"token_hash": token_hash})

    if not record:
        return None

    # TTL index handles expiry automatically, but double-check
    if record["expires_at"] < datetime.now(timezone.utc):
        await db.refresh_tokens.delete_one({"token_hash": token_hash})
        return None

    # Delete old token (rotation)
    await db.refresh_tokens.delete_one({"token_hash": token_hash})
    return record


async def revoke_refresh_tokens_for_user(user_id: str) -> None:
    """Revoke all refresh tokens for a user (logout all devices)."""
    db = get_db()
    result = await db.refresh_tokens.delete_many({"user_id": user_id})
    logger.info(f"Revoked {result.deleted_count} refresh token(s) for user_id={user_id}")


# ---------------------------------------------------------------------------
# FastAPI dependency — current authenticated user
# ---------------------------------------------------------------------------

class CurrentUser:
    """Injected into route handlers via Depends(get_current_user)."""

    def __init__(self, user_id: str, tenant_id: str, role: str, email: str):
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.role = role
        self.email = email

    @property
    def is_super_admin(self) -> bool:
        return self.role == "GAIN_SUPER_ADMIN"

    @property
    def is_tenant_admin(self) -> bool:
        return self.role in ("TENANT_ADMIN", "GAIN_SUPER_ADMIN")


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    return CurrentUser(
        user_id=payload["sub"],
        tenant_id=payload["tenant_id"],
        role=payload["role"],
        email=payload["email"],
    )


def require_role(*roles: str):
    """Dependency factory — restrict route to specified roles."""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not permitted to access this endpoint",
            )
        return user

    return _check


require_super_admin = require_role("GAIN_SUPER_ADMIN")
require_admin = require_role("TENANT_ADMIN", "GAIN_SUPER_ADMIN")
