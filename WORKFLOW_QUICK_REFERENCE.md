# Workflow Quick Reference — Service Dependency Map

## Service Entry Points

### From FastAPI Routes

```
POST /leads/{id}/verify-pan
  └→ pan_service.mark_pan_verified()
     └→ Sets status = PAN_VERIFIED
        └→ workflow_engine.process_lead() [triggered implicitly]
           └→ voice_service.enqueue_qualification_call()
              └→ voice_worker.place_voice_call (Celery)

POST /leads/{id}/trigger-agent
  └→ voice_service.enqueue_qualification_call()
     └→ voice_worker.place_voice_call (Celery)

POST /leads/{id}/upload + /physical-files/confirm
  └→ storage_service.get_upload_presigned_url()
  └→ Create PhysicalFile record
     └→ classify_document (Celery) OR extract_zip (Celery)
```

### From Webhooks

```
POST /webhooks/elevenlabs/call-completed
  └→ voice_service.process_call_completed()
     ├→ _extract_from_transcript() [Claude Haiku]
     ├→ Update call_records + lead status
     └→ _trigger_doc_collection() [if QUALIFIED]
        ├→ send_doc_checklist_whatsapp (Celery)
        └→ send_doc_checklist_email (Celery)

POST /webhooks/whatsapp/incoming
  └→ Find lead by mobile_hash
  ├→ Save whatsapp_message record
  └→ If media: process_whatsapp_document (Celery)

POST /webhooks/gmail-push
  └→ gmail_service.process_new_messages()
     └→ For each attachment: process_whatsapp_document (Celery)
```

---

## Classification Pipeline

```
┌─ INPUT: Physical file (PDF/image) via WhatsApp, Email, or Portal
│
├─→ classify_document (Celery: ai-classification queue)
│   └─→ ai_service.classify_physical_file() [Claude Haiku]
│       └─ Returns: {doc_type, confidence, ambiguity_type, reasoning}
│
├─ DECISION: Is classification valid?
│   │
│   ├─ No (low confidence OR doc_type=OTHER OR ambiguity=BUNDLED)
│   │   └─→ phys_file.status = NEEDS_HUMAN_REVIEW [END]
│   │
│   └─ Yes (confidence ≥ threshold AND doc_type ≠ OTHER AND ambiguity ≠ BUNDLED)
│       └─→ Create LogicalDoc record
│           └─→ extract_document_data (Celery: ai-extraction queue)
```

---

## Extraction Pipeline

```
┌─ INPUT: LogicalDoc (after classification)
│
├─→ extract_document_data (Celery: ai-extraction queue)
│   ├─ Fetch ExtractionAI config: fields_to_extract[doc_type]
│   ├─ ai_service.extract_data() [Claude Haiku]
│   └─ Returns: {field_name: extracted_value, ...}
│
├─→ Update LogicalDoc.extracted_data
└─→ run_tier1_validation (Celery: ai-tier1 queue)
```

---

## Tier 1 Validation Pipeline

```
┌─ INPUT: LogicalDoc with extracted_data
│
├─→ run_tier1_validation (Celery: ai-tier1 queue)
│   ├─ Fetch ValidationAI config: tier1_rules[]
│   ├─ For each rule applicable to doc_type:
│   │   └─→ ai_service.evaluate_tier1_rule() [Claude Haiku, except T1-007]
│   └─ Returns: {passed: bool, rule_results: []}
│
├─ DECISION: Did document pass all rules?
│   │
│   ├─ No (any rule failed)
│   │   ├─→ Update LogicalDoc.status = TIER1_FAILED
│   │   ├─→ Send email status update (send_doc_status_update_email)
│   │   ├─ If on_tier1_failure_action == NOTIFY_BORROWER_AND_CONTINUE:
│   │   │   └─→ notify_tier1_failure() [WhatsApp message]
│   │   └─ [END]
│   │
│   └─ Yes (all rules passed)
│       ├─→ Update LogicalDoc.status = TIER1_PASSED
│       ├─→ Send email status update
│       └─→ Check: are ALL REQUIRED docs now TIER1_PASSED?
│           │
│           ├─ No: [END] (wait for remaining docs)
│           │
│           └─ Yes: run_tier2_validation (Celery: ai-tier2 queue)
```

---

## Tier 2 Validation Pipeline

