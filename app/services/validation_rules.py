"""
Validation rules service — orchestrates Tier 1 and Tier 2 validation for leads.

Tier 1: per-document rules (run after each document is extracted).
Tier 2: cross-document consistency rules (run once all mandatory docs pass T1).

Both tiers read their rules from the VALIDATION_AI agent config stored in MongoDB.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from app.database import get_db
from app.utils.logging import get_logger

logger = get_logger("validation_rules")

# Canonical list of mandatory doc types that must be present before Tier 2 fires.
# The actual required list is determined per entity_type from the DOC_COLLECTION config.
_MANDATORY_STATUSES = {"TIER1_PASSED", "HUMAN_REVIEWED"}


async def get_validation_agent_config(db, tenant_id: str) -> dict:
    """Fetch VALIDATION_AI agent config from DB. Returns defaults if not found."""
    agent = await db.agents.find_one(
        {"tenant_id": tenant_id, "type": "VALIDATION_AI", "status": "ACTIVE"}
    )
    if agent and agent.get("config"):
        return agent["config"]
    logger.warning(f"No active VALIDATION_AI agent for tenant_id={tenant_id} — using built-in defaults")
    return {
        "tier1_rules": _DEFAULT_TIER1_RULES,
        "tier2_rules": [],
        "on_tier1_failure_action": "NOTIFY_BORROWER_AND_CONTINUE",
        "on_tier2_failure_action": "FLAG_FOR_OPS_REVIEW",
        "tier1_failure_message_templates": {},
    }


# Default tier 1 validation rules (used when no VALIDATION_AI agent in DB)
_DEFAULT_TIER1_RULES = [
    {"rule": "file_size_minimum", "params": {"min_bytes": 10240}, "description": "File must be at least 10KB"},
    {"rule": "file_not_blank", "params": {}, "description": "Document must contain readable content"},
    {"rule": "name_match_fuzzy", "params": {"threshold": 0.7}, "description": "Name on document should match lead name"},
]


async def get_extraction_agent_config(db, tenant_id: str) -> dict:
    """Fetch EXTRACTION_AI agent config. Returns defaults if not found."""
    agent = await db.agents.find_one(
        {"tenant_id": tenant_id, "type": "EXTRACTION_AI", "status": "ACTIVE"}
    )
    if agent and agent.get("config"):
        return agent["config"]
    logger.warning(f"No active EXTRACTION_AI agent for tenant_id={tenant_id} — using built-in defaults")
    return {
        "classification_confidence_threshold": 75,
        "classification_prompt_additions": "",
        "extraction_prompt_additions": "",
        "extraction_fields_by_doc_type": _DEFAULT_EXTRACTION_FIELDS,
    }


# Default extraction fields per document type (used when no EXTRACTION_AI agent in DB)
_DEFAULT_EXTRACTION_FIELDS: dict = {
    "PAN_CARD": ["pan_number", "name_on_pan", "date_of_birth", "father_name"],
    "AADHAAR_CARD": ["aadhaar_number", "name", "date_of_birth", "address", "gender"],
    "BANK_STATEMENT": ["account_number", "bank_name", "account_holder", "period_from", "period_to", "opening_balance", "closing_balance"],
    "ITR": ["assessment_year", "pan_number", "name", "total_income", "tax_paid", "filing_date"],
    "GST_CERTIFICATE": ["gstin", "legal_name", "trade_name", "date_of_registration", "business_type"],
    "PARTNERSHIP_DEED": ["firm_name", "partners", "date_of_deed", "business_nature"],
    "COI": ["company_name", "cin_number", "date_of_incorporation", "registered_address"],
    "MOA_AOA": ["company_name", "authorized_capital", "objectives"],
    "UDYAM_CERTIFICATE": ["udyam_number", "enterprise_name", "type_of_enterprise", "date_of_registration"],
    "ADDRESS_PROOF": ["name", "address", "document_type"],
    "SALARY_SLIP": ["employee_name", "employer_name", "month", "gross_salary", "net_salary"],
    "BALANCE_SHEET": ["company_name", "financial_year", "total_assets", "total_liabilities", "net_worth"],
    "PNL_STATEMENT": ["company_name", "financial_year", "revenue", "expenses", "net_profit"],
    "GST_RETURN": ["gstin", "return_period", "turnover", "tax_payable"],
}


async def get_doc_collection_config(db, tenant_id: str) -> dict:
    """Fetch DOC_COLLECTION agent config. Returns defaults if not found."""
    agent = await db.agents.find_one(
        {"tenant_id": tenant_id, "type": "DOC_COLLECTION", "status": "ACTIVE"}
    )
    if agent and agent.get("config"):
        return agent["config"]
    # Sensible Indian SME lending defaults when no agent config exists
    return {
        "doc_checklist_by_entity_type": {
            "PROPRIETORSHIP": {
                "required": ["Aadhaar Card (front & back)", "PAN Card", "Bank Statement (last 12 months)", "Latest ITR with computation", "GST Certificate"],
                "optional": ["UDYAM Registration Certificate", "Office Address Proof"],
            },
            "PARTNERSHIP": {
                "required": ["Aadhaar Card of all partners", "PAN Card of firm and partners", "Partnership Deed", "Bank Statement (last 12 months)", "Latest 2 years ITR / Audited P&L", "GST Certificate"],
                "optional": ["GST Returns (last 6 months)"],
            },
            "PRIVATE_LIMITED": {
                "required": ["Aadhaar + PAN of all directors", "Certificate of Incorporation (COI)", "MOA + AOA", "Bank Statement (last 12 months)", "Audited P&L + Balance Sheet (2 years)", "GST Certificate"],
                "optional": ["GST Returns (last 6 months)"],
            },
            "INDIVIDUAL": {
                "required": ["Aadhaar Card", "PAN Card", "Bank Statement (last 12 months)", "Latest ITR", "Address Proof"],
                "optional": [],
            },
        },
        "whatsapp_checklist_template": (
            "Hello {{borrower_name}} ji! 👋\n\n"
            "Thank you for your {{loan_type}} application with Gain AI.\n\n"
            "Please send the following documents to continue your application:\n\n"
            "{{doc_list}}\n\n"
            "You can send documents one by one or all at once as a ZIP file.\n"
            "Reply HELP if you need assistance."
        ),
        "email_subject_template": "Documents Required — {{loan_type}} Application for {{company_name}}",
        "email_body_template": (
            "Dear {{borrower_name}},\n\n"
            "Thank you for your loan application. To proceed, please share the following documents:\n\n"
            "{{doc_list}}\n\n"
            "You can reply to this email with the documents attached.\n\n"
            "Regards,\nGain AI Operations Team"
        ),
    }


async def check_all_required_docs_passed(
    db, lead_id: str, tenant_id: str
) -> bool:
    """
    Check whether all required documents for this lead have passed Tier 1.
    Returns True if we should proceed to Tier 2 validation.
    """
    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
    if not lead:
        return False

    entity_type = lead.get("entity_type", "INDIVIDUAL")

    # Get required doc types for this entity from DOC_COLLECTION config
    doc_config = await get_doc_collection_config(db, tenant_id)
    entity_checklist = doc_config.get("doc_checklist_by_entity_type", {}).get(entity_type, {})
    required_doc_types = set(entity_checklist.get("required", []))

    if not required_doc_types:
        logger.info(f"No required doc types defined for entity_type={entity_type} — T2 will run")
        return True

    # Check logical docs
    cursor = db.logical_docs.find({"lead_id": lead_id, "tenant_id": tenant_id, "is_mandatory": True})
    passed_types = set()
    async for doc in cursor:
        if doc.get("status") in _MANDATORY_STATUSES:
            passed_types.add(doc.get("doc_type", ""))

    missing = required_doc_types - passed_types
    if missing:
        logger.info(
            f"T2 check for lead_id={lead_id}: {len(missing)} required docs still pending: {missing}"
        )
        return False

    logger.info(f"T2 check for lead_id={lead_id}: all {len(required_doc_types)} required docs passed T1")
    return True


async def build_tier2_lead_summary(db, lead_id: str, tenant_id: str) -> dict:
    """
    Build the complete lead summary with all extracted data for Tier 2.
    """
    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
    if not lead:
        return {}

    logical_docs = []
    cursor = db.logical_docs.find(
        {"lead_id": lead_id, "tenant_id": tenant_id, "status": {"$in": list(_MANDATORY_STATUSES)}}
    )
    async for doc in cursor:
        logical_docs.append({
            "doc_type": doc.get("doc_type"),
            "extracted_data": doc.get("extracted_data", {}),
            "completeness_status": doc.get("completeness_status", "COMPLETE"),
        })

    return {
        "lead_id": lead_id,
        "borrower_name": lead.get("name", ""),
        "entity_type": lead.get("entity_type", ""),
        "loan_type": lead.get("loan_type", ""),
        "logical_docs": logical_docs,
    }


async def notify_tier1_failure(
    db,
    lead_id: str,
    tenant_id: str,
    doc_type: str,
    failed_rule_ids: list[str],
    tier1_failure_templates: dict,
) -> None:
    """
    Send WhatsApp notification to borrower for Tier 1 failures.
    Enqueues a WhatsApp text message with the relevant failure template.
    """
    from app.services.whatsapp_service import find_lead_by_whatsapp_number

    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
    if not lead:
        return

    from app.utils.encryption import decrypt_field
    mobile_raw = decrypt_field(lead.get("mobile", ""))
    if not mobile_raw:
        return

    for rule_id in failed_rule_ids:
        template = tier1_failure_templates.get(rule_id, "")
        if not template:
            continue
        # Basic variable substitution
        message = (
            template
            .replace("{{doc_type}}", doc_type)
            .replace("{{months_covered}}", "?")
            .replace("{{expected_period}}", "last 12 months")
            .replace("{{bill_date}}", "?")
        )
        try:
            from app.services.whatsapp_service import send_text_message
            await send_text_message(lead_id, mobile_raw, message)
            # Save to whatsapp_messages
            await db.whatsapp_messages.insert_one({
                "lead_id": lead_id,
                "tenant_id": tenant_id,
                "direction": "OUTBOUND",
                "message_type": "TEXT",
                "content": message,
                "status": "SENT",
                "template_name": f"T1_FAILURE_{rule_id}",
                "sent_at": datetime.now(timezone.utc),
            })
        except Exception as exc:
            logger.error(f"Failed to send T1 failure notification for lead_id={lead_id}: {exc}")
