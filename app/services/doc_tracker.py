"""
Document Tracker — tracks required vs received vs validated documents per lead.

Provides:
  get_doc_status(lead_id, tenant_id) → full checklist with status per doc
  get_missing_docs_message(lead_id, tenant_id) → formatted message of pending docs
  get_completion_summary(lead_id, tenant_id) → {total, received, validated, pending}
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from app.utils.logging import get_logger

logger = get_logger("doc_tracker")

# Map logical doc_type codes to human-readable checklist names
_DOC_TYPE_TO_CHECKLIST = {
    "AADHAAR": ["Aadhaar Card", "Aadhaar Card (front & back)", "Aadhaar + PAN of all directors", "Aadhaar Card of all partners"],
    "PAN_CARD": ["PAN Card", "PAN Card of firm and partners"],
    "BANK_STATEMENT": ["Bank Statement (last 12 months)"],
    "ITR": ["Latest ITR", "Latest ITR with computation", "Latest 2 years ITR / Audited P&L"],
    "GST_CERT": ["GST Certificate"],
    "GST_RETURN": ["GST Returns (last 6 months)"],
    "AUDITED_PL": ["Audited P&L + Balance Sheet (2 years)", "Latest 2 years ITR / Audited P&L"],
    "PARTNERSHIP_DEED": ["Partnership Deed"],
    "COI": ["Certificate of Incorporation (COI)"],
    "MOA": ["MOA + AOA"],
    "AOA": ["MOA + AOA"],
    "UDYAM": ["UDYAM Registration Certificate"],
    "TITLE_DEED": ["Title Deed"],
    "ELECTRICITY_BILL": ["Office Address Proof", "Address Proof"],
}


def _checklist_name_matches_doc_type(checklist_name: str, doc_type: str) -> bool:
    """Check if a classified doc_type matches a checklist item name."""
    checklist_name_lower = checklist_name.lower()
    matches = _DOC_TYPE_TO_CHECKLIST.get(doc_type, [])
    for match in matches:
        if match.lower() in checklist_name_lower or checklist_name_lower in match.lower():
            return True
    # Fallback: fuzzy keyword matching
    doc_keywords = doc_type.lower().replace("_", " ").split()
    return all(kw in checklist_name_lower for kw in doc_keywords if len(kw) > 2)


async def get_doc_status(db, lead_id: str, tenant_id: str) -> dict:
    """
    Return the full document checklist with status for each item.

    Returns:
        {
            "entity_type": "PROPRIETORSHIP",
            "checklist": [
                {
                    "name": "Aadhaar Card (front & back)",
                    "required": True,
                    "status": "VALIDATED" | "RECEIVED" | "FAILED" | "PENDING",
                    "doc_type": "AADHAAR" | null,
                    "logical_doc_id": "..." | null,
                    "received_at": "..." | null,
                    "validation_status": "TIER1_PASSED" | "TIER1_FAILED" | null,
                    "validation_detail": "..." | null,
                    "original_filename": "..." | null,
                    "channel_received": "WHATSAPP" | "EMAIL" | null,
                }
            ],
            "summary": {"total": 5, "received": 3, "validated": 2, "pending": 2, "failed": 0}
        }
    """
    from app.services.validation_rules import get_doc_collection_config

    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
    if not lead:
        return {"entity_type": "INDIVIDUAL", "checklist": [], "summary": {}}

    entity_type = lead.get("entity_type", "INDIVIDUAL")
    config = await get_doc_collection_config(db, tenant_id)
    entity_checklist = config.get("doc_checklist_by_entity_type", {}).get(
        entity_type, config.get("doc_checklist_by_entity_type", {}).get("INDIVIDUAL", {})
    )
    required_docs = entity_checklist.get("required", [])
    optional_docs = entity_checklist.get("optional", [])

    # Fetch all logical docs for this lead
    logical_docs = []
    async for doc in db.logical_docs.find(
        {"lead_id": lead_id, "tenant_id": tenant_id},
        sort=[("created_at", 1)],
    ):
        logical_docs.append(doc)

    # Fetch physical files for channel info
    phys_files = {}
    async for pf in db.phys_files.find({"lead_id": lead_id, "tenant_id": tenant_id}):
        phys_files[str(pf["_id"])] = pf

    # Build checklist items
    checklist = []
    used_logical_doc_ids = set()

    for doc_name in required_docs:
        item = _build_checklist_item(doc_name, True, logical_docs, phys_files, used_logical_doc_ids)
        checklist.append(item)

    for doc_name in optional_docs:
        item = _build_checklist_item(doc_name, False, logical_docs, phys_files, used_logical_doc_ids)
        checklist.append(item)

    # Add any extra docs that were received but aren't in the checklist
    for ldoc in logical_docs:
        if str(ldoc["_id"]) not in used_logical_doc_ids:
            pf_id = ldoc.get("physical_file_ids", [None])[0]
            pf = phys_files.get(pf_id, {}) if pf_id else {}
            status = _resolve_status(ldoc)
            checklist.append({
                "name": ldoc.get("doc_type", "OTHER").replace("_", " ").title(),
                "required": False,
                "status": status,
                "doc_type": ldoc.get("doc_type"),
                "logical_doc_id": str(ldoc["_id"]),
                "physical_file_id": pf_id,
                "received_at": ldoc.get("created_at", "").isoformat() if hasattr(ldoc.get("created_at"), "isoformat") else None,
                "validation_status": ldoc.get("status"),
                "validation_detail": _get_validation_detail(ldoc),
                "original_filename": pf.get("original_filename"),
                "channel_received": pf.get("channel_received"),
            })

    # Summary
    total = len([c for c in checklist if c["required"]])
    received = len([c for c in checklist if c["required"] and c["status"] in ("RECEIVED", "VALIDATED", "FAILED")])
    validated = len([c for c in checklist if c["required"] and c["status"] == "VALIDATED"])
    failed = len([c for c in checklist if c["required"] and c["status"] == "FAILED"])
    pending = total - received

    return {
        "entity_type": entity_type,
        "checklist": checklist,
        "summary": {
            "total_required": total,
            "received": received,
            "validated": validated,
            "pending": pending,
            "failed": failed,
            "completion_pct": round(validated / total * 100) if total > 0 else 0,
        },
    }


def _build_checklist_item(
    doc_name: str, required: bool, logical_docs: list, phys_files: dict, used_ids: set
) -> dict:
    """Match a checklist item to a received logical doc."""
    item = {
        "name": doc_name,
        "required": required,
        "status": "PENDING",
        "doc_type": None,
        "logical_doc_id": None,
        "physical_file_id": None,
        "received_at": None,
        "validation_status": None,
        "validation_detail": None,
        "original_filename": None,
        "channel_received": None,
    }

    # Find best matching logical doc
    for ldoc in logical_docs:
        ldoc_id = str(ldoc["_id"])
        if ldoc_id in used_ids:
            continue
        doc_type = ldoc.get("doc_type", "")
        if _checklist_name_matches_doc_type(doc_name, doc_type):
            used_ids.add(ldoc_id)
            pf_id = ldoc.get("physical_file_ids", [None])[0]
            pf = phys_files.get(pf_id, {}) if pf_id else {}

            item["status"] = _resolve_status(ldoc)
            item["doc_type"] = doc_type
            item["logical_doc_id"] = ldoc_id
            item["physical_file_id"] = pf_id
            item["received_at"] = ldoc.get("created_at", "").isoformat() if hasattr(ldoc.get("created_at"), "isoformat") else None
            item["validation_status"] = ldoc.get("status")
            item["validation_detail"] = _get_validation_detail(ldoc)
            item["original_filename"] = pf.get("original_filename")
            item["channel_received"] = pf.get("channel_received")
            break

    return item


def _resolve_status(ldoc: dict) -> str:
    """Map logical doc status to checklist status."""
    s = ldoc.get("status", "")
    if s == "TIER1_PASSED":
        return "VALIDATED"
    elif s == "TIER1_FAILED":
        return "FAILED"
    elif s in ("REJECTED",):
        return "FAILED"
    elif s in ("RECEIVED", "CLASSIFYING", "CLASSIFIED", "READY_FOR_EXTRACTION",
               "EXTRACTING", "EXTRACTED", "TIER1_VALIDATING", "ASSEMBLING"):
        return "RECEIVED"
    elif s == "NEEDS_HUMAN_REVIEW":
        return "RECEIVED"
    return "PENDING"


def _get_validation_detail(ldoc: dict) -> Optional[str]:
    """Extract validation detail/failure reason from logical doc."""
    t1 = ldoc.get("tier1_validation", {})
    if not t1:
        return None
    results = t1.get("rule_results", [])
    failed = [r for r in results if r.get("status") == "FAIL" or r.get("passed") is False]
    if failed:
        return "; ".join(r.get("detail", r.get("message", "Failed")) for r in failed[:3])
    if t1.get("passed"):
        return "All checks passed"
    return None


async def get_missing_docs_summary(db, lead_id: str, tenant_id: str) -> dict:
    """
    Get summary of missing/pending docs for reply messages.

    Returns:
        {
            "pending_docs": ["Aadhaar Card (front & back)", "Bank Statement (last 12 months)"],
            "received_docs": ["PAN Card", "GST Certificate"],
            "failed_docs": [{"name": "ITR", "reason": "Missing page 2"}],
            "all_done": False,
            "message": "..."  # formatted message string
        }
    """
    tracker = await get_doc_status(db, lead_id, tenant_id)
    checklist = tracker.get("checklist", [])

    pending = [c["name"] for c in checklist if c["required"] and c["status"] == "PENDING"]
    received = [c["name"] for c in checklist if c["required"] and c["status"] in ("RECEIVED", "VALIDATED")]
    failed = [{"name": c["name"], "reason": c.get("validation_detail", "Validation failed")}
              for c in checklist if c["required"] and c["status"] == "FAILED"]

    all_done = len(pending) == 0 and len(failed) == 0

    # Build message
    if all_done:
        message = (
            "✅ All required documents have been received and verified! "
            "Your application is being processed. We'll update you shortly."
        )
    else:
        parts = []
        if received:
            parts.append(f"✅ Received ({len(received)}): " + ", ".join(received))
        if failed:
            parts.append("❌ Needs resubmission:")
            for f in failed:
                parts.append(f"   • {f['name']} — {f['reason']}")
        if pending:
            parts.append(f"📋 Still needed ({len(pending)}):")
            for i, doc in enumerate(pending, 1):
                parts.append(f"   {i}. {doc}")
        message = "\n".join(parts)

    return {
        "pending_docs": pending,
        "received_docs": received,
        "failed_docs": failed,
        "all_done": all_done,
        "message": message,
    }
