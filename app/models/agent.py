"""
Agent Pydantic models — full config schemas per PRD Section 5.7.

Every agent type (VOICE_AI, DOC_COLLECTION, EXTRACTION_AI, VALIDATION_AI) has a
detailed config schema that matches the Agent Studio frontend forms and is read
by the backend services at runtime.  No hardcoded defaults in services — they all
read from the database config.
"""
from pydantic import BaseModel, Field, model_validator
from typing import Optional, Any
from datetime import datetime
from enum import Enum


class AgentType(str, Enum):
    VOICE_AI = "VOICE_AI"
    DOC_COLLECTION = "DOC_COLLECTION"
    EXTRACTION_AI = "EXTRACTION_AI"
    VALIDATION_AI = "VALIDATION_AI"


class AgentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DRAFT = "DRAFT"
    INACTIVE = "INACTIVE"


# ---------------------------------------------------------------------------
# VOICE_AI — full config schema
# ---------------------------------------------------------------------------

class QualificationQuestion(BaseModel):
    question_key: str            # e.g. "monthly_turnover"
    question_text: str           # e.g. "Aapka business ka monthly turnover kitna hai?"
    data_type: str = "text"      # "text" | "number" | "boolean"
    required: bool = True


class CallScheduleWindow(BaseModel):
    start_hour: int = 9   # 9 AM IST — no calls before this
    end_hour: int = 18    # 6 PM IST — no calls after this


class VoiceAIConfig(BaseModel):
    """
    Ops-editable config for the Voice AI agent.
    Backend reads this from DB and uses it to build the ElevenLabs API call.
    """
    # Conversation script — {{borrower_name}}, {{loan_type}}, {{loan_amount}}, {{company_name}} are injected
    script_template: str = (
        "You are a friendly loan officer calling on behalf of {{company_name}}. "
        "Greet {{borrower_name}} warmly. Confirm their interest in a {{loan_type}} "
        "of {{loan_amount}}. Collect qualification data and consent."
    )
    # First sentence spoken when borrower answers
    opening_message: str = (
        "Namaste {{borrower_name}} ji, main Gain AI se bol raha hoon. "
        "Kya aap abhi baat kar sakte hain?"
    )
    # Language for the ElevenLabs agent
    language: str = "HINGLISH"    # "HINDI" | "ENGLISH" | "HINGLISH"

    # ElevenLabs voice overrides (optional — blank = use dashboard default)
    elevenlabs_voice_id: Optional[str] = None
    voice_stability: Optional[float] = None          # 0.0–1.0
    voice_similarity_boost: Optional[float] = None   # 0.0–1.0

    # Questions the agent must ask — keys used by Claude to extract structured data
    qualification_questions: list[QualificationQuestion] = [
        QualificationQuestion(
            question_key="monthly_turnover",
            question_text="Aapke business ka monthly turnover approx kitna hai?",
            data_type="number",
            required=True,
        ),
        QualificationQuestion(
            question_key="business_vintage_years",
            question_text="Aapka business kitne saal purana hai?",
            data_type="number",
            required=True,
        ),
        QualificationQuestion(
            question_key="existing_emis",
            question_text="Kya aapke upar koi existing loan ki EMI chal rahi hai?",
            data_type="text",
            required=True,
        ),
        QualificationQuestion(
            question_key="consent_given",
            question_text="Kya aap is loan ke liye apply karna chahenge?",
            data_type="boolean",
            required=True,
        ),
    ]

    # Call behaviour
    max_call_duration_minutes: int = 8
    max_attempts: int = 3
    retry_after_hours: int = 2
    fallback_to_whatsapp_after_n_failures: int = 3
    call_schedule_window: CallScheduleWindow = CallScheduleWindow()


# ---------------------------------------------------------------------------
# DOC_COLLECTION — full config schema
# ---------------------------------------------------------------------------

