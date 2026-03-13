from pydantic import BaseModel, Field, field_validator, EmailStr
from typing import Optional, Any
from datetime import datetime
from enum import Enum
import re


class LoanType(str, Enum):
    TERM_LOAN = "TERM_LOAN"
    LAP = "LAP"
    SCF = "SCF"
    TOP_UP = "TOP_UP"


class EntityType(str, Enum):
    PVT_LTD = "PVT_LTD"
    PRIVATE_LIMITED = "PRIVATE_LIMITED"   # alias used in seed data
    PARTNERSHIP = "PARTNERSHIP"
    PROPRIETORSHIP = "PROPRIETORSHIP"
    INDIVIDUAL = "INDIVIDUAL"


class LeadStatus(str, Enum):
    NEW = "NEW"
    PAN_VERIFIED = "PAN_VERIFIED"
    CALL_SCHEDULED = "CALL_SCHEDULED"
    CALL_COMPLETED = "CALL_COMPLETED"
    QUALIFIED = "QUALIFIED"
    NOT_QUALIFIED = "NOT_QUALIFIED"
    DOC_COLLECTION = "DOC_COLLECTION"
    DOCS_COMPLETE = "DOCS_COMPLETE"
    VALIDATION_IN_PROGRESS = "VALIDATION_IN_PROGRESS"
    TIER1_ISSUES = "TIER1_ISSUES"
    TIER2_ISSUES = "TIER2_ISSUES"
    READY_FOR_UNDERWRITING = "READY_FOR_UNDERWRITING"
    DROPPED = "DROPPED"


class LeadSource(str, Enum):
    DIRECT = "DIRECT"
    CROSS_SELL = "CROSS_SELL"
    TOP_UP = "TOP_UP"
    RENEWAL = "RENEWAL"
    DROP_OFF = "DROP_OFF"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class LeadCreate(BaseModel):
    campaign_id: Optional[str] = None
    name: str = Field(..., min_length=2, max_length=200)
    company_name: Optional[str] = Field(default=None, max_length=300)
    pan: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[EmailStr] = None
    loan_type: Optional[LoanType] = None
    entity_type: Optional[EntityType] = None
    loan_amount_requested: Optional[int] = None  # In paise (INR × 100)
    source: LeadSource = LeadSource.DIRECT
    assigned_to: Optional[str] = None
    metadata: dict[str, Any] = {}

    @field_validator("pan")
    @classmethod
    def validate_pan(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        pan = v.strip().upper()
        if not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan):
            raise ValueError(
                "Invalid PAN format. Expected format: AAAAA9999A (5 letters, 4 digits, 1 letter)"
            )
        return pan

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        digits = re.sub(r"\D", "", v)
        # Accept 10-digit Indian numbers, optionally prefixed with +91 or 91
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        if not (len(digits) == 10 and digits[0] in "6789"):
            raise ValueError("Invalid mobile number. Must be a 10-digit Indian mobile number.")
        return digits


class LeadUpdate(BaseModel):
    campaign_id: Optional[str] = None
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    company_name: Optional[str] = None
    email: Optional[EmailStr] = None
    loan_type: Optional[LoanType] = None
    entity_type: Optional[EntityType] = None
    loan_amount_requested: Optional[int] = None
    status: Optional[LeadStatus] = None
    assigned_to: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class BulkLeadCreate(BaseModel):
    campaign_id: Optional[str] = None
    leads: list[LeadCreate] = Field(..., min_length=1, max_length=500)


class VerifyPANRequest(BaseModel):
    # MVP placeholder — no external API called
    notes: Optional[str] = None


class TriggerAgentRequest(BaseModel):
    agent_id: str
    reason: Optional[str] = None


class OverrideRequest(BaseModel):
    action: str  # FORCE_UNDERWRITING | DROP | CHANGE_FOLLOW_UP_FREQUENCY | advance_to_doc_collection
    reason: Optional[str] = None
    follow_up_frequency_days: Optional[int] = None


class LogActionRequest(BaseModel):
    action_type: str  # e.g. CALL_MADE, NOTE_ADDED, EMAIL_SENT
    notes: str
    outcome: Optional[str] = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class QualificationResult(BaseModel):
    outcome: Optional[str] = None  # QUALIFIED | NOT_QUALIFIED | CALLBACK_REQUESTED
    call_transcript: Optional[str] = None
    key_data: dict[str, Any] = {}
    callback_time: Optional[datetime] = None


class LeadResponse(BaseModel):
    id: str
    tenant_id: str
    campaign_id: Optional[str] = None
    assigned_to: Optional[str] = None
    name: str
    company_name: Optional[str] = None
    # PAN and mobile are returned masked in list views
    pan_masked: Optional[str] = None
    mobile_masked: Optional[str] = None
    email: Optional[str] = None
    loan_type: Optional[LoanType] = None
    entity_type: Optional[EntityType] = None
    loan_amount_requested: Optional[int] = None
    status: LeadStatus
    source: LeadSource
    pan_verified_by: Optional[str] = None
    pan_verified_at: Optional[datetime] = None
    qualification_result: Optional[QualificationResult] = None
    validation_flags: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class LeadListResponse(BaseModel):
    leads: list[LeadResponse]
    total: int
    page: int
    page_size: int
    pages: int
