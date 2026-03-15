"""
Gmail Push Notification Service
---------------------------------
Uses Gmail API + Google Cloud Pub/Sub for real-time inbound email processing.

Flow:
  1. On startup, setup_watch() registers Gmail push notifications.
     Gmail publishes to the Pub/Sub topic whenever new mail arrives.
  2. Pub/Sub instantly POSTs to /api/v1/webhooks/gmail-push.
  3. Webhook calls process_new_messages(historyId) which fetches new messages
     via gmail.users.history.list() and processes attachments.
  4. watch() expires every 7 days — renewed automatically every 6 days.

Required env vars:
  GMAIL_SERVICE_ACCOUNT_JSON  — full contents of service account JSON key
  GMAIL_PUBSUB_TOPIC          — e.g. projects/gain-mvp/topics/gmail-inbound
  GMAIL_INBOUND_ADDRESS       — default: demo.docs@unlockgain.com
"""

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("gain.gmail")

_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
_INBOUND_ADDRESS = os.getenv("GMAIL_INBOUND_ADDRESS", "demo.docs@unlockgain.com")
_PUBSUB_TOPIC = os.getenv("GMAIL_PUBSUB_TOPIC", "")
_STATE_COLLECTION = "system_state"
_HISTORY_KEY = "gmail_last_history_id"


def _is_configured() -> bool:
    return bool(os.getenv("GMAIL_SERVICE_ACCOUNT_JSON")) and bool(_PUBSUB_TOPIC)


def _build_service():
    raw = os.getenv("GMAIL_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=_SCOPES
        )
        return build("gmail", "v1", credentials=creds.with_subject(_INBOUND_ADDRESS), cache_discovery=False)
    except Exception as exc:
        logger.error(f"Gmail: failed to build service: {exc}")
        return None


