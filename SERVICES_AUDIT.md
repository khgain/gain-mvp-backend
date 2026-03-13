# Backend Services Layer Audit — Complete Service Map

**Generated:** 2026-03-14
**Scope:** `/sessions/clever-intelligent-gauss/mnt/gain-backend/app/services/` + routes + workers
**Purpose:** Wire up workflow engine connecting all pipeline pieces

---

## EXECUTION FLOW OVERVIEW

The system follows this pipeline:

```
Lead Created/PAN Verified → Qualification Call → Qualified → Doc Collection →
(WhatsApp/Email) → Document Upload → Classification → Extraction →
Tier 1 Validation → Email Status Update → [All Docs Pass T1?] →
Tier 2 Validation → Ready for Underwriting
```

---

## 1. CORE AI SERVICE
**File:** `app/services/ai_service.py`

### Functions

#### `classify_physical_file(s3_key, original_filename, extraction_prompt_additions="")`
- **Signature:** `(str, str, str="") → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Download file from S3, use Claude Haiku to classify document type
- **Returns:** `{"doc_type": str, "confidence": int, "ambiguity_type": str, "reasoning": str}`
- **Doc Types:** AADHAAR, PAN_CARD, BANK_STATEMENT, ITR, GST_CERT, GST_RETURN, AUDITED_PL, TITLE_DEED, PROPERTY_TAX, NOC, UDYAM, MOA, AOA, COI, ELECTRICITY_BILL, PASSPORT_PHOTO, PARTNERSHIP_DEED, OTHER
- **Ambiguity Types:** NORMAL (single doc), BUNDLED (multiple doc types), PARTIAL (incomplete doc)
- **Triggered by:** ai_worker.classify_document Celery task
- **Calls next:** LogicalDoc creation → extract_data (if NORMAL/PARTIAL)

#### `extract_data(s3_key, original_filename, doc_type, fields_to_extract, extraction_prompt_additions="")`
- **Signature:** `(str, str, str, list[str], str="") → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Use Claude Haiku to extract structured fields from document
- **Returns:** `{field_name: value, ...}` with null for missing fields
- **Rules:** Dates in ISO format, monetary in INR (no symbols), averages for multi-month statements, account numbers last 4 digits only
- **Triggered by:** ai_worker.extract_document_data Celery task
- **Calls next:** run_tier1_rules

#### `evaluate_tier1_rule(rule, extracted_data, doc_type, file_size_bytes=None)`
- **Signature:** `(dict, dict, str, int=None) → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Evaluate a single per-document validation rule
- **Returns:** `{"passed": bool, "message": str}`
- **Special handling:** T1-007 (minimum file size) checked without Claude; all others use Claude Haiku
- **Triggered by:** run_tier1_rules (loops over all rules)
- **Calls next:** Individual result aggregation

#### `run_tier1_rules(doc_type, extracted_data, tier1_rules, file_size_bytes=None)`
- **Signature:** `(str, dict, list[dict], int=None) → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Run all Tier 1 rules applicable to a document
- **Returns:** `{"passed": bool, "rule_results": [...], "failed_rule_ids": [...]}`
- **Logic:** ALL rules must pass (AND logic)
- **Triggered by:** ai_worker.run_tier1_validation Celery task
- **Calls next:** Tier 2 check (if all required docs pass T1)

#### `run_tier2_rules(lead_summary, tier2_rules, validation_prompt_additions="")`
- **Signature:** `(dict, list[dict], str="") → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Cross-document consistency validation using Claude Sonnet
- **Returns:** `{"passed": bool, "rule_results": [...], "failed_rule_ids": [...]}`
- **Input lead_summary:** `{"lead_id", "borrower_name", "entity_type", "logical_docs": [{"doc_type", "extracted_data"}]}`
- **Model:** Claude Sonnet (not Haiku; more complex reasoning)
- **Triggered by:** ai_worker.run_tier2_validation Celery task (only when all mandatory docs pass T1)
- **Calls next:** workflow_engine.advance_to_underwriting (if passed)

---

## 2. WORKFLOW ENGINE
**File:** `app/services/workflow_engine.py`

### Functions

#### `process_lead(lead_id, tenant_id)`
- **Signature:** `async(str, str) → None`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Main entry point; reads lead status, determines next action, executes it
- **State machine logic:**
  - `PAN_VERIFIED` → trigger_qualification_call
  - `QUALIFIED` → trigger_doc_collection
  - `READY_FOR_UNDERWRITING` → notify_underwriting
  - Others → no action
- **Catches all exceptions** to prevent single-lead failure from crashing other leads
- **Triggered by:**
  - Lead creation (from routes/leads.py POST /leads)
  - Webhook completion (voice_service, when qualification call ends)
  - Scheduled retry (future enhancement)
- **Calls next:**
  - voice_service.enqueue_qualification_call
  - _trigger_doc_collection (WhatsApp + Email workers)
  - _notify_underwriting

#### `advance_to_underwriting(lead_id, tenant_id)`
- **Signature:** `async(str, str) → None`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Move lead from DOC_COLLECTION to READY_FOR_UNDERWRITING after Tier 2 passes
- **Side effects:**
  - Updates lead.status to READY_FOR_UNDERWRITING
  - Records activity_feed event
  - Creates workflow_runs audit record
- **Triggered by:** ai_worker._tier2_async (after tier2_passed == true)
- **Calls next:** N/A (terminal state until underwriting)

#### Helper functions:
- `_trigger_qualification_call`: Enqueues voice_worker.place_voice_call
- `_trigger_doc_collection`: Enqueues send_doc_checklist_whatsapp + send_doc_checklist_email
- `_notify_underwriting`: Logs activity_feed event (RM notification TBD)
- `_record_workflow_failure`: Audit logging for all exceptions
- `_record_activity`: Generic activity_feed logger

---

## 3. VALIDATION RULES SERVICE
**File:** `app/services/validation_rules.py`

### Functions

#### `get_validation_agent_config(db, tenant_id)`
- **Signature:** `async(Motor, str) → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Fetch VALIDATION_AI agent config from MongoDB
- **Returns:** `{tier1_rules: [...], tier2_rules: [...], on_tier1_failure_action, on_tier2_failure_action, ...}`
- **Fallback:** Empty defaults if agent not found
- **Triggered by:** ai_worker tier1/tier2 validation tasks
- **Calls next:** Rule execution in ai_service

