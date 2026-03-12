"""
AI Worker — Celery tasks for document classification, extraction, and validation.

Queue routing:
  classify_document       → ai-classification
  extract_document_data   → ai-extraction
  run_tier1_validation    → ai-tier1
  run_tier2_validation    → ai-tier2
  extract_zip             → zip
"""
import asyncio
import logging
from datetime import datetime, timezone

from bson import ObjectId

from app.celery_app import celery_app

logger = logging.getLogger("gain.workers.ai")


# ---------------------------------------------------------------------------
# 1. Classify document
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.classify_document", queue="ai-classification", bind=True, max_retries=2)
def classify_document(self, phys_file_id: str, tenant_id: str) -> dict:
    """
    Classify a physical file using Claude.

    Steps:
      1. Fetch PhysicalFile record → get s3_key + filename
      2. Call ai_service.classify_physical_file()
      3. Handle NORMAL / BUNDLED / PARTIAL / low-confidence outcomes
      4. Create LogicalDoc record if NORMAL or PARTIAL
      5. Enqueue extract_document_data for NORMAL docs
    """
    logger.info(f"[AI WORKER] Classifying phys_file_id={phys_file_id}")
    try:
        result = asyncio.run(_classify_async(phys_file_id, tenant_id))
        return result
    except Exception as exc:
        logger.error(f"[AI WORKER] Classification failed for {phys_file_id}: {exc}")
        raise self.retry(exc=exc, countdown=60)


async def _classify_async(phys_file_id: str, tenant_id: str) -> dict:
    from app.database import get_db
    from app.services.ai_service import classify_physical_file
    from app.services.validation_rules import get_extraction_agent_config

    db = get_db()
    now = datetime.now(timezone.utc)

    phys_file = await db.phys_files.find_one(
        {"_id": ObjectId(phys_file_id), "tenant_id": tenant_id}
    )
    if not phys_file:
        logger.error(f"PhysFile not found: {phys_file_id}")
        return {"status": "error", "reason": "not_found"}

    # Update status to CLASSIFYING
    await db.phys_files.update_one(
        {"_id": ObjectId(phys_file_id)},
        {"$set": {"status": "CLASSIFYING", "updated_at": now}},
    )

    # Get extraction config for prompt additions
    extraction_config = await get_extraction_agent_config(db, tenant_id)
    prompt_additions = extraction_config.get("classification_prompt_additions", "")
    confidence_threshold = extraction_config.get("classification_confidence_threshold", 75)

    # Run classification
    result = classify_physical_file(
        s3_key=phys_file["s3_key"],
        original_filename=phys_file["original_filename"],
        extraction_prompt_additions=prompt_additions,
    )

    doc_type = result.get("doc_type", "OTHER")
    confidence = result.get("confidence", 0)
    ambiguity_type = result.get("ambiguity_type", "NORMAL")
    reasoning = result.get("reasoning", "")

    lead_id = phys_file["lead_id"]

    # Update PhysicalFile with classification results
    new_status = "CLASSIFIED"
    if confidence < confidence_threshold or doc_type == "OTHER":
        new_status = "NEEDS_HUMAN_REVIEW"
    elif ambiguity_type == "BUNDLED":
        new_status = "NEEDS_HUMAN_REVIEW"

    await db.phys_files.update_one(
        {"_id": ObjectId(phys_file_id)},
        {"$set": {
            "status": new_status,
            "ambiguity_type": ambiguity_type,
            "classification_confidence": confidence,
            "classification_reasoning": reasoning,
            "updated_at": now,
        }},
    )

    if new_status == "NEEDS_HUMAN_REVIEW":
        logger.info(
            f"[AI WORKER] Flagged for human review: {phys_file_id} "
            f"type={doc_type} confidence={confidence} ambiguity={ambiguity_type}"
        )
        return {"status": "needs_review", "doc_type": doc_type, "confidence": confidence}

    # NORMAL or PARTIAL: create LogicalDoc and enqueue extraction
    logical_doc_id = await _create_logical_doc(
        db, lead_id, tenant_id, phys_file_id, doc_type, ambiguity_type, now
    )

    if logical_doc_id:
        # Enqueue extraction
        extract_document_data.apply_async(
            kwargs={"logical_doc_id": logical_doc_id, "tenant_id": tenant_id},
            queue="ai-extraction",
        )

    return {"status": "classified", "doc_type": doc_type, "confidence": confidence, "logical_doc_id": logical_doc_id}