class DocChecklistByEntity(BaseModel):
    """Required and optional documents for one entity type."""
    required: list[str] = []
    optional: list[str] = []


class DocCollectionConfig(BaseModel):
    """
    Ops-editable config for the Doc Collection agent.
    Backend reads doc_checklist_by_entity_type to build WhatsApp/email messages.
    """
    # Message templates — {{borrower_name}}, {{doc_list}}, {{company_name}} are injected
    whatsapp_checklist_template: str = (
        "Hello {{borrower_name}} ji! 🙏\n\n"
        "Thank you for your interest in our loan product.\n\n"
        "Please send the following documents to process your application for {{company_name}}:\n\n"
        "{{doc_list}}\n\n"
        "You can send documents one by one or as a ZIP file.\n"
        "Reply HELP if you need assistance."
    )
    email_subject_template: str = (
        "Documents Required — {{loan_type}} Application for {{company_name}}"
    )
    email_body_template: str = (
        "Dear {{borrower_name}},\n\n"
        "Thank you for your interest. Please send the following documents:\n\n"
        "{{doc_list}}\n\n"
        "You may reply to this email with documents as attachments.\n\n"
        "Regards,\nGain AI Operations Team"
    )

    # Channels to use
    channels: list[str] = ["WHATSAPP", "EMAIL"]

    # Follow-up nudge schedule (days after initial checklist)
    reminder_schedule_days: list[int] = [1, 3, 5, 7]
    reminder_whatsapp_template: str = (
        "Hi {{borrower_name}}, gentle reminder! 📋\n\n"
        "We are still waiting for:\n{{pending_docs}}\n\n"
        "Please send these to continue your loan application."
    )
    escalate_to_rm_after_days: int = 7

    # Document checklist per entity type — ops team edits from Agent Studio
    doc_checklist_by_entity_type: dict[str, DocChecklistByEntity] = {
        "PROPRIETORSHIP": DocChecklistByEntity(
            required=[
                "AADHAAR", "PAN_CARD", "BANK_STATEMENT",
                "ITR", "GST_CERT", "UDYAM",
            ],
            optional=["GST_RETURN", "ELECTRICITY_BILL"],
        ),
        "PARTNERSHIP": DocChecklistByEntity(
            required=[
                "AADHAAR", "PAN_CARD", "PARTNERSHIP_DEED",
                "BANK_STATEMENT", "ITR", "GST_CERT",
            ],
            optional=["GST_RETURN", "AUDITED_PL"],
        ),
        "PRIVATE_LIMITED": DocChecklistByEntity(
            required=[
                "AADHAAR", "PAN_CARD", "COI", "MOA", "AOA",
                "BANK_STATEMENT", "AUDITED_PL", "GST_CERT",
            ],
            optional=["GST_RETURN", "ITR"],
        ),
        "LLP": DocChecklistByEntity(
            required=[
                "AADHAAR", "PAN_CARD", "COI", "MOA",
                "BANK_STATEMENT", "AUDITED_PL", "GST_CERT",
            ],
            optional=["GST_RETURN"],
        ),
        "PUBLIC_LIMITED": DocChecklistByEntity(
            required=[
                "AADHAAR", "PAN_CARD", "COI", "MOA", "AOA",
                "BANK_STATEMENT", "AUDITED_PL", "GST_CERT", "GST_RETURN",
            ],
            optional=["ITR"],
        ),
        "INDIVIDUAL": DocChecklistByEntity(
            required=["AADHAAR", "PAN_CARD", "BANK_STATEMENT", "ITR"],
            optional=["ELECTRICITY_BILL"],
        ),
    }

    # File upload rules
    accepted_file_types: list[str] = ["PDF", "JPG", "PNG", "ZIP"]
    max_file_size_mb: int = 25


# ---------------------------------------------------------------------------
# EXTRACTION_AI — full config schema
# ---------------------------------------------------------------------------