#### `get_extraction_agent_config(db, tenant_id)`
- **Signature:** `async(Motor, str) → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Fetch EXTRACTION_AI agent config
- **Returns:** `{classification_confidence_threshold, extraction_fields_by_doc_type: {doc_type: [fields]}, ...}`
- **Triggered by:** ai_worker.classify_document, extract_document_data
- **Calls next:** Classification/extraction prompt building

#### `get_doc_collection_config(db, tenant_id)`
- **Signature:** `async(Motor, str) → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Fetch DOC_COLLECTION agent config
- **Returns:** `{doc_checklist_by_entity_type: {entity_type: {required: [...], optional: [...]}}}`
- **Triggered by:** whatsapp_worker checklist send, follow_up_service reminders
- **Calls next:** Message template building

#### `check_all_required_docs_passed(db, lead_id, tenant_id)`
- **Signature:** `async(Motor, str, str) → bool`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Check if all mandatory docs for lead have passed Tier 1
- **Logic:**
  1. Get entity_type from lead
  2. Get required doc types from DOC_COLLECTION config
  3. Query logical_docs with status in {TIER1_PASSED, HUMAN_REVIEWED}
  4. Return True only if all required types present
- **Triggered by:** ai_worker._tier1_async (after individual doc passes)
- **Calls next:** Queue Tier 2 if True

