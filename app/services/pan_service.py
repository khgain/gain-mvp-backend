"""
PAN Verification Service — MVP PLACEHOLDER.

In the MVP, PAN verification is a manual button click by the ops team.
NO external API is called. The endpoint simply records who clicked it and when,
updates the lead status to PAN_VERIFIED, and enqueues the qualification voice call.

A real PAN API integration (Sandbox.co.in or Setu) will be added post-MVP
when instructed. DO NOT implement the external API call here.
"""
from datetime import datetime, timezone
from bson import ObjectId

from app.database import get_db
from app.utils.logging import get_logger

logger = get_logger("pan_service")


async def mark_pan_verified(
    lead_id: str,
    tenant_id: str,
    verified_by_user_id: str,
    notes: str | None = None,
) -> dict:
    """
    MVP placeholder: mark a lead's PAN as verified without calling an external API.

    Returns the updated lead document.
    Raises ValueError if lead not found or already verified.
    """
    db = get_db()

    lead = await db.leads.find_one(
        {"_id": ObjectId(lead_id), "tenant_id": tenant_id}
    )
    if not lead:
        raise ValueError(f"Lead {lead_id} not found for tenant {tenant_id}")

    if lead.get("status") == "PAN_VERIFIED":
        raise ValueError("PAN already verified for this lead")

    now = datetime.now(timezone.utc)
    update = {
        "$set": {
            "status": "PAN_VERIFIED",
            "pan_verified_by": verified_by_user_id,
            "pan_verified_at": now,
            "updated_at": now,
        }
    }
    if notes:
        update["$set"]["pan_verification_notes"] = notes

    await db.leads.update_one(
        {"_id": ObjectId(lead_id), "tenant_id": tenant_id},
        update,
    )

    logger.info(
        f"PAN verified (MVP placeholder) — lead_id={lead_id} by user_id={verified_by_user_id}"
    )

    return await db.leads.find_one({"_id": ObjectId(lead_id), "tenant_id": tenant_id})