class ExtractionAIConfig(BaseModel):
    """
    Ops-editable config for the Extraction AI agent.
    Controls Claude classification/extraction prompts and confidence thresholds.
    """
    # Documents classified below this score → NEEDS_HUMAN_REVIEW
    classification_confidence_threshold: int = 75

    # Fields to extract per document type — ops team can add/remove fields
    extraction_fields_by_doc_type: dict[str, list[str]] = {
        "BANK_STATEMENT": [
            "account_holder_name",
            "bank_name",
            "account_number_last4",
            "ifsc_code",
            "period_from",
            "period_to",
            "avg_monthly_balance",
            "avg_monthly_credits",
            "avg_monthly_debits",
            "opening_balance",
            "closing_balance",
        ],
        "ITR": [
            "taxpayer_name",
            "pan",
            "assessment_year",
            "gross_total_income",
            "net_taxable_income",
            "gross_turnover",
            "net_profit",
        ],
        "GST_CERT": [
            "gstin",
            "legal_name",
            "trade_name",
            "registration_date",
            "business_type",
            "principal_place_of_business",
        ],
        "GST_RETURN": [
            "gstin",
            "legal_name",
            "period_from",
            "period_to",
            "total_taxable_turnover",
            "total_tax_paid",
        ],
        "AUDITED_PL": [
            "business_name",
            "financial_year",
            "gross_revenue",
            "total_expenses",
            "net_profit",
            "depreciation",
        ],
        "AADHAAR": [
            "name",
            "dob",
            "gender",
            "aadhaar_last4",
            "address",
        ],
        "PAN_CARD": [
            "name",
            "fathers_name",
            "dob",
            "pan_number",
        ],
        "MOA": [
            "company_name",
            "cin",
            "registered_office",
            "directors",
            "authorized_capital",
        ],
        "COI": [
            "company_name",
            "cin",
            "date_of_incorporation",
            "company_type",
        ],
        "UDYAM": [
            "enterprise_name",
            "udyam_registration_number",
            "major_activity",
            "social_category",
            "date_of_registration",
        ],
        "TITLE_DEED": [
            "property_address",
            "owner_name",
            "survey_number",
            "registration_date",
            "property_area",
        ],
        "ELECTRICITY_BILL": [
            "consumer_name",
            "address",
            "bill_date",
            "consumer_number",
            "amount_due",
        ],
        "PARTNERSHIP_DEED": [
            "firm_name",
            "partners",
            "profit_sharing_ratio",
            "date_of_partnership",
            "registered_address",
        ],
        "PROPERTY_TAX": [
            "owner_name",
            "property_address",
            "assessment_year",
            "tax_amount",
        ],
        "NOC": [
            "borrower_name",
            "issued_by",
            "issue_date",
            "property_description",
        ],
    }

    # What to do when classification confidence is below threshold
    on_low_confidence_classification: str = "FLAG_FOR_REVIEW"
    # "FLAG_FOR_REVIEW" | "AUTO_REJECT" | "AUTO_ACCEPT_BEST_GUESS"

    # Lender-specific prompt additions (no code change needed)
    classification_prompt_additions: str = ""
    extraction_prompt_additions: str = ""


# ---------------------------------------------------------------------------
# VALIDATION_AI — full config schema
# ---------------------------------------------------------------------------

class ValidationRule(BaseModel):
    rule_id: str             # e.g. "T1-001"
    rule_name: str           # e.g. "Bank Statement Coverage"
    doc_type: str            # e.g. "BANK_STATEMENT" (or "ANY" for universal rules)
    enabled: bool = True
    threshold: Optional[Any] = None   # Rule-specific threshold (e.g. 12 for months, 3 for days)
    custom_prompt: str = ""   # Optional extra context for Claude when evaluating this rule