#### `build_tier2_lead_summary(db, lead_id, tenant_id)`
- **Signature:** `async(Motor, str, str) → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Assemble complete lead data for Tier 2 prompt
- **Returns:** `{lead_id, borrower_name, entity_type, loan_type, logical_docs: [{doc_type, extracted_data, completeness_status}]}`
- **Triggered by:** ai_worker._tier2_async
- **Calls next:** ai_service.run_tier2_rules

#### `notify_tier1_failure(db, lead_id, tenant_id, doc_type, failed_rule_ids, tier1_failure_templates)`
- **Signature:** `async(Motor, str, str, str, list, dict) → None`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Send WhatsApp notification to borrower for Tier 1 failures
- **Side effects:**
  - Decrypts mobile number
  - Sends WhatsApp message via whatsapp_service.send_text_message
  - Saves record to whatsapp_messages collection
- **Triggered by:** ai_worker._tier1_async (if on_tier1_failure_action == NOTIFY_BORROWER_AND_CONTINUE)
- **Calls next:** whatsapp_service.send_text_message

---

## 4. VOICE SERVICE
**File:** `app/services/voice_service.py`

### Functions

#### `enqueue_qualification_call(lead_id, tenant_id)`
- **Signature:** `async(str, str) → None`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** FastAPI entry point; enqueue Celery voice-call task
- **Side effects:**
  - Updates lead.status to CALL_SCHEDULED
  - Records activity_feed event
- **Triggered by:** FastAPI route leads.POST /leads/{id}/trigger-agent (manual) OR workflow_engine
- **Calls next:** voice_worker.place_voice_call (Celery task)

#### `trigger_outbound_call(lead_id, tenant_id)`
- **Signature:** `async(str, str) → Optional[str]`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Place outbound call via ElevenLabs Conversational AI API
- **API:** `POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call`
- **Payload:**
  - agent_id, agent_phone_number_id, to_number (E.164 format)
  - dynamic_variables: borrower_name, company_name, loan_type, loan_amount, lead_id, tenant_id
- **Returns:** conversation_id (string) or None
- **Side effects:**
  - Creates call_records document with status INITIATED
  - Updates lead.status to CALL_SCHEDULED
  - Records activity_feed event
- **Triggered by:** voice_worker.place_voice_call (Celery task running asyncio.run)
- **Calls next:** ElevenLabs webhook → process_call_completed

#### `should_retry_call(lead_id, attempt_number)`
- **Signature:** `(str, int) → bool`
- **Status:** FULLY IMPLEMENTED (simple)
- **Purpose:** Decide whether to retry a failed call
- **Logic:** Retry up to 3 times (attempt < 3)
- **Triggered by:** voice_worker retry logic
- **Calls next:** Re-trigger trigger_outbound_call OR fallback to WhatsApp

#### `process_call_completed(payload)`
- **Signature:** `async(dict) → None`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Handle ElevenLabs post-call webhook with full transcript
- **Payload structure:** `{conversation_id, data: {transcript: [...], duration, status, metadata: {lead_id, tenant_id}, analysis: {...}}}`
- **Processing steps:**
  1. Extract conversation_id, transcript, duration, status
  2. Normalize transcript (role, message, timestamp)
  3. Extract key data via _extract_from_transcript (Claude Haiku)
  4. Determine qualification_outcome (QUALIFIED | NOT_QUALIFIED | INCOMPLETE)
  5. Update call_records with full data + extracted_data + outcome
  6. Update lead.status based on outcome
  7. If QUALIFIED: trigger doc_collection via _trigger_doc_collection
- **Triggered by:** WAHA webhook /webhooks/elevenlabs/call-completed
- **Calls next:** workflow_engine.process_lead (implicitly via trigger_doc_collection) OR end

#### `process_call_status_update(payload)`
- **Signature:** `async(dict) → None`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Handle real-time ElevenLabs call status updates
- **Updates:** call_records.status to latest value
- **Triggered by:** WAHA webhook /webhooks/elevenlabs/call-status
- **Calls next:** None (status update only)

#### `_extract_from_transcript(transcript_raw)`
- **Signature:** `async(str) → tuple`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Use Claude Haiku to extract key loan data from call transcript
- **Returns:** `(extracted_data: dict, outcome: str)`
- **Extracted fields:** declaredTurnover, businessVintage, existingEmis, consentGiven, loanPurpose, callSummary
- **Outcome:** QUALIFIED | NOT_QUALIFIED | INCOMPLETE
- **Triggered by:** process_call_completed
- **Calls next:** Update call_records

#### Helper functions:
- `_get_decrypted_mobile(lead)`: Decrypt encrypted mobile number
- `_format_e164(mobile)`: Convert Indian mobile to E.164 format (+91XXXXXXXXXX)
- `_trigger_doc_collection(lead_id, tenant_id)`: Enqueue whatsapp_worker.send_doc_checklist_whatsapp

---

## 5. WHATSAPP SERVICE
**File:** `app/services/whatsapp_service.py`

### Functions

#### `send_document_checklist(lead_id, mobile, name, entity_type=None, loan_amount_paise=None)`
- **Signature:** `async(str, str, str, str=None, int=None) → bool`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Send initial document checklist via WAHA WhatsApp API
- **Returns:** True if sent, False if skipped/error
- **API:** `POST {WAHA_BASE_URL}/api/sendText`
- **Triggered by:** whatsapp_worker.send_doc_checklist_whatsapp (Celery task)
- **Calls next:** whatsapp_messages record logging

#### `send_reminder(lead_id, mobile, name, day, missing_count)`
- **Signature:** `async(str, str, str, int, int) → bool`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Send follow-up reminder on days 1, 3, 5, 7
- **Returns:** True if sent, False otherwise
- **Triggered by:** follow_up_service._send_reminder_for_day
- **Calls next:** whatsapp_messages record logging

#### `compute_mobile_hash(mobile)`
- **Signature:** `(str) → str`
- **Status:** FULLY IMPLEMENTED (helper)
- **Purpose:** Generate SHA256 hash of 10-digit mobile for indexing
- **Used for:** Finding lead by incoming WAHA sender (no decryption needed)
- **Triggered by:** whatsapp_worker._send_checklist_async, webhook handler
- **Calls next:** Database index lookup

#### `find_lead_by_whatsapp_number(db, tenant_id, sender_chat_id)`
- **Signature:** `async(Motor, str, str) → Optional[dict]`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Match incoming WAHA sender to lead using mobile_hash
- **Returns:** Lead document or None
- **Triggered by:** whatsapp_incoming webhook handler
- **Calls next:** Process inbound message/document

#### `send_text_message(lead_id, mobile, text)`
- **Signature:** `async(str, str, str) → bool`
- **Status:** FULLY IMPLEMENTED (low-level)
- **Purpose:** Send plain text WhatsApp message via WAHA
- **Triggered by:** validation_rules.notify_tier1_failure, follow_up_service reminders
- **Calls next:** whatsapp_messages logging

#### Helper functions:
- `_whatsapp_number(mobile)`: Convert 10-digit mobile to WAHA format (91XXXXXXXXXX@c.us)
- `_build_checklist_message(name, entity_type, loan_amount_paise)`: Format checklist message
- `_build_reminder_message(name, day, missing_count)`: Format reminder message

---

## 6. EMAIL SERVICE
**File:** `app/services/email_service.py`

### Functions

#### `send_document_checklist_email(lead_id, tenant_id, borrower_name, borrower_email, company_name, loan_type, doc_list, subject_template, body_template)`
- **Signature:** `async(str, str, str, str, str, str, list[str], str, str) → bool`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Send initial document checklist via SendGrid
- **Returns:** True if sent, False if skipped/error
- **API:** `POST https://api.sendgrid.com/v3/mail/send`
- **Template variables:** {{borrower_name}}, {{company_name}}, {{loan_type}}, {{doc_list}}
- **Triggered by:** whatsapp_worker.send_doc_checklist_email (Celery task)
- **Calls next:** activity_feed logging