async def _create_logical_doc(
    db, lead_id: str, tenant_id: str, phys_file_id: str,
    doc_type: str, ambiguity_type: str, now: datetime
) -> str:
    """Create a LogicalDoc record for a classified physical file."""
    completeness = "PARTIAL" if ambiguity_type == "PARTIAL" else "COMPLETE"
    status = "ASSEMBLING" if ambiguity_type == "PARTIAL" else "READY_FOR_EXTRACTION"

    result = await db.logical_docs.insert_one({
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "doc_type": doc_type,
        "assembly_type": "SINGLE",
        "physical_file_ids": [phys_file_id],
        "completeness_status": completeness,
        "is_mandatory": True,
        "extracted_data": {},
        "tier1_validation": None,
        "status": status,
        "created_at": now,
        "updated_at": now,
    })
    logical_doc_id = str(result.inserted_id)

    # Link phys_file → logical_doc
    await db.phys_files.update_one(
        {"_id": ObjectId(phys_file_id)},
        {"$addToSet": {"logical_doc_ids": logical_doc_id}},
    )
    return logical_doc_id


# ---------------------------------------------------------------------------
# 2. Extract document data
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.extract_document_data", queue="ai-extraction", bind=True, max_retries=2)
def extract_document_data(self, logical_doc_id: str, tenant_id: str) -> dict:
    """Extract structured fields from a logical document using Claude."""
    logger.info(f"[AI WORKER] Extracting data from logical_doc_id={logical_doc_id}")
    try:
        result = asyncio.run(_extract_async(logical_doc_id, tenant_id))
        return result
    except Exception as exc:
        logger.error(f"[AI WORKER] Extraction failed for {logical_doc_id}: {exc}")
        raise self.retry(exc=exc, countdown=60)


async def _extract_async(logical_doc_id: str, tenant_id: str) -> dict:
    from app.database import get_db
    from app.services.ai_service import extract_data
    from app.services.validation_rules import get_extraction_agent_config

    db = get_db()
    now = datetime.now(timezone.utc)

    logical_doc = await db.logical_docs.find_one(
        {"_id": ObjectId(logical_doc_id), "tenant_id": tenant_id}
    )
    if not logical_doc:
        return {"status": "error", "reason": "not_found"}

    doc_type = logical_doc["doc_type"]
    lead_id = logical_doc["lead_id"]

    # Get primary physical file (first in list)
    phys_file_ids = logical_doc.get("physical_file_ids", [])
    if not phys_file_ids:
        return {"status": "error", "reason": "no_physical_files"}

    phys_file = await db.phys_files.find_one({"_id": ObjectId(phys_file_ids[0])})
    if not phys_file:
        return {"status": "error", "reason": "phys_file_not_found"}

    # Get field list from ExtractionAI config
    extraction_config = await get_extraction_agent_config(db, tenant_id)
    fields = extraction_config.get("extraction_fields_by_doc_type", {}).get(doc_type, [])
    prompt_additions = extraction_config.get("extraction_prompt_additions", "")

    # Update status to EXTRACTING
    await db.logical_docs.update_one(
        {"_id": ObjectId(logical_doc_id)},
        {"$set": {"status": "EXTRACTING", "updated_at": now}},
    )

    # Run extraction
    extracted = extract_data(
        s3_key=phys_file["s3_key"],
        original_filename=phys_file["original_filename"],
        doc_type=doc_type,
        fields_to_extract=fields,
        extraction_prompt_additions=prompt_additions,
    )

    # Update LogicalDoc with extracted data
    await db.logical_docs.update_one(
        {"_id": ObjectId(logical_doc_id)},
        {"$set": {"extracted_data": extracted, "status": "EXTRACTED", "updated_at": now}},
    )

    # Enqueue Tier 1 validation
    run_tier1_validation.apply_async(
        kwargs={"logical_doc_id": logical_doc_id, "tenant_id": tenant_id},
        queue="ai-tier1",
    )

    return {"status": "extracted", "doc_type": doc_type, "fields_extracted": len(extracted)}


# ---------------------------------------------------------------------------
# 3. Tier 1 validation
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.run_tier1_validation", queue="ai-tier1", bind=True, max_retries=2)
def run_tier1_validation(self, logical_doc_id: str, tenant_id: str) -> dict:
    """Run Tier 1 per-document validation rules using Claude."""
    logger.info(f"[AI WORKER] Tier 1 validation for logical_doc_id={logical_doc_id}")
    try:
        result = asyncio.run(_tier1_async(logical_doc_id, tenant_id))
        return result
    except Exception as exc:
        logger.error(f"[AI WORKER] Tier 1 failed for {logical_doc_id}: {exc}")
        raise self.retry(exc=exc, countdown=60)


