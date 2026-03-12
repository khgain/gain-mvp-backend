from pydantic import BaseModel, Field, EmailStr
from typing import Optional, Any
from datetime import datetime
from enum import Enum


class TenantType(str, Enum):
    BANK = "BANK"
    NBFC = "NBFC"
    FINTECH = "FINTECH"
    DSA = "DSA"


class UserRole(str, Enum):
    TENANT_ADMIN = "TENANT_ADMIN"
    CAMPAIGN_MANAGER = "CAMPAIGN_MANAGER"
    SALES_AGENT = "SALES_AGENT"
    GAIN_SUPER_ADMIN = "GAIN_SUPER_ADMIN"


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------

class TenantCreate(BaseModel):
    name: str
    type: TenantType
    products: list[str] = []
    config: dict[str, Any] = {}


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[TenantType] = None
    products: Optional[list[str]] = None
    config: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None


class TenantResponse(BaseModel):
    id: str
    name: str
    type: TenantType
    products: list[str]
    config: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    tenant_id: str
    name: str
    email: EmailStr
    phone: Optional[str] = None
    role: UserRole
    password: str


class UserUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None


class UserResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    email: str
    phone: Optional[str] = None
    role: UserRole
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
