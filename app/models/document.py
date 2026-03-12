from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime
from enum import Enum


class DocType(str, Enum):
    AADHAAR = "AADHAAR"
    PAN_CARD = "PAN_CARD"
    BANK_STATEMENT = "BANK_STATEMENT"
    ITR = "ITR"
    GST_CERT = "GST_CERT"
    AUDITED_PL = "AUDITED_PL"
    TITLE_DEED = "TITLE_DEED"
    PROPERTY_TAX = "PROPERTY_TAX"
    NOC = "NOC"
    UDYAM = "UDYAM"
    MOA = "MOA"
    AOA = "AOA"
    COI = "COI"
    ELECTRICITY_BILL = "ELECTRICITY_BILL"
    PASSPORT_PHOTO = "PASSPORT_PHOTO"
    GST_RETURN = "GST_RETURN"
    PARTNERSHIP_DEED = "PARTNERSHIP_DEED"
    OTHER = "OTHER"


class ChannelReceived(str, Enum):
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    PORTAL_UPLOAD = "PORTAL_UPLOAD"
    ZIP_EXTRACTED = "ZIP_EXTRACTED"


class FileType(str, Enum):
    PDF = "PDF"
    JPG = "JPG"
    PNG = "PNG"
    ZIP = "ZIP"
    OTHER = "OTHER"


class AmbiguityType(str, Enum):
    NORMAL = "NORMAL"          # One file → one doc type
    BUNDLED = "BUNDLED"        # One file → multiple doc types
    PARTIAL = "PARTIAL"        # This file is one part of a multi-file document


class PhysFileStatus(str, Enum):
    RECEIVED = "RECEIVED"
    EXTRACTING_ZIP = "EXTRACTING_ZIP"
    CLASSIFYING = "CLASSIFYING"
    CLASSIFIED = "CLASSIFIED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    HUMAN_REVIEWED = "HUMAN_REVIEWED"
    PROCESSED = "PROCESSED"


class LogicalDocStatus(str, Enum):
    PENDING = "PENDING"
    ASSEMBLING = "ASSEMBLING"
    READY_FOR_EXTRACTION = "READY_FOR_EXTRACTION"
    EXTRACTING = "EXTRACTING"
    EXTRACTED = "EXTRACTED"
    TIER1_VALIDATING = "TIER1_VALIDATING"
    TIER1_PASSED = "TIER1_PASSED"
    TIER1_FAILED = "TIER1_FAILED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    HUMAN_REVIEWED = "HUMAN_REVIEWED"
    REJECTED = "REJECTED"


class AssemblyType(str, Enum):
    SINGLE = "SINGLE"          # One physical file → one logical doc
    MULTI_FILE = "MULTI_FILE"  # Multiple physical files assembled into one logical doc
    EXTRACTED = "EXTRACTED"    # Logical doc extracted from a bundled physical file


class CompletenessStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Physical File
# ---------------------------------------------------------------------------

class PhysicalFileResponse(BaseModel):
    id: str
    lead_id: str
    tenant_id: str
    original_filename: str
    channel_received: ChannelReceived
    parent_zip_id: Optional[str] = None
    s3_key: str
    file_type: FileType
    file_size_bytes: Optional[int] = None
    ambiguity_type: Optional[AmbiguityType] = None
    status: PhysFileStatus
    classification_confidence: Optional[float] = None
    classification_reasoning: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_notes: Optional[str] = None
    logical_doc_ids: list[str] = []
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Logical Document
# ---------------------------------------------------------------------------

class Tier1RuleResult(BaseModel):
    rule_id: str
    rule_name: str
    passed: bool
    message: str


class Tier1Validation(BaseModel):
    passed: bool
    rule_results: list[Tier1RuleResult] = []


class LogicalDocResponse(BaseModel):
    id: str
    lead_id: str
    tenant_id: str
    doc_type: DocType
    assembly_type: AssemblyType
    physical_file_ids: list[str] = []
    completeness_status: CompletenessStatus
    period_covered: Optional[dict[str, Any]] = None
    is_mandatory: bool = True
    extracted_data: dict[str, Any] = {}
    tier1_validation: Optional[Tier1Validation] = None
    tier1_validated_at: Optional[datetime] = None
    status: LogicalDocStatus
    rejection_reason: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Document review request models
# ---------------------------------------------------------------------------

class ReviewDecision(str, Enum):
    CONFIRM_SINGLE = "CONFIRM_SINGLE"
    SPLIT_BUNDLED = "SPLIT_BUNDLED"
    MARK_PARTIAL = "MARK_PARTIAL"


class SplitDefinition(BaseModel):
    doc_type: DocType
    page_range: str  # e.g. "1-4"


class ReviewSubmitRequest(BaseModel):
    decision: ReviewDecision
    doc_type: Optional[DocType] = None       # For CONFIRM_SINGLE
    splits: Optional[list[SplitDefinition]] = None  # For SPLIT_BUNDLED
    notes: Optional[str] = None


class GroupFilesRequest(BaseModel):
    physical_file_ids: list[str] = Field(..., min_length=2)


class RejectDocRequest(BaseModel):
    reason: str