class ValidationAIConfig(BaseModel):
    """
    Ops-editable config for the Validation AI agent.
    Tier 1 and Tier 2 rules are loaded from here at runtime — not hardcoded.
    """
    # Tier 1: per-document rules run immediately after extraction
    tier1_rules: list[ValidationRule] = [
        ValidationRule(
            rule_id="T1-001",
            rule_name="Bank Statement Coverage",
            doc_type="BANK_STATEMENT",
            enabled=True,
            threshold=12,
            custom_prompt="Check that the bank statement covers a complete 12 consecutive months.",
        ),
        ValidationRule(
            rule_id="T1-002",
            rule_name="Electricity Bill Recency",
            doc_type="ELECTRICITY_BILL",
            enabled=True,
            threshold=3,
            custom_prompt="Check that the electricity bill issue date is within the last 3 months.",
        ),
        ValidationRule(
            rule_id="T1-003",
            rule_name="ITR Financial Year",
            doc_type="ITR",
            enabled=True,
            threshold=None,
            custom_prompt=(
                "Check that the ITR is for the most recent complete financial year. "
                "Flag for review if it is more than 2 years old — do not auto-reject."
            ),
        ),
        ValidationRule(
            rule_id="T1-004",
            rule_name="GST Return Coverage",
            doc_type="GST_RETURN",
            enabled=True,
            threshold=6,
            custom_prompt="Check that GST returns cover at least the last 6 months.",
        ),
        ValidationRule(
            rule_id="T1-005",
            rule_name="KYC Document Readability",
            doc_type="ANY",
            enabled=True,
            threshold=None,
            custom_prompt=(
                "Check that the KYC document (Aadhaar or PAN) is legible — not blurry, "
                "not expired, and all key fields are readable."
            ),
        ),
        ValidationRule(
            rule_id="T1-006",
            rule_name="Bank Statement — Scheduled Bank",
            doc_type="BANK_STATEMENT",
            enabled=True,
            threshold=None,
            custom_prompt=(
                "Check that the bank statement is from a Scheduled Commercial Bank. "
                "Co-operative bank statements should be flagged for ops review."
            ),
        ),
        ValidationRule(
            rule_id="T1-007",
            rule_name="Minimum File Size",
            doc_type="ANY",
            enabled=True,
            threshold=10240,  # 10 KB in bytes
            custom_prompt="Reject any file smaller than 10KB — likely corrupt or placeholder.",
        ),
        ValidationRule(
            rule_id="T1-008",
            rule_name="Partial Document Completeness",
            doc_type="ANY",
            enabled=True,
            threshold=48,  # hours to wait before sending nudge
            custom_prompt=(
                "If this document is marked PARTIAL, check whether all parts have now been received. "
                "If complete, mark as COMPLETE and proceed. If incomplete after 48 hours, send a nudge."
            ),
        ),
    ]

    # Tier 2: cross-document rules run once all mandatory docs pass Tier 1
    tier2_rules: list[ValidationRule] = [
        ValidationRule(
            rule_id="T2-001",
            rule_name="Partnership KYC Completeness",
            doc_type="PARTNERSHIP_DEED",
            enabled=True,
            threshold=None,
            custom_prompt=(
                "For every partner named in the partnership deed, verify that both "
                "Aadhaar AND PAN card have been received. List missing partner names."
            ),
        ),
        ValidationRule(
            rule_id="T2-002",
            rule_name="GST vs ITR Turnover Consistency",
            doc_type="ANY",
            enabled=True,
            threshold=20,  # % variance allowed
            custom_prompt=(
                "Compare the GST declared turnover with ITR declared turnover. "
                "They should not vary by more than 20%. Flag with exact figures if they do."
            ),
        ),
        ValidationRule(
            rule_id="T2-003",
            rule_name="Bank Statement vs ITR Consistency",
            doc_type="ANY",
            enabled=True,
            threshold=40,  # % variance allowed
            custom_prompt=(
                "Compare average monthly credits in bank statement with ITR turnover. "
                "They should be broadly consistent (within 40% tolerance). Flag for ops review if not."
            ),
        ),
        ValidationRule(
            rule_id="T2-004",
            rule_name="Director KYC Completeness",
            doc_type="MOA",
            enabled=True,
            threshold=None,
            custom_prompt=(
                "For each director named in the MOA or COI, verify that both "
                "Aadhaar AND PAN have been received. List missing director names."
            ),
        ),
        ValidationRule(
            rule_id="T2-005",
            rule_name="Audited P&L vs ITR Net Profit",
            doc_type="ANY",
            enabled=True,
            threshold=15,  # % variance allowed
            custom_prompt=(
                "Compare net profit in Audited P&L with net income in ITR. "
                "They should match within 15% tolerance. Flag with both figures."
            ),
        ),
        ValidationRule(
            rule_id="T2-006",
            rule_name="No Future-Dated Documents",
            doc_type="ANY",
            enabled=True,
            threshold=None,
            custom_prompt=(
                "Check all documents for date fields. Flag any document with a date "
                "in the future (issue date, bill date, registration date, etc.)."
            ),
        ),
    ]

    # What to do when Tier 1 or Tier 2 fails
    on_tier1_failure_action: str = "NOTIFY_BORROWER_AND_CONTINUE"
    # "NOTIFY_BORROWER_AND_CONTINUE" | "BLOCK_LEAD" | "FLAG_FOR_OPS_REVIEW"
    on_tier2_failure_action: str = "FLAG_FOR_OPS_REVIEW"
    # "FLAG_FOR_OPS_REVIEW" | "BLOCK_LEAD"

    # WhatsApp/email templates for each Tier 1 rule failure notification
    tier1_failure_message_templates: dict[str, str] = {
        "T1-001": (
            "Your bank statement only covers {{months_covered}} months. "
            "Please send a complete 12-month bank statement ({{expected_period}})."
        ),
        "T1-002": (
            "Your electricity bill dated {{bill_date}} is more than 3 months old. "
            "Please send a bill from the last 3 months."
        ),
        "T1-003": (
            "The ITR received appears to be for an older financial year. "
            "Please send the most recent ITR if available."
        ),
        "T1-004": (
            "Your GST returns cover only {{months_covered}} months. "
            "Please send returns for at least the last 6 months."
        ),
        "T1-005": (
            "The {{doc_type}} you sent is not clearly readable. "
            "Please send a clear, high-quality scan or photo."
        ),
        "T1-006": (
            "The bank statement you sent is from a co-operative bank. "
            "Please also provide a statement from a scheduled commercial bank."
        ),
        "T1-007": (
            "The file you sent appears to be corrupt or incomplete. "
            "Please resend the document."
        ),
        "T1-008": (
            "We received only part of your {{doc_type}}. "
            "Please send all pages of the complete document."
        ),
    }

    # Additional lender-specific prompt additions
    validation_prompt_additions: str = ""


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

# Map agent type → config class (for validation)
_CONFIG_SCHEMA_MAP = {
    AgentType.VOICE_AI: VoiceAIConfig,
    AgentType.DOC_COLLECTION: DocCollectionConfig,
    AgentType.EXTRACTION_AI: ExtractionAIConfig,
    AgentType.VALIDATION_AI: ValidationAIConfig,
}


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    type: AgentType
    config: dict[str, Any] = {}

    @model_validator(mode="after")
    def validate_config(self) -> "AgentCreate":
        """Validate config against the correct schema for this agent type."""
        schema_cls = _CONFIG_SCHEMA_MAP.get(self.type)
        if schema_cls:
            try:
                schema_cls(**self.config)
            except Exception as exc:
                raise ValueError(f"Invalid config for agent type {self.type}: {exc}") from exc
        return self


class AgentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    status: Optional[AgentStatus] = None
    config: Optional[dict[str, Any]] = None


class AgentResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    type: AgentType
    status: AgentStatus
    config: dict[str, Any]
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
