from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime
from enum import Enum


class CampaignUseCase(str, Enum):
    LOAN_ONBOARDING = "LOAN_ONBOARDING"
    TOP_UP_OUTREACH = "TOP_UP_OUTREACH"
    RENEWAL = "RENEWAL"
    DROP_OFF_RECOVERY = "DROP_OFF_RECOVERY"
    CUSTOM = "CUSTOM"


class CampaignStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    use_case: CampaignUseCase
    lender_product: Optional[str] = None
    workflow_graph: dict[str, Any] = {}  # ReactFlow JSON
    assigned_agents: list[str] = []


class CampaignUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    use_case: Optional[CampaignUseCase] = None
    lender_product: Optional[str] = None
    workflow_graph: Optional[dict[str, Any]] = None
    assigned_agents: Optional[list[str]] = None
    status: Optional[CampaignStatus] = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class CampaignStats(BaseModel):
    total_leads: int = 0
    active_leads: int = 0
    qualified: int = 0
    ready_for_underwriting: int = 0
    dropped: int = 0


class CampaignResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    use_case: CampaignUseCase
    status: CampaignStatus
    lender_product: Optional[str] = None
    workflow_graph: dict[str, Any] = {}
    assigned_agents: list[str] = []
    lead_count: int = 0
    stats: Optional[CampaignStats] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Workflow graph templates (returned by GET /campaigns/templates)
# ---------------------------------------------------------------------------

LOAN_ONBOARDING_TEMPLATE = {
    "nodes": [
        {"id": "start", "type": "start", "data": {"label": "Lead Created"}, "position": {"x": 0, "y": 0}},
        {"id": "pan_verify", "type": "action", "data": {"label": "PAN Verification", "agent_type": "MANUAL"}, "position": {"x": 0, "y": 100}},
        {"id": "voice_qualify", "type": "action", "data": {"label": "Voice Qualification Call", "agent_type": "VOICE_AI"}, "position": {"x": 0, "y": 200}},
        {"id": "branch_qualified", "type": "branch", "data": {"label": "Qualified?", "condition": "qualification_result.outcome == 'QUALIFIED'"}, "position": {"x": 0, "y": 300}},
        {"id": "doc_collection", "type": "action", "data": {"label": "Document Collection", "agent_type": "DOC_COLLECTION"}, "position": {"x": -100, "y": 400}},
        {"id": "validation", "type": "action", "data": {"label": "AI Validation", "agent_type": "VALIDATION_AI"}, "position": {"x": -100, "y": 500}},
        {"id": "ready", "type": "end", "data": {"label": "Ready for Underwriting"}, "position": {"x": -100, "y": 600}},
        {"id": "not_qualified", "type": "end", "data": {"label": "Not Qualified"}, "position": {"x": 100, "y": 400}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "pan_verify"},
        {"id": "e2", "source": "pan_verify", "target": "voice_qualify"},
        {"id": "e3", "source": "voice_qualify", "target": "branch_qualified"},
        {"id": "e4", "source": "branch_qualified", "target": "doc_collection", "label": "Yes"},
        {"id": "e5", "source": "branch_qualified", "target": "not_qualified", "label": "No"},
        {"id": "e6", "source": "doc_collection", "target": "validation"},
        {"id": "e7", "source": "validation", "target": "ready"},
    ],
}

TOP_UP_TEMPLATE = {
    "nodes": [
        {"id": "start", "type": "start", "data": {"label": "Eligible Borrower"}, "position": {"x": 0, "y": 0}},
        {"id": "pitch_call", "type": "action", "data": {"label": "Top-up Pitch Call", "agent_type": "VOICE_AI"}, "position": {"x": 0, "y": 100}},
        {"id": "branch_interest", "type": "branch", "data": {"label": "Interested?", "condition": "qualification_result.outcome == 'QUALIFIED'"}, "position": {"x": 0, "y": 200}},
        {"id": "min_doc_collection", "type": "action", "data": {"label": "Minimal Doc Collection", "agent_type": "DOC_COLLECTION"}, "position": {"x": -100, "y": 300}},
        {"id": "ready", "type": "end", "data": {"label": "Ready for Underwriting"}, "position": {"x": -100, "y": 400}},
        {"id": "not_interested", "type": "end", "data": {"label": "Not Interested"}, "position": {"x": 100, "y": 300}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "pitch_call"},
        {"id": "e2", "source": "pitch_call", "target": "branch_interest"},
        {"id": "e3", "source": "branch_interest", "target": "min_doc_collection", "label": "Yes"},
        {"id": "e4", "source": "branch_interest", "target": "not_interested", "label": "No"},
        {"id": "e5", "source": "min_doc_collection", "target": "ready"},
    ],
}

DROP_OFF_TEMPLATE = {
    "nodes": [
        {"id": "start", "type": "start", "data": {"label": "Inactive Lead Detected"}, "position": {"x": 0, "y": 0}},
        {"id": "recovery_call", "type": "action", "data": {"label": "Recovery Call", "agent_type": "VOICE_AI"}, "position": {"x": 0, "y": 100}},
        {"id": "branch_revived", "type": "branch", "data": {"label": "Re-engaged?", "condition": "qualification_result.outcome == 'QUALIFIED'"}, "position": {"x": 0, "y": 200}},
        {"id": "resume_docs", "type": "action", "data": {"label": "Resume Doc Collection", "agent_type": "DOC_COLLECTION"}, "position": {"x": -100, "y": 300}},
        {"id": "dropped", "type": "end", "data": {"label": "Lead Dropped"}, "position": {"x": 100, "y": 300}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "recovery_call"},
        {"id": "e2", "source": "recovery_call", "target": "branch_revived"},
        {"id": "e3", "source": "branch_revived", "target": "resume_docs", "label": "Yes"},
        {"id": "e4", "source": "branch_revived", "target": "dropped", "label": "No"},
    ],
}

WORKFLOW_TEMPLATES = [
    {
        "id": "loan_onboarding",
        "name": "Loan Application Onboarding",
        "use_case": "LOAN_ONBOARDING",
        "description": "Full onboarding flow: PAN verify → qualification call → doc collection → validation → underwriting",
        "graph": LOAN_ONBOARDING_TEMPLATE,
    },
    {
        "id": "top_up_outreach",
        "name": "Top-up / Renewal Outreach",
        "use_case": "TOP_UP_OUTREACH",
        "description": "Pitch existing borrowers for top-up: pitch call → minimal doc collection → underwriting",
        "graph": TOP_UP_TEMPLATE,
    },
    {
        "id": "drop_off_recovery",
        "name": "Mid-Journey Drop-off Recovery",
        "use_case": "DROP_OFF_RECOVERY",
        "description": "Re-engage silent borrowers: recovery call → resume from last checkpoint",
        "graph": DROP_OFF_TEMPLATE,
    },
]