async def _tier1_async(logical_doc_id: str, tenant_id: str) -> dict:
    from app.database import get_db
    from app.services.ai_service import run_tier1_rules
    from app.services.validation_rules import (
        get_validation_agent_config,
        check_all_required_docs_passed,
        notify_tier1_failure,
    )

    db = get_db()
    now = datetime.now(timezone.utc)

    logical_doc = await db.logical_docs.find_one(
        {"_id": ObjectId(logical_doc_id), "tenant_id": tenant_id}
    )
    if not logical_doc:
        return {"status": "error", "reason": "not_found"}

    doc_type = logical_doc["doc_type"]
    lead_id = logical_doc["lead_id"]
    extracted_data = logical_doc.get("extracted_data", {})

    # Get file size from primary phys_file
    file_size_bytes = None
    phys_file_ids = logical_doc.get("physical_file_ids", [])
    if phys_file_ids:
        pf = await db.phys_files.find_one({"_id": ObjectId(phys_file_ids[0])})
        if pf:
            file_size_bytes = pf.get("file_size_bytes")

    # Get validation config
    val_config = await get_validation_agent_config(db, tenant_id)
    tier1_rules = val_config.get("tier1_rules", [])
    on_failure_action = val_config.get("on_tier1_failure_action", "NOTIFY_BORROWER_AND_CONTINUE")
    failure_templates = val_config.get("tier1_failure_message_templates", {})

    # Update status
    await db.logical_docs.update_one(
        {"_id": ObjectId(logical_doc_id)},
        {"$set": {"status": "TIER1_VALIDATING", "updated_at": now}},
    )

    # Run rules
    validation_result = run_tier1_rules(
        doc_type=doc_type,
        extracted_data=extracted_data,
        tier1_rules=tier1_rules,
        file_size_bytes=file_size_bytes,
    )

    tier1_passed = validation_result["passed"]
    new_status = "TIER1_PASSED" if tier1_passed else "TIER1_FAILED"

    await db.logical_docs.update_one(
        {"_id": ObjectId(logical_doc_id)},
        {"$set": {
            "tier1_validation": {
                "passed": tier1_passed,
                "rule_results": validation_result["rule_results"],
            },
            "tier1_validated_at": now,
            "status": new_status,
            "updated_at": now,
        }},
    )

    logger.info(
        f"[AI WORKER] Tier 1 {new_status} for logical_doc_id={logical_doc_id} "
        f"doc_type={doc_type}"
    )

    if not tier1_passed:
        failed_rule_ids = validation_result.get("failed_rule_ids", [])
        if on_failure_action == "NOTIFY_BORROWER_AND_CONTINUE":
            await notify_tier1_failure(
                db, lead_id, tenant_id, doc_type, failed_rule_ids, failure_templates
            )
        # Send email status update showing what failed and what's still missing
        try:
            from app.services.email_service import send_doc_status_update_email
            await send_doc_status_update_email(lead_id=lead_id, tenant_id=tenant_id)
        except Exception as exc:
            logger.error(f"[AI WORKER] Doc status email failed after T1 failure: {exc}")
        return {"status": "tier1_failed", "failed_rules": failed_rule_ids}

    # Check if all required docs have passed → trigger Tier 2
    all_passed = await check_all_required_docs_passed(db, lead_id, tenant_id)

    # Send email status update after every tier 1 pass
    try:
        from app.services.email_service import send_doc_status_update_email
        await send_doc_status_update_email(lead_id=lead_id, tenant_id=tenant_id)
    except Exception as exc:
        logger.error(f"[AI WORKER] Doc status email failed after T1 pass: {exc}")

    if all_passed:
        run_tier2_validation.apply_async(
            kwargs={"lead_id": lead_id, "tenant_id": tenant_id},
            queue="ai-tier2",
        )
        logger.info(f"[AI WORKER] All required docs passed T1 — Tier 2 queued for lead_id={lead_id}")

    return {"status": "tier1_passed", "doc_type": doc_type}


# ---------------------------------------------------------------------------
# 4. Tier 2 validation
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.run_tier2_validation", queue="ai-tier2", bind=True, max_retries=2)
def run_tier2_validation(self, lead_id: str, tenant_id: str) -> dict:
    """Run Tier 2 cross-document consistency validation via Claude."""
    logger.info(f"[AI WORKER] Tier 2 validation for lead_id={lead_id}")
    try:
        result = asyncio.run(_tier2_async(lead_id, tenant_id))
        return result
    except Exception as exc:
        logger.error(f"[AI WORKER] Tier 2 failed for lead_id={lead_id}: {exc}")
        raise self.retry(exc=exc, countdown=120)