#### `send_reminder_email(lead_id, borrower_name, borrower_email, company_name, pending_docs, day)`
- **Signature:** `async(str, str, str, str, list[str], int) → bool`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Send follow-up reminder email on days 1, 3, 5, 7
- **Returns:** True if sent, False otherwise
- **Labels:** Day 1 = "Gentle Reminder", Day 3 = "Reminder", Day 5 = "Important Reminder", Day 7 = "Final Reminder"
- **Triggered by:** follow_up_service._send_reminder_for_day
- **Calls next:** None (end of reminder flow)

#### `send_doc_status_update_email(lead_id, tenant_id)`
- **Signature:** `async(str, str) → bool`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Send document status update email after each Tier 1 validation
- **Content:** Categorizes docs as:
  - ✅ Received & Verified (TIER1_PASSED, TIER2_PASSED, HUMAN_APPROVED)
  - ❌ Issues Found (TIER1_FAILED with specific reasons)
  - ⏳ Still Required (not yet received)
- **Final state:** If all docs verified, sends confirmation; otherwise requests resubmission
- **Triggered by:** ai_worker._tier1_async (after every tier1 result)
- **Calls next:** activity_feed logging

#### `log_outbound_email(db, lead_id, tenant_id, to_email, subject)`
- **Signature:** `async(Motor, str, str, str, str) → None`
- **Status:** FULLY IMPLEMENTED (audit helper)
- **Purpose:** Record outbound email in activity_feed
- **Triggered by:** Email send functions
- **Calls next:** None (audit logging only)

#### Helper functions:
- `_inject(template, variables)`: Simple template variable substitution
- `_fmt_text(docs)`: Format doc list as numbered text
- `_fmt_html(docs)`: Format doc list as HTML ordered list

---

## 7. FOLLOW-UP SERVICE
**File:** `app/services/follow_up_service.py`

### Functions

#### `check_and_send_reminders()`
- **Signature:** `async() → dict`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Find all leads in DOC_COLLECTION status and send overdue reminders
- **Returns:** `{sent: int, escalated: int}`
- **Schedule:** Called hourly via Celery beat (or on-demand)
- **Triggered by:** Celery periodic task (TBD: add schedule config)
- **Calls next:** _process_lead_reminders for each lead

#### Helper functions:
- `_process_lead_reminders(db, lead, now)`: Process reminders for single lead
- `_get_sent_reminder_days(db, lead_id, tenant_id)`: Check which reminder days already sent (by querying whatsapp_messages + activity_feed)
- `_send_reminder_for_day(db, lead, lead_id, tenant_id, day, now)`: Send both WhatsApp + email reminders for given day
- `_escalate_to_rm(db, lead, lead_id, tenant_id, now)`: Log escalation event after day 7
- `_count_missing_docs(db, lead_id, tenant_id, entity_type)`: Count required docs not yet submitted

**State tracking:** Uses whatsapp_messages and activity_feed collections to avoid duplicate sends

---

## 8. STORAGE SERVICE
**File:** `app/services/storage_service.py`

### Functions

#### `upload_file(file_bytes, s3_key, content_type="application/octet-stream")`
- **Signature:** `async(bytes, str, str="...") → str`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Upload bytes to S3 with encryption
- **Returns:** s3_key on success
- **S3 settings:** AES256 encryption, bucket from settings.S3_BUCKET_NAME
- **Triggered by:**
  - gmail_service (email attachments)
  - whatsapp_worker (whatsapp documents)
  - routes/documents.py (via presigned URL)
- **Calls next:** Classification workflow (from documents route)

#### `generate_view_url(s3_key, tenant_id, lead_tenant_id, expiry_seconds=3600)`
- **Signature:** `async(str, str, str, int=3600) → str`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Generate 1-hour presigned GET URL for viewing document
- **Tenant verification:** Checks tenant_id ownership before generating
- **Returns:** Presigned URL
- **Triggered by:** routes/documents.py GET /leads/{id}/physical-files/{file_id}/view-url
- **Calls next:** Frontend download

#### `get_upload_presigned_url(s3_key, content_type="application/pdf", expiry_seconds=3600)`
- **Signature:** `async(str, str, int=3600) → str`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Generate 1-hour presigned PUT URL for browser direct upload
- **Returns:** Presigned URL
- **Triggered by:** routes/documents.py POST /leads/{id}/upload
- **Calls next:** Browser file upload → confirm_portal_upload endpoint

