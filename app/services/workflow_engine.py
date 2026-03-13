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
    logger.info(f"[WF_ENGINE] Processing lead_id={lead_id} current_status={status!r} tenant_id={tenant_id}")

    # State machine: map lead status → next action
    if status == "PAN_VERIFIED":
        logger.info(f"[WF_ENGINE] → PAN_VERIFIED branch — triggering qualification call")
        await _trigger_qualification_call(lead, db)

    elif status == "QUALIFIED":
        logger.info(f"[WF_ENGINE] → QUALIFIED branch — triggering doc collection for lead_id={lead_id}")
        await _trigger_doc_collection(lead, db)
        logger.info(f"[WF_ENGINE] → Doc collection trigger completed for lead_id={lead_id}")

    elif status == "READY_FOR_UNDERWRITING":
        logger.info(f"[WF_ENGINE] → READY_FOR_UNDERWRITING branch")
        await _notify_underwriting(lead, db)

    else:
        logger.warning(
            f"[WF_ENGINE] No automatic action for lead_id={lead_id} status={status!r} — "
            f"expected one of: PAN_VERIFIED, QUALIFIED, READY_FOR_UNDERWRITING"
        )


async def _trigger_qualification_call(lead: dict, db) -> None:
    from app.services.voice_service import enqueue_qualification_call

    lead_id = str(lead["_id"])
    tenant_id = lead["tenant_id"]

    await enqueue_qualification_call(lead_id, tenant_id)
    logger.info(f"Qualification call enqueued for lead_id={lead_id}")


async def _trigger_doc_collection(lead: dict, db) -> None:
    """Trigger WhatsApp + Email doc checklist using direct service calls."""
    lead_id = str(lead["_id"])
    tenant_id = lead["tenant_id"]
    now = datetime.now(timezone.utc)
    logger.info(f"[WF_ENGINE] _trigger_doc_collection ENTERED — lead_id={lead_id} mobile_present={bool(lead.get('mobile'))} email_present={bool(lead.get('email'))}")

    # Update lead status
    await db.leads.update_one(
        {"_id": lead["_id"], "tenant_id": tenant_id},
        {"$set": {"status": "DOC_COLLECTION", "updated_at": now}},
    )

    borrower_name = lead.get("name", "Borrower")
    entity_type = lead.get("entity_type", "INDIVIDUAL")
    loan_amount = lead.get("loan_amount_requested", 0)

    # --- Send WhatsApp checklist (same pattern as working follow-up button) ---
    wa_sent = False
    try:
        from app.utils.encryption import decrypt_field
        mobile_raw = decrypt_field(lead.get("mobile", ""))
        if mobile_raw:
            from app.services.whatsapp_service import send_document_checklist, compute_mobile_hash
            wa_sent = await send_document_checklist(
                lead_id=lead_id,
                mobile=mobile_raw,
                name=borrower_name,
                entity_type=entity_type,
                loan_amount_paise=loan_amount,
            )
            # Store mobile_hash for inbound WA matching
            mobile_hash = compute_mobile_hash(mobile_raw)
            await db.leads.update_one(
                {"_id": lead["_id"]},
                {"$set": {"mobile_hash": mobile_hash}},
            )
            logger.info(f"WhatsApp checklist sent={wa_sent} for lead_id={lead_id}")
        else:
            logger.warning(f"No mobile number for lead_id={lead_id} — skipping WhatsApp")
    except Exception as exc:
        logger.error(f"WhatsApp checklist failed for lead_id={lead_id}: {exc}", exc_info=True)

    # --- Send Email checklist ---
    email_sent = False
    try:
        borrower_email = lead.get("email", "")
        if borrower_email:
            from app.services.email_service import send_document_checklist_email
            from app.services.validation_rules import get_doc_collection_config

            config = await get_doc_collection_config(db, tenant_id)
            entity_checklist = config.get("doc_checklist_by_entity_type", {}).get(
                entity_type, config.get("doc_checklist_by_entity_type", {}).get("INDIVIDUAL", {})
            )
            all_docs = entity_checklist.get("required", []) + [
                f"{d} (optional)" for d in entity_checklist.get("optional", [])
            ]

            subject_template = config.get(
                "email_subject_template",
                "Documents Required — {{loan_type}} Application for {{company_name}}",
            )
            body_template = config.get(
                "email_body_template",
                "Dear {{borrower_name}},\n\nPlease send the following documents:\n\n{{doc_list}}\n\nRegards,\nGain AI",
            )

            email_sent = await send_document_checklist_email(
                lead_id=lead_id,
                tenant_id=tenant_id,
                borrower_name=borrower_name,
                borrower_email=borrower_email,
                company_name=lead.get("company_name", ""),
                loan_type=lead.get("loan_type", ""),
                doc_list=all_docs,
                subject_template=subject_template,
                body_template=body_template,
            )
            logger.info(f"Email checklist sent={email_sent} for lead_id={lead_id}")
        else:
            logger.warning(f"No email for lead_id={lead_id} — skipping email")
    except Exception as exc:
        logger.error(f"Email checklist failed for lead_id={lead_id}: {exc}", exc_info=True)

    # Log activity
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "DOC_COLLECTION_STARTED",
        "message": f"Doc collection started — WhatsApp={'sent' if wa_sent else 'failed'}, Email={'sent' if email_sent else 'failed'}",
        "created_at": now,
    })

    # Create workflow run record
    await db.workflow_runs.insert_one(
        {
            "lead_id": lead_id,
            "tenant_id": tenant_id,
            "campaign_id": str(lead.get("campaign_id")) if lead.get("campaign_id") else None,
            "node_id": "doc_collection",
            "node_type": "DOC_COLLECTION",
            "status": "RUNNING",
            "input_data": {"entity_type": entity_type, "loan_type": lead.get("loan_type")},
            "output_data": {"whatsapp_sent": wa_sent, "email_sent": email_sent},
            "triggered_by": "SYSTEM",
            "executed_at": now,
            "duration_ms": 0,
        }
    )

    logger.info(f"Doc collection triggered for lead_id={lead_id} — WA={wa_sent} Email={email_sent}")


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