async def setup_watch() -> Optional[int]:
    """Register Gmail push notifications. Returns baseline historyId or None."""
    if not _is_configured():
        return None

    loop = asyncio.get_event_loop()
    service = await loop.run_in_executor(None, _build_service)
    if not service:
        return None

    try:
        result = await loop.run_in_executor(
            None,
            lambda: service.users().watch(
                userId="me",
                body={"labelIds": ["INBOX"], "topicName": _PUBSUB_TOPIC},
            ).execute(),
        )
        history_id = int(result.get("historyId", 0))
        logger.info(f"Gmail watch registered — historyId={history_id}")

        from app.database import get_db
        db = get_db()
        await db[_STATE_COLLECTION].update_one(
            {"key": _HISTORY_KEY},
            {"$set": {"key": _HISTORY_KEY, "value": str(history_id), "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return history_id
    except Exception as exc:
        logger.error(f"Gmail watch setup failed: {exc}")
        return None


async def get_last_history_id() -> Optional[str]:
    try:
        from app.database import get_db
        db = get_db()
        doc = await db[_STATE_COLLECTION].find_one({"key": _HISTORY_KEY})
        return doc["value"] if doc else None
    except Exception:
        return None


async def save_history_id(history_id: str):
    try:
        from app.database import get_db
        db = get_db()
        await db[_STATE_COLLECTION].update_one(
            {"key": _HISTORY_KEY},
            {"$set": {"key": _HISTORY_KEY, "value": str(history_id), "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as exc:
        logger.warning(f"Gmail: failed to save historyId: {exc}")


async def process_new_messages(notification_history_id: str):
    """Called by /webhooks/gmail-push. Fetches and processes new messages."""
    if not _is_configured():
        return

    last_id = await get_last_history_id()
    if not last_id:
        await save_history_id(notification_history_id)
        logger.info(f"Gmail: storing baseline historyId={notification_history_id}")
        return

    loop = asyncio.get_event_loop()
    service = await loop.run_in_executor(None, _build_service)
    if not service:
        return

    try:
        history_result = await loop.run_in_executor(
            None,
            lambda: service.users().history().list(
                userId="me",
                startHistoryId=last_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute(),
        )
    except Exception as exc:
        logger.error(f"Gmail history.list failed: {exc}")
        return

    history_records = history_result.get("history", [])
    new_history_id = history_result.get("historyId", notification_history_id)

    if not history_records:
        await save_history_id(new_history_id)
        return

    msg_ids = []
    for record in history_records:
        for added in record.get("messagesAdded", []):
            msg = added.get("message", {})
            if "INBOX" in msg.get("labelIds", []):
                msg_ids.append(msg["id"])

    logger.info(f"Gmail push: {len(msg_ids)} new message(s)")

    from app.database import get_db
    from app.services.storage_service import upload_file, build_s3_key

    db = get_db()
    now = datetime.now(timezone.utc)

    for msg_id in msg_ids:
        await _process_single_message(loop, service, db, msg_id, now, upload_file, build_s3_key)

    await save_history_id(new_history_id)


async def _process_single_message(loop, service, db, msg_id, now, upload_file, build_s3_key):
    try:
        msg = await loop.run_in_executor(
            None,
            lambda mid=msg_id: service.users().messages().get(
                userId="me", id=mid, format="full"
            ).execute(),
        )

        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        sender_raw = headers.get("from", "")
        subject = headers.get("subject", "(no subject)")

        sender_email = sender_raw
        if "<" in sender_raw:
            sender_email = sender_raw.split("<")[-1].rstrip(">").strip()
        sender_email = sender_email.lower().strip()

        # Prefer the most recent lead in DOC_COLLECTION status (same email on multiple leads)
        candidates = await db.leads.find({"email": sender_email}).sort("created_at", -1).to_list(10)
        lead = None
        if candidates:
            for c in candidates:
                if c.get("status") == "DOC_COLLECTION":
                    lead = c
                    break
            if not lead:
                lead = candidates[0]
            logger.info(f"Gmail: matched lead_id={lead['_id']} status={lead.get('status')} (from {len(candidates)} candidates)")
        if not lead:
            logger.info(f"Gmail: no lead for {sender_email} — skipping")
            return

        lead_id = str(lead["_id"])
        tenant_id = lead["tenant_id"]

        await db.activity_feed.insert_one({
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "event_type": "EMAIL_RECEIVED",
            "message": f"Email received from {sender_email}: {subject[:80]}",
            "subject": subject,
            "from_email": sender_email,
            "created_at": now,
        })

        parts = _get_all_parts(msg["payload"])
        attachment_names = []
        processed = 0

        for part in parts:
            filename = part.get("filename", "").strip()
            body = part.get("body", {})
            if not filename or not body:
                continue

            try:
                if "attachmentId" in body:
                    att = await loop.run_in_executor(
                        None,
                        lambda mid=msg_id, aid=body["attachmentId"]: (
                            service.users().messages().attachments().get(
                                userId="me", messageId=mid, id=aid
                            ).execute()
                        ),
                    )
                    file_bytes = base64.urlsafe_b64decode(att["data"])
                elif "data" in body:
                    file_bytes = base64.urlsafe_b64decode(body["data"])
                else:
                    continue

                s3_key = build_s3_key(tenant_id, lead_id, filename)
                await upload_file(file_bytes, s3_key)

                attachment_names.append({"filename": filename, "size": len(file_bytes), "s3_key": s3_key})

                # Process document directly (bypass Celery eager mode)
                from app.routes.webhooks import _run_doc_processing_pipeline
                asyncio.create_task(
                    _run_doc_processing_pipeline(
                        s3_key, lead_id, tenant_id, filename,
                        len(file_bytes), "", channel="EMAIL",
                    )
                )
                processed += 1
                logger.info(f"Gmail: processing started for '{filename}' lead_id={lead_id}")

            except Exception as exc:
                logger.error(f"Gmail: failed to process '{filename}': {exc}")

        # Persist email message record
        await db.email_messages.insert_one({
            "lead_id": lead_id, "tenant_id": tenant_id,
            "direction": "INBOUND", "from_email": sender_email,
            "subject": subject, "body_text": "",
            "attachments": attachment_names, "received_at": now,
        })
        logger.info(f"Gmail: {processed} attachment(s) processed from {sender_email}")

    except Exception as exc:
        logger.error(f"Gmail: failed to process message {msg_id}: {exc}")


def _get_all_parts(payload: dict) -> list:
    parts = []
    if "parts" in payload:
        for part in payload["parts"]:
            parts.extend(_get_all_parts(part))
    else:
        parts.append(payload)
    return parts