#### `stream_zip_from_s3_files(files)`
- **Signature:** `(list[dict]) → bytes`
- **Status:** IMPLEMENTED (sync version; async streaming TBD)
- **Purpose:** Assemble ZIP in memory with category subfolders
- **File structure:** `{category}/{idx}_{doc_type}.{ext}`
- **Categories:** KYC, Financials, Business, Property, Other
- **Triggered by:** routes/documents.py GET /leads/{id}/download-zip
- **Calls next:** Return ZIP bytes to FastAPI Response

#### Helper function:
- `build_s3_key(tenant_id, lead_id, filename)`: Build S3 path with timestamp
- `_get_s3_client()`: Create boto3 S3 client with credentials

---

## 9. ZIP SERVICE
**File:** `app/services/zip_service.py`

### Functions

#### `extract_and_upload_zip(zip_s3_key, parent_phys_file_id, lead_id, tenant_id)`
- **Signature:** `(str, str, str, str) → list[dict]`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Download ZIP from S3, extract allowed files, re-upload to S3
- **Returns:** `[{s3_key: str, filename: str, file_size_bytes: int}, ...]`
- **Allowed extensions:** .pdf, .jpg, .jpeg, .png
- **Max file size:** 50 MB per extracted file
- **File naming:** Preserves original names, sanitizes spaces
- **Triggered by:** ai_worker.extract_zip Celery task
- **Calls next:** PhysicalFile record creation for each extracted file

#### Helper function:
- `_ext_to_content_type(ext)`: Map extension to MIME type

---

## 10. PAN SERVICE
**File:** `app/services/pan_service.py`

### Functions

#### `mark_pan_verified(lead_id, tenant_id, verified_by_user_id, notes=None)`
- **Signature:** `async(str, str, str, str=None) → dict`
- **Status:** FULLY IMPLEMENTED (MVP placeholder)
- **Purpose:** MVP placeholder — manually mark PAN as verified (no external API call)
- **Returns:** Updated lead document
- **Side effects:**
  - Updates lead.status to PAN_VERIFIED
  - Records verified_by_user_id, verified_at, optional notes
- **Validation:** Raises ValueError if lead not found or already verified
- **Triggered by:** routes/leads.py POST /leads/{id}/verify-pan (manual ops action)
- **Calls next:** workflow_engine.process_lead (implicitly, via status change)
- **Note:** Real PAN API integration (Sandbox.co.in or Setu) to be added post-MVP

---

## 11. GMAIL SERVICE
**File:** `app/services/gmail_service.py`

### Functions

#### `setup_watch()`
- **Signature:** `async() → Optional[int]`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Register Gmail push notifications via Google Cloud Pub/Sub
- **Returns:** historyId or None
- **Setup:** Called at app startup (main.py lifespan hook)
- **Side effects:**
  - Creates system_state record with gmail_last_history_id
  - Gmail will POST to Pub/Sub topic on new mail
- **Triggered by:** app startup
- **Calls next:** Pub/Sub service

#### `process_new_messages(notification_history_id)`
- **Signature:** `async(str) → None`
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Called by Pub/Sub webhook; fetch new messages via Gmail API
- **Processing:**
  1. Get last stored historyId
  2. Fetch messages since last_id using history.list()
  3. Find lead by sender email
  4. Download attachments (PDFs, images)
  5. Upload to S3
  6. Enqueue process_whatsapp_document for each attachment
- **Triggered by:** Pub/Sub webhook /webhooks/gmail-push
- **Calls next:** process_whatsapp_document (Celery task)

#### `get_last_history_id()`, `save_history_id(history_id)`
- **Signature:** `async() → Optional[str]`, `async(str) → None`
- **Status:** FULLY IMPLEMENTED (helpers)
- **Purpose:** Persist Gmail historyId to track processed messages

#### Helper function:
- `_build_service()`: Create Google Gmail API service object
- `_process_single_message(...)`: Extract sender, subject, attachments; upload to S3
- `_get_all_parts(payload)`: Recursively extract all MIME parts from email

---

## WORKERS (CELERY TASKS)

**File:** `app/workers/ai_worker.py`

### Tasks

#### `classify_document(phys_file_id, tenant_id)` ← Queue: ai-classification
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Classify a physical file
- **Steps:**
  1. Fetch PhysicalFile → get s3_key, filename
  2. Call ai_service.classify_physical_file()
  3. If confidence < threshold OR type = OTHER OR ambiguity = BUNDLED: mark NEEDS_HUMAN_REVIEW
  4. Else: create LogicalDoc → enqueue extract_document_data
- **Retry:** max_retries=2, countdown=60s
- **Next:** extract_document_data (Celery task)

#### `extract_document_data(logical_doc_id, tenant_id)` ← Queue: ai-extraction
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Extract structured fields from logical document
- **Steps:**
  1. Fetch LogicalDoc → get doc_type, phys_file_ids
  2. Get extraction field list from ExtractionAI config
  3. Call ai_service.extract_data()
  4. Update LogicalDoc.extracted_data, status=EXTRACTED
  5. Enqueue run_tier1_validation
