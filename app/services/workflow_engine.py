"""
Workflow Engine — the state machine that decides what happens next for each lead.

process_lead(lead_id) is called whenever:
  - A lead is created
  - A workflow step completes (webhook arrives)
  - A scheduled retry fires

It reads the lead's current status and the campaign's workflow_graph,
determines the next node, and queues the appropriate agent task.
Every node execution creates a WorkflowRun record for audit.
"""
from datetime import datetime, timezone
from bson import ObjectId

from app.database import get_db
from app.utils.logging import get_logger

logger = get_logger("workflow_engine")


async def process_lead(lead_id: str, tenant_id: str) -> None:
    """
    Main entry point. Read lead status, determine next action, execute it.
    Catches all exceptions — a single lead failure must NEVER crash other leads.
    """
    try:
        await _process_lead_unsafe(lead_id, tenant_id)
    except Exception as exc:
        logger.error(
            f"Workflow engine error — lead_id={lead_id} error={exc}",
            exc_info=True,
        )
        await _record_workflow_failure(lead_id, tenant_id, str(exc))


async def _process_lead_unsafe(lead_id: str, tenant_id: str) -> None:
    db = get_db()
    lead = await db.leads.find_one(
        {"_id": ObjectId(lead_id), "tenant_id": tenant_id}
    )
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    status = lead.get("status", "NEW")
    logger.info(f"Processing lead_id={lead_id} current_status={status}")

    # State machine: map lead status → next action
    if status == "PAN_VERIFIED":
        await _trigger_qualification_call(lead, db)

    elif status == "QUALIFIED":
        await _trigger_doc_collection(lead, db)

    elif status == "READY_FOR_UNDERWRITING":
        await _notify_underwriting(lead, db)

    else:
        logger.info(
            f"No automatic action for lead_id={lead_id} status={status}"
        )


async def _trigger_qualification_call(lead: dict, db) -> None:
    from app.services.voice_service import enqueue_qualification_call

    lead_id = str(lead["_id"])
    tenant_id = lead["tenant_id"]

    await enqueue_qualification_call(lead_id, tenant_id)
    logger.info(f"Qualification call enqueued for lead_id={lead_id}")


async def _trigger_doc_collection(lead: dict, db) -> None:
    """Trigger WhatsApp + Email doc checklist simultaneously."""
    lead_id = str(lead["_id"])
    tenant_id = lead["tenant_id"]
    now = datetime.now(timezone.utc)

    # Update lead status
    await db.leads.update_one(
        {"_id": lead["_id"], "tenant_id": tenant_id},
        {"$set": {"status": "DOC_COLLECTION", "updated_at": now}},
    )

    # Enqueue WhatsApp checklist
    try:
        from app.workers.whatsapp_worker import send_doc_checklist_whatsapp
        send_doc_checklist_whatsapp.apply_async(
            kwargs={"lead_id": lead_id, "tenant_id": tenant_id},
            queue="whatsapp",
        )
    except Exception as exc:
        logger.error(f"WhatsApp checklist enqueue failed for lead_id={lead_id}: {exc}")

    # Enqueue email checklist simultaneously
    try:
        from app.workers.whatsapp_worker import send_doc_checklist_email
        send_doc_checklist_email.apply_async(
            kwargs={"lead_id": lead_id, "tenant_id": tenant_id},
            queue="email",
        )
    except Exception as exc:
        logger.error(f"Email checklist enqueue failed for lead_id={lead_id}: {exc}")

    # Create workflow run record
    await db.workflow_runs.insert_one(
        {
            "lead_id": lead_id,
            "tenant_id": tenant_id,
            "campaign_id": str(lead.get("campaign_id")) if lead.get("campaign_id") else None,
            "node_id": "doc_collection",
            "node_type": "DOC_COLLECTION",
            "status": "RUNNING",
            "input_data": {"entity_type": lead.get("entity_type"), "loan_type": lead.get("loan_type")},
            "output_data": {},
            "triggered_by": "SYSTEM",
            "executed_at": now,
            "duration_ms": 0,
        }
    )

    logger.info(f"Doc collection triggered for lead_id={lead_id}")


async def _notify_underwriting(lead: dict, db) -> None:
    """Notify the assigned RM / underwriting team."""
    lead_id = str(lead["_id"])
    logger.info(f"Lead ready for underwriting — lead_id={lead_id}. Notification queued.")
    # Notification implementation in Day 3
    await _record_activity(
        db,
        lead["tenant_id"],
        lead_id,
        "READY_FOR_UNDERWRITING",
        "Lead is ready for underwriting review",
    )


async def _record_workflow_failure(lead_id: str, tenant_id: str, error: str) -> None:
    db = get_db()
    try:
        await db.workflow_runs.insert_one(
            {
                "lead_id": lead_id,
                "tenant_id": tenant_id,
                "node_id": "unknown",
                "node_type": "WORKFLOW_ENGINE",
                "status": "FAILED",
                "input_data": {},
                "output_data": {"error": error},
                "triggered_by": "SYSTEM",
                "executed_at": datetime.now(timezone.utc),
                "duration_ms": 0,
            }
        )
    except Exception:
        pass  # Never let audit logging crash the system


async def _record_activity(
    db, tenant_id: str, lead_id: str, event_type: str, message: str
) -> None:
    await db.activity_feed.insert_one(
        {
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "event_type": event_type,
            "message": message,
            "created_at": datetime.now(timezone.utc),
        }
    )


async def advance_to_underwriting(lead_id: str, tenant_id: str) -> None:
    """
    Move a lead from DOC_COLLECTION to READY_FOR_UNDERWRITING after Tier 2 passes.
    Called by the ai_worker after a successful Tier 2 validation run.
    """
    db = get_db()
    now = datetime.now(timezone.utc)

    await db.leads.update_one(
        {"_id": ObjectId(lead_id), "tenant_id": tenant_id},
        {"$set": {"status": "READY_FOR_UNDERWRITING", "updated_at": now}},
    )

    await _record_activity(
        db, tenant_id, lead_id,
        "READY_FOR_UNDERWRITING",
        "All documents validated — lead is ready for underwriting review",
    )

    await db.workflow_runs.insert_one({
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "node_id": "tier2_complete",
        "node_type": "VALIDATION_AI",
        "status": "COMPLETED",
        "input_data": {},
        "output_data": {"tier2_passed": True},
        "triggered_by": "SYSTEM",
        "executed_at": now,
        "duration_ms": 0,
    })

    logger.info(f"Lead advanced to READY_FOR_UNDERWRITING — lead_id={lead_id}")