async def _tier2_async(lead_id: str, tenant_id: str) -> dict:
    from app.database import get_db
    from app.services.ai_service import run_tier2_rules
    from app.services.validation_rules import (
        get_validation_agent_config,
        build_tier2_lead_summary,
    )
    from app.services.workflow_engine import advance_to_underwriting

    db = get_db()
    now = datetime.now(timezone.utc)

    val_config = await get_validation_agent_config(db, tenant_id)
    tier2_rules = val_config.get("tier2_rules", [])
    on_failure_action = val_config.get("on_tier2_failure_action", "FLAG_FOR_OPS_REVIEW")
    prompt_additions = val_config.get("validation_prompt_additions", "")

    lead_summary = await build_tier2_lead_summary(db, lead_id, tenant_id)

    result = run_tier2_rules(lead_summary, tier2_rules, prompt_additions)
    tier2_passed = result["passed"]

    # Store Tier 2 results in activity_feed
    await db.activity_feed.insert_one({
        "tenant_id": tenant_id,
        "lead_id": lead_id,
        "event_type": "TIER2_VALIDATION_COMPLETE",
        "message": f"Tier 2 validation {'PASSED' if tier2_passed else 'FAILED'}",
        "metadata": {"rule_results": result.get("rule_results", [])},
        "created_at": now,
    })

    if tier2_passed:
        # Advance lead to READY_FOR_UNDERWRITING
        await advance_to_underwriting(lead_id, tenant_id)
        logger.info(f"[AI WORKER] Tier 2 PASSED — lead_id={lead_id} → READY_FOR_UNDERWRITING")
        return {"status": "tier2_passed", "lead_id": lead_id}
    else:
        failed = result.get("failed_rule_ids", [])
        logger.warning(
            f"[AI WORKER] Tier 2 FAILED — lead_id={lead_id} failed_rules={failed}"
        )
        if on_failure_action == "BLOCK_LEAD":
            await db.leads.update_one(
                {"_id": ObjectId(lead_id), "tenant_id": tenant_id},
                {"$set": {"status": "VALIDATION_FAILED", "updated_at": now}},
            )
        # Otherwise FLAG_FOR_OPS_REVIEW — lead stays in current status, ops team sees it
        return {"status": "tier2_failed", "failed_rules": failed}


# ---------------------------------------------------------------------------
# 5. ZIP extraction
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.extract_zip", queue="zip", bind=True, max_retries=2)
def extract_zip(self, phys_file_id: str, tenant_id: str) -> dict:
    """Extract files from a ZIP archive and enqueue each for classification."""
    logger.info(f"[AI WORKER] ZIP extraction for phys_file_id={phys_file_id}")
    try:
        result = asyncio.run(_extract_zip_async(phys_file_id, tenant_id))
        return result
    except Exception as exc:
        logger.error(f"[AI WORKER] ZIP extraction failed for {phys_file_id}: {exc}")
        raise self.retry(exc=exc, countdown=60)


async def _extract_zip_async(phys_file_id: str, tenant_id: str) -> dict:
    from app.database import get_db
    from app.services.zip_service import extract_and_upload_zip

    db = get_db()
    now = datetime.now(timezone.utc)

    phys_file = await db.phys_files.find_one(
        {"_id": ObjectId(phys_file_id), "tenant_id": tenant_id}
    )
    if not phys_file:
        return {"status": "error", "reason": "not_found"}

    lead_id = phys_file["lead_id"]

    # Extract ZIP → get list of child files
    extracted_files = extract_and_upload_zip(
        zip_s3_key=phys_file["s3_key"],
        parent_phys_file_id=phys_file_id,
        lead_id=lead_id,
        tenant_id=tenant_id,
    )

    child_ids = []
    for file_info in extracted_files:
        # Create PhysicalFile record for each extracted file
        child_doc = {
            "lead_id": lead_id,
            "tenant_id": tenant_id,
            "original_filename": file_info["filename"],
            "channel_received": "ZIP_EXTRACTED",
            "parent_zip_id": phys_file_id,
            "s3_key": file_info["s3_key"],
            "file_type": _ext_to_file_type(file_info["filename"]),
            "file_size_bytes": file_info["file_size_bytes"],
            "status": "RECEIVED",
            "logical_doc_ids": [],
            "created_at": now,
            "updated_at": now,
        }
        res = await db.phys_files.insert_one(child_doc)
        child_id = str(res.inserted_id)
        child_ids.append(child_id)

        # Enqueue classification for each extracted file
        classify_document.apply_async(
            kwargs={"phys_file_id": child_id, "tenant_id": tenant_id},
            queue="ai-classification",
        )

    # Update parent ZIP file status
    await db.phys_files.update_one(
        {"_id": ObjectId(phys_file_id)},
        {"$set": {"status": "PROCESSED", "updated_at": now}},
    )

    logger.info(
        f"[AI WORKER] ZIP extracted {len(extracted_files)} files from "
        f"phys_file_id={phys_file_id}"
    )
    return {"status": "extracted", "child_count": len(extracted_files), "child_ids": child_ids}


def _ext_to_file_type(filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "other"
    return {"pdf": "PDF", "jpg": "JPG", "jpeg": "JPG", "png": "PNG"}.get(ext, "OTHER")