- **Retry:** max_retries=2, countdown=60s
- **Next:** run_tier1_validation (Celery task)

#### `run_tier1_validation(logical_doc_id, tenant_id)` ← Queue: ai-tier1
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Run per-document validation rules
- **Steps:**
  1. Get VALIDATION_AI config (tier1_rules, on_failure_action)
  2. Call ai_service.run_tier1_rules()
  3. Update LogicalDoc.tier1_validation, status = TIER1_PASSED or TIER1_FAILED
  4. If failed: notify borrower via notify_tier1_failure + send doc status email
  5. If passed: send doc status email
  6. Check if all required docs passed via check_all_required_docs_passed()
  7. If yes: enqueue run_tier2_validation
- **Retry:** max_retries=2, countdown=60s
- **Next:** run_tier2_validation (Celery task) OR end

#### `run_tier2_validation(lead_id, tenant_id)` ← Queue: ai-tier2
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Run cross-document consistency validation
- **Steps:**
  1. Get VALIDATION_AI config (tier2_rules, on_failure_action)
  2. Build lead_summary with all extracted data
  3. Call ai_service.run_tier2_rules()
  4. Record activity_feed event
  5. If passed: advance_to_underwriting
  6. If failed: update lead.status = VALIDATION_FAILED (if BLOCK_LEAD action)
- **Retry:** max_retries=2, countdown=120s
- **Next:** workflow_engine.advance_to_underwriting OR end

#### `extract_zip(phys_file_id, tenant_id)` ← Queue: zip
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Extract files from ZIP archive
- **Steps:**
  1. Fetch PhysicalFile (ZIP)
  2. Call zip_service.extract_and_upload_zip()
  3. For each extracted file:
     - Create PhysicalFile record
     - Enqueue classify_document
  4. Update parent ZIP.status = PROCESSED
- **Retry:** max_retries=2, countdown=60s
- **Next:** classify_document (Celery task) for each file

---

**File:** `app/workers/voice_worker.py`

#### `place_voice_call(lead_id, tenant_id)` ← Queue: voice
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Place outbound ElevenLabs call
- **Steps:**
  1. Call voice_service.trigger_outbound_call()
  2. Return conversation_id or None
- **Retry:** max_retries=3, countdown=2 hours (for retry attempts)
- **Fallback:** After retries exhausted, fall back to WhatsApp checklist (TBD)
- **Next:** ElevenLabs webhook → process_call_completed

---

**File:** `app/workers/whatsapp_worker.py`

#### `send_doc_checklist_whatsapp(lead_id, tenant_id)` ← Queue: whatsapp
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Send initial document checklist via WhatsApp
- **Steps:**
  1. Fetch lead + DOC_COLLECTION config
  2. Decrypt mobile; compute + store mobile_hash on lead
  3. Build checklist message from config template
  4. Call whatsapp_service.send_document_checklist()
  5. Save to whatsapp_messages collection
- **Retry:** max_retries=3, countdown=5 min
- **Next:** End (reminder_service picks up after 1+ days)

#### `send_doc_checklist_email(lead_id, tenant_id)` ← Queue: email
- **Status:** FULLY IMPLEMENTED
- **Purpose:** Send initial document checklist via SendGrid email
- **Steps:**
  1. Fetch lead + DOC_COLLECTION config
  2. Get email, borrower_name, company_name, entity_type
  3. Build doc list + message from config template
  4. Call email_service.send_document_checklist_email()
- **Retry:** max_retries=3, countdown=5 min
- **Next:** End

#### `process_whatsapp_document(file_s3_key, lead_id, tenant_id, original_filename, file_size_bytes, waha_message_id)` ← Queue: whatsapp
- **Status:** PARTIALLY IMPLEMENTED (signature shown in gmail_service)
- **Purpose:** Process incoming WhatsApp document attachment
- **Steps:**
  1. Create PhysicalFile record with channel_received=WHATSAPP
  2. Enqueue classify_document
- **Triggered by:**
  - whatsapp_incoming webhook (direct attachment)
  - gmail_service.process_new_messages (email attachment)
- **Next:** classify_document (Celery task)

---

## ROUTES (HTTP ENTRY POINTS)

**File:** `app/routes/leads.py`

### Lead Management

#### `POST /leads` — Create single lead
- **Triggers:** process_lead (workflow engine) via workflow pattern
- **Calls next:** None directly; status determines workflow

#### `POST /leads/{id}/verify-pan` — Manual PAN verification
- **Calls:** pan_service.mark_pan_verified()
- **Side effect:** Updates lead.status = PAN_VERIFIED
- **Next:** process_lead → enqueue_qualification_call

#### `POST /leads/{id}/trigger-agent` — Manually trigger agent
- **Calls:** voice_service.enqueue_qualification_call() OR custom agent
- **Next:** voice_worker.place_voice_call (Celery task)

---

**File:** `app/routes/documents.py`

### Document Management

#### `POST /leads/{id}/upload` — Get presigned S3 PUT URL
- **Calls:** storage_service.get_upload_presigned_url()
- **Returns:** Presigned URL for browser direct upload