```
┌─ INPUT: All required LogicalDocs with TIER1_PASSED status
│
├─→ run_tier2_validation (Celery: ai-tier2 queue)
│   ├─ Fetch ValidationAI config: tier2_rules[]
│   ├─ build_tier2_lead_summary(): combine all extracted_data
│   ├─ ai_service.run_tier2_rules() [Claude Sonnet — more complex reasoning]
│   └─ Returns: {passed: bool, rule_results: []}
│
├─ DECISION: Did all documents pass cross-doc validation?
│   │
│   ├─ No (any rule failed)
│   │   ├─ If on_tier2_failure_action == BLOCK_LEAD:
│   │   │   └─→ Update lead.status = VALIDATION_FAILED
│   │   └─ [END] (ops team reviews in dashboard)
│   │
│   └─ Yes (all rules passed)
│       └─→ workflow_engine.advance_to_underwriting()
│           ├─→ Update lead.status = READY_FOR_UNDERWRITING
│           ├─→ Record activity_feed event
│           └─→ Record workflow_runs audit
```

---

## Follow-up Service Pipeline (Hourly)

```
┌─ TRIGGER: Celery Beat scheduler (hourly)
│
├─→ follow_up_service.check_and_send_reminders()
│   └─ For each lead with status = DOC_COLLECTION:
│       ├─ Get workflow_run timestamp: when did doc_collection start?
│       ├─ Calculate days_elapsed since then
│       │
│       ├─ For each day in [1, 3, 5, 7]:
│       │   ├─ If day ≤ days_elapsed AND not yet sent:
│       │   │   └─→ _send_reminder_for_day()
│       │   │       ├─ Send WhatsApp message
│       │   │       └─ Send email
│       │   └─ Mark day as sent in whatsapp_messages + activity_feed
│       │
│       └─ If days_elapsed ≥ 7 AND not yet escalated:
│           └─→ _escalate_to_rm()
│               └─ Log activity_feed event for RM action
```

---

## Queue-to-Service Mapping

| Celery Queue | Worker Task | Service Called | Claude Model |
|---|---|---|---|
| ai-classification | classify_document | ai_service.classify_physical_file | Haiku |
| ai-extraction | extract_document_data | ai_service.extract_data | Haiku |
| ai-tier1 | run_tier1_validation | ai_service.run_tier1_rules + evaluate_tier1_rule | Haiku |
| ai-tier2 | run_tier2_validation | ai_service.run_tier2_rules | Sonnet |
| zip | extract_zip | zip_service.extract_and_upload_zip | None |
| voice | place_voice_call | voice_service.trigger_outbound_call | Haiku (transcript) |
| whatsapp | send_doc_checklist_whatsapp | whatsapp_service.send_document_checklist | None |
| whatsapp | send_doc_checklist_email | email_service.send_document_checklist_email | None |
| whatsapp | process_whatsapp_document | storage_service.upload_file | None |

---

## Status Transitions

### Lead Status

```
NEW
  ↓ (manual: ops verifies PAN)
PAN_VERIFIED
  ↓ (voice_worker places call)
CALL_SCHEDULED
  ↓ (ElevenLabs webhook: call completed)
├─→ QUALIFIED (if qualification_outcome = QUALIFIED)
│   ↓ (enqueue WhatsApp/Email checklists)
│   DOC_COLLECTION
│   ├─→ (Tier 1 validation fails on all docs)
│   │   └─→ [stays in DOC_COLLECTION + reminders on days 1,3,5,7]
│   │       └─→ (after day 7, escalate to RM)
│   │
│   └─→ (all required docs pass Tier 1)
│       ├─→ (Tier 2 validation fails)
│       │   └─→ VALIDATION_FAILED (if BLOCK_LEAD action)
│       │
│       └─→ (Tier 2 validation passes)
│           └─→ READY_FOR_UNDERWRITING
│               └─→ [terminal state for this workflow]
│
├─→ NOT_QUALIFIED (if qualification_outcome = NOT_QUALIFIED)
│   └─→ [end: borrower declined or failed screening]
│
└─→ INCOMPLETE (if qualification_outcome = INCOMPLETE or call failed)
    └─→ [retry logic in voice_worker]
```

### PhysicalFile Status

```
RECEIVED → CLASSIFYING → CLASSIFIED → NEEDS_HUMAN_REVIEW (if confidence < threshold)
                              ↓ (else)
                         CLASSIFIED → create LogicalDoc

(if ZIP file)
EXTRACTING_ZIP → extract files → create child PhysicalFile records for each → PROCESSED
```

### LogicalDoc Status

```
ASSEMBLING (if PARTIAL doc) OR READY_FOR_EXTRACTION (if NORMAL)
  ↓
EXTRACTING → EXTRACTED
  ↓
TIER1_VALIDATING → TIER1_PASSED (all rules pass)
                       ↓
                  (check: all required docs passed?)
                       ↓ (yes)
                  TIER2_VALIDATING → [ai_worker enqueues Tier 2]
                                      ↓
                                  READY_FOR_TIER2

OR

TIER1_FAILED (any rule failed)
  ↓ (may resubmit)
  READY_FOR_EXTRACTION (awaiting borrower action)
```

---

## Configuration Agent Types

### EXTRACTION_AI
- Configures field extraction per doc_type
- Controls classification confidence threshold
- Adds custom prompts for doc-specific contexts