#### `POST /leads/{id}/physical-files/confirm` — Register browser-uploaded file
- **Calls:** Create PhysicalFile record
- **Enqueues:**
  - extract_zip (Celery) if ZIP
  - classify_document (Celery) if PDF/image
- **Next:** Classification workflow

#### `GET /leads/{id}/physical-files/{file_id}/view-url` — Get view URL
- **Calls:** storage_service.generate_view_url()
- **Returns:** 1-hour presigned GET URL

#### `POST /leads/{id}/logical-docs/group` — Group physical files into logical doc
- **Calls:** Create/update LogicalDoc record
- **Next:** Manual trigger to start extraction

---

**File:** `app/routes/webhooks.py`

### Webhook Receivers

#### `POST /webhooks/elevenlabs/call-completed` — ElevenLabs post-call webhook
- **Calls:** voice_service.process_call_completed()
- **Next:**
  - If QUALIFIED: workflow_engine → doc_collection
  - Else: End (lead status updated)

#### `POST /webhooks/elevenlabs/call-status` — ElevenLabs real-time status
- **Calls:** voice_service.process_call_status_update()
- **Next:** None (status update only)

#### `POST /webhooks/whatsapp/incoming` — WAHA WhatsApp incoming message
- **Steps:**
  1. Find lead by mobile_hash
  2. Save to whatsapp_messages collection
  3. If media: download, upload to S3, enqueue process_whatsapp_document
  4. If text = "HELP": re-enqueue send_doc_checklist_whatsapp
- **Next:** process_whatsapp_document (Celery task)

#### `POST /webhooks/gmail-push` — Google Pub/Sub email push
- **Calls:** gmail_service.process_new_messages()
- **Next:** process_whatsapp_document (Celery task) for each attachment

---

## CELERY CONFIGURATION

**File:** `app/celery_app.py`

### Queue Setup

- **Broker:** AWS SQS (or memory/eager in dev mode)
- **Result backend:** None (fire-and-forget; results in MongoDB)
- **Task serialization:** JSON
- **Timezone:** Asia/Kolkata
- **Acks:** Late (after task completes)
- **Prefetch:** 1 (one message per worker at a time)

### Queues

| Queue | Purpose | Status |
|-------|---------|--------|
| ai-classification | Classify physical files | Operational |
| ai-extraction | Extract document data | Operational |
| ai-tier1 | Tier 1 validation | Operational |
| ai-tier2 | Tier 2 validation | Operational |
| zip | ZIP extraction | Operational |
| voice | Outbound calls | Operational |
| whatsapp | WhatsApp messages | Operational |
| email | Email messages | Operational |

### Periodic Tasks (Celery Beat)

- **follow_up_service.check_and_send_reminders()** — Hourly reminder check (TBD: add schedule config)
- **gmail_service.setup_watch() renewal** — Every 6 days (TBD: implement scheduler)

---

## DATA FLOW DIAGRAM

```
[Lead Created]
    ↓
[workflow_engine.process_lead] (status = NEW)
    ↓
[Manual: ops clicks Verify PAN]
    ↓
[pan_service.mark_pan_verified] → lead.status = PAN_VERIFIED
    ↓
[workflow_engine.process_lead] → voice_service.enqueue_qualification_call
    ↓
[voice_worker.place_voice_call] (Celery)
    ↓
[ElevenLabs API] → outbound call
    ↓
[ElevenLabs webhook: call-completed]
    ↓
[voice_service.process_call_completed]
    ├─→ Extract transcript via Claude
    ├─→ If QUALIFIED: lead.status = QUALIFIED
    └─→ _trigger_doc_collection
        ├─→ send_doc_checklist_whatsapp (Celery)
        └─→ send_doc_checklist_email (Celery)

[Borrower sends documents]
    ├─→ [WhatsApp] → whatsapp_incoming webhook → process_whatsapp_document (Celery)
    ├─→ [Email] → Gmail push → gmail_service.process_new_messages → process_whatsapp_document (Celery)
    └─→ [Portal] → confirm_portal_upload → classify_document (Celery)

[classify_document] (Celery)
    ├─→ ai_service.classify_physical_file (Claude Haiku)
    ├─→ If low confidence/BUNDLED/OTHER: NEEDS_HUMAN_REVIEW (end)
    └─→ Else: create LogicalDoc → extract_document_data (Celery)

[extract_document_data] (Celery)
    ├─→ ai_service.extract_data (Claude Haiku)
    ├─→ Update LogicalDoc.extracted_data
    └─→ run_tier1_validation (Celery)

[run_tier1_validation] (Celery)
    ├─→ ai_service.run_tier1_rules (Claude Haiku per rule)
    ├─→ If TIER1_FAILED:
    │   ├─→ notify_tier1_failure (WhatsApp)
    │   └─→ send_doc_status_update_email
    ├─→ If TIER1_PASSED:
    │   └─→ send_doc_status_update_email
    └─→ check_all_required_docs_passed
        └─→ If YES: run_tier2_validation (Celery)

[run_tier2_validation] (Celery)
    ├─→ build_tier2_lead_summary
    ├─→ ai_service.run_tier2_rules (Claude Sonnet)
    ├─→ If TIER2_PASSED: advance_to_underwriting
    │   └─→ lead.status = READY_FOR_UNDERWRITING
    └─→ If TIER2_FAILED:
        └─→ lead.status = VALIDATION_FAILED (if BLOCK_LEAD action)

[follow_up_service] (Celery Beat hourly)
    ├─→ For leads in DOC_COLLECTION:
    │   ├─→ Send reminders on days 1, 3, 5, 7 (WhatsApp + Email)
    │   └─→ After day 7: escalate_to_rm (activity_feed event)
```

---

## MISSING IMPLEMENTATIONS / STUBS

1. **PAN Service:** No external API call; MVP placeholder only
2. **RM Notification:** _notify_underwriting logs to activity_feed; actual notification TBD
3. **ZIP Streaming:** storage_service.stream_zip_from_s3_files is sync; async streaming variant TBD
4. **Celery Beat Schedule:** follow_up_service and gmail_watch renewal need schedule definitions
5. **Document Grouping:** POST /leads/{id}/logical-docs/group endpoint logic TBD
6. **Human Review UI:** NEEDS_HUMAN_REVIEW path not fully wired to UI backend
7. **Voice Retry Fallback:** After voice retry exhaustion, should fall back to WhatsApp (not yet implemented)

---

## INTEGRATION CHECKLIST FOR WORKFLOW ENGINE WIRING

- [x] **Classify → Extract:** ai_worker.classify_document enqueues extract_document_data
- [x] **Extract → Tier 1:** ai_worker.extract_document_data enqueues run_tier1_validation
- [x] **Tier 1 → Tier 2:** run_tier1_validation checks all_required_docs_passed, enqueues run_tier2_validation
- [x] **Tier 2 → Underwriting:** run_tier2_validation calls advance_to_underwriting
- [x] **Voice → Doc Collection:** voice_service.process_call_completed triggers _trigger_doc_collection
- [x] **PAN Verified → Voice:** workflow_engine.process_lead triggers enqueue_qualification_call
- [x] **Document Upload → Classify:** confirm_portal_upload enqueues classify_document
- [x] **Email/WhatsApp → Classify:** process_new_messages, whatsapp_incoming enqueue classify_document
- [x] **Status Updates → Email:** ai_worker sends send_doc_status_update_email after T1
- [x] **Reminders:** follow_up_service.check_and_send_reminders (needs scheduler)
- [ ] **RM Escalation:** _notify_underwriting → actual RM notification (TBD)

---

## CONFIGURATION REQUIRED

Agents stored in MongoDB (agents collection) with type + config:

1. **EXTRACTION_AI** config:
   - classification_confidence_threshold: int (default 75)
   - extraction_fields_by_doc_type: {doc_type: [field_names]}
   - classification_prompt_additions: str
   - extraction_prompt_additions: str

2. **VALIDATION_AI** config:
   - tier1_rules: [{rule_id, rule_name, doc_type, enabled, custom_prompt, threshold}]
   - tier2_rules: [{rule_id, rule_name, custom_prompt}]
   - on_tier1_failure_action: "NOTIFY_BORROWER_AND_CONTINUE" | "BLOCK_LEAD"
   - on_tier2_failure_action: "FLAG_FOR_OPS_REVIEW" | "BLOCK_LEAD"
   - tier1_failure_message_templates: {rule_id: message_template}
   - validation_prompt_additions: str

3. **DOC_COLLECTION** config:
   - doc_checklist_by_entity_type: {entity_type: {required: [...], optional: [...]}}
   - whatsapp_checklist_template: str
   - reminder_whatsapp_template: str

4. **VOICE_AI** config:
   - (Used by ElevenLabs agent ID from settings)

---

## ENVIRONMENT VARIABLES REQUIRED

```
# AI
ANTHROPIC_API_KEY=sk-...

# Voice
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=...
ELEVENLABS_PHONE_NUMBER_ID=...
ELEVENLABS_WEBHOOK_SECRET=...

# WhatsApp
WAHA_BASE_URL=http://...
WAHA_API_KEY=...

# Email
SENDGRID_API_KEY=SG....

# Gmail
GMAIL_SERVICE_ACCOUNT_JSON={...}
GMAIL_PUBSUB_TOPIC=projects/.../topics/...

# S3
AWS_REGION=ap-south-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=...

# Celery
CELERY_TASK_ALWAYS_EAGER=false (set true for dev)
SQS_VOICE_QUEUE_URL=...
SQS_WHATSAPP_QUEUE_URL=...
SQS_EMAIL_QUEUE_URL=...
SQS_ZIP_QUEUE_URL=...
SQS_AI_CLASSIFICATION_QUEUE_URL=...
SQS_AI_EXTRACTION_QUEUE_URL=...
SQS_AI_TIER1_QUEUE_URL=...
SQS_AI_TIER2_QUEUE_URL=...
```