### VALIDATION_AI
- Defines Tier 1 rules (per-document)
- Defines Tier 2 rules (cross-document)
- Controls failure actions (block vs. notify borrower)

### DOC_COLLECTION
- Defines required + optional docs per entity_type
- Stores message templates (WhatsApp checklist, email, reminders)

### VOICE_AI
- (Managed by ElevenLabs; referenced by ELEVENLABS_AGENT_ID env var)

---

## Key Functions by Service

### ai_service.py (Core AI Layer)
- `classify_physical_file()` — File classification (Haiku)
- `extract_data()` — Field extraction (Haiku)
- `run_tier1_rules()` — Per-doc validation (Haiku per rule)
- `run_tier2_rules()` — Cross-doc validation (Sonnet)

### workflow_engine.py (Orchestration)
- `process_lead()` — State machine, routes to next action
- `advance_to_underwriting()` — Final state after Tier 2 pass

### validation_rules.py (Config + Helpers)
- `get_*_agent_config()` — Fetch configs from MongoDB
- `check_all_required_docs_passed()` — Guard for Tier 2
- `build_tier2_lead_summary()` — Assemble cross-doc data

### voice_service.py (Call Management)
- `enqueue_qualification_call()` — FastAPI entry point
- `trigger_outbound_call()` — ElevenLabs API call
- `process_call_completed()` — Webhook handler

### whatsapp_service.py (WhatsApp Messaging)
- `send_document_checklist()` — Initial checklist
- `send_reminder()` — Day-based reminders
- `send_text_message()` — Generic messaging
- `find_lead_by_whatsapp_number()` — Inbound matching

### email_service.py (Email Messaging)
- `send_document_checklist_email()` — Initial checklist
- `send_reminder_email()` — Day-based reminders
- `send_doc_status_update_email()` — Per-Tier1 status

### follow_up_service.py (Automated Reminders)
- `check_and_send_reminders()` — Hourly scheduler entry point

### storage_service.py (File Storage)
- `upload_file()` — S3 upload
- `generate_view_url()` — Presigned GET URL
- `get_upload_presigned_url()` — Presigned PUT URL
- `stream_zip_from_s3_files()` — ZIP assembly

### zip_service.py (ZIP Processing)
- `extract_and_upload_zip()` — Decompose ZIP → individual files

### gmail_service.py (Email Inbound)
- `setup_watch()` — Register Pub/Sub push notification
- `process_new_messages()` — Webhook handler → save attachments

---

## Environment Dependencies

```
ANTHROPIC_API_KEY              → Claude Haiku/Sonnet calls
ELEVENLABS_API_KEY            → Place outbound calls
ELEVENLABS_AGENT_ID           → Which voice agent to use
WAHA_BASE_URL + WAHA_API_KEY  → Send WhatsApp messages
SENDGRID_API_KEY              → Send emails
GMAIL_*                        → Receive email attachments
AWS_* + S3_*                   → File storage
SQS_*_QUEUE_URL               → Celery queue URLs
```

---

## Common Issues & Debug Paths

### Document stuck in CLASSIFYING
- Check: ANTHROPIC_API_KEY set and valid?
- Check: File in S3 accessible?
- Review: Classification confidence threshold vs. returned confidence
- Logs: `ai_worker` → "Classification JSON parse error"

### Tier 1 validation not triggering
- Check: Did extract_document_data succeed? (LogicalDoc.extracted_data populated?)
- Check: Is LogicalDoc.status == EXTRACTED before Tier 1?
- Check: VALIDATION_AI agent config exists in MongoDB?
- Logs: `ai_worker` → "Tier 1 validation for logical_doc_id"

### Tier 2 not triggering despite T1 passing
- Check: All REQUIRED docs in DOC_COLLECTION config marked as TIER1_PASSED?
- Check: check_all_required_docs_passed() returning true?
- Check: TIER1_PASSED count == required count?
- Logs: `validation_rules` → "T2 check for lead_id: X required docs still pending"

### Reminders not sending
- Check: Celery Beat scheduler running? (follow-up service is hourly)
- Check: Lead.status == DOC_COLLECTION?
- Check: workflow_runs record exists with node_type=DOC_COLLECTION?
- Check: WAHA_API_KEY or SENDGRID_API_KEY configured?
- Logs: `follow_up_service` → "Day X WhatsApp/email reminder sent"

### Lead not advancing to READY_FOR_UNDERWRITING
- Check: Tier 2 passed? (TIER2_VALIDATION_COMPLETE in activity_feed with passed=true?)
- Check: advance_to_underwriting() called and succeeded?
- Check: lead.status actually changed in database?
- Logs: `ai_worker` → "Tier 2 PASSED — lead_id=... → READY_FOR_UNDERWRITING"

