# Services Layer Audit — Complete Documentation

This directory contains comprehensive documentation of the GAIN backend services layer, created to enable complete workflow engine integration.

## Documents

### 1. **SERVICES_AUDIT.md** (Primary Reference)
Complete audit of all 11 core services with:
- Function signatures and implementations
- Status (fully implemented vs. stub)
- Purpose and behavior for each function
- What triggers each function
- What it calls next in the pipeline
- Data flow through the system
- MongoDB collections used
- Environment variables required

**Use this for:** Detailed understanding of any specific service or function.

### 2. **WORKFLOW_QUICK_REFERENCE.md** (Developer Guide)
Visual quick reference with:
- Service entry points (FastAPI routes, webhooks)
- Pipeline diagrams (classification → extraction → Tier1 → Tier2)
- Follow-up service flow
- Queue-to-service mapping table
- Status transition diagrams
- Configuration agent types
- Debug paths for common issues

**Use this for:** Quick lookup, debugging, understanding workflows visually.

### 3. **SERVICES_SUMMARY.txt** (Executive Overview)
High-level summary with:
- All 11 services listed with key functions
- Celery worker overview
- Data flow overview
- Complete wiring checklist (what's done, what's TODO)
- Configuration requirements
- Environment setup guide

**Use this for:** Understanding the big picture, onboarding, status tracking.

---

## Service Organization

### Core AI & Decision Services
- **ai_service.py** — Classification, extraction, validation (Claude integration)
- **workflow_engine.py** — State machine orchestration
- **validation_rules.py** — Config management + validation helpers

### Communication Services
- **voice_service.py** — ElevenLabs call management
- **whatsapp_service.py** — WAHA WhatsApp messaging
- **email_service.py** — SendGrid email messaging
- **gmail_service.py** — Gmail inbound via Pub/Sub
- **follow_up_service.py** — Automated reminders (Celery Beat)

### Infrastructure Services
- **storage_service.py** — AWS S3 file storage
- **zip_service.py** — ZIP file decomposition
- **pan_service.py** — PAN verification (MVP placeholder)

### Workers & Queues
- **ai_worker.py** — Classification, extraction, Tier1/Tier2 validation tasks
- **voice_worker.py** — Outbound call placement
- **whatsapp_worker.py** — Document checklist, message processing

### Routes & Webhooks
- **routes/leads.py** — Lead CRUD, PAN verification, manual triggers
- **routes/documents.py** — Document upload, viewing, ZIP download
- **routes/webhooks.py** — Webhook receivers for ElevenLabs, WAHA, Gmail

---

## Understanding the Pipeline

```
1. Lead PAN Verified → 2. Voice Call → 3. Qualified → 4. Doc Collection
                                                           ↓
5. Documents Received (WhatsApp/Email/Portal)
   ↓
6. Classification (Claude Haiku)
   ↓
7. Extraction (Claude Haiku)
   ↓
8. Tier 1 Validation (Claude Haiku per rule)
   ├─ If failed: Notify borrower, request resubmission
   └─ If passed: Check if all required docs passed
      ↓
9. Tier 2 Validation (Claude Sonnet, cross-document)
   ├─ If failed: Flag for ops review
   └─ If passed: Ready for Underwriting
      ↓
10. Underwriting (terminal state for this workflow)
```

### Parallel: Follow-up Service (Hourly)
- Days 1, 3, 5, 7: Send WhatsApp + Email reminders
- Day 7+: Escalate to RM via activity feed

---

## Key Integration Points

### State Machine: workflow_engine.process_lead()
Main entry point for all state transitions:
- `PAN_VERIFIED` → trigger voice call
- `QUALIFIED` → trigger doc collection
- `READY_FOR_UNDERWRITING` → notify (TBD)

### Classification Chain: ai_worker.classify_document()
- Input: Physical file (PDF/image)
- Claude Haiku: Classify document type
- Output: LogicalDoc record OR human review flag
- Next: Extraction (if valid classification)

### Validation Chain: ai_worker.run_tier1_validation()
- Input: Extracted document data
- Process: Run per-document rules
- Next: Check if all required docs passed
- If yes: Enqueue Tier 2 validation

### Tier 2 Gating: validation_rules.check_all_required_docs_passed()
- Prevents Tier 2 from running until all mandatory docs pass Tier 1
- Queries DOC_COLLECTION agent config for required doc types

### Message Routing: whatsapp_service.find_lead_by_whatsapp_number()
- Maps inbound WAHA messages to leads using mobile_hash
- Enables document receipt without decryption of every lead record

---

## Configuration Workflow

1. **Create Agent Records** in MongoDB (`agents` collection):
   ```json
   {
     "type": "EXTRACTION_AI|VALIDATION_AI|DOC_COLLECTION|VOICE_AI",
     "tenant_id": "...",
     "status": "ACTIVE",
     "config": { ... }
   }
   ```

2. **Set Environment Variables** (.env):
   - API keys (Anthropic, ElevenLabs, WAHA, SendGrid, etc.)
   - AWS S3 credentials
   - Celery queue URLs
   - Gmail service account JSON

3. **Configure Celery Beat** (for follow_up_service):
   - Add periodic task to run `check_and_send_reminders()` hourly

---

## Testing & Development

### Dev Mode (No SQS needed)
```bash
export CELERY_TASK_ALWAYS_EAGER=true
# Tasks run synchronously in the same process
```

### Production Mode (AWS SQS)
```bash
export CELERY_TASK_ALWAYS_EAGER=false
# Tasks dispatched to SQS queues
```

### Common Debug Points

1. **Document stuck in CLASSIFYING?**
   - Check ANTHROPIC_API_KEY configured
   - Check file in S3 accessible
   - Review classification confidence vs. threshold

2. **Tier 1 not triggering?**
   - Check LogicalDoc.extracted_data populated
   - Check VALIDATION_AI agent config exists
   - Verify status == EXTRACTED before Tier 1

3. **Tier 2 not triggering?**
   - Check all required docs marked TIER1_PASSED
   - Check check_all_required_docs_passed() returning true
   - Verify DOC_COLLECTION config has required list

4. **Reminders not sending?**
   - Check Celery Beat scheduler running
   - Check lead.status == DOC_COLLECTION
   - Check WAHA_API_KEY or SENDGRID_API_KEY configured

---

## Document Cross-Reference

| Topic | Primary Doc | Secondary |
|-------|---|---|
| Service function details | SERVICES_AUDIT.md | — |
| Pipeline flow | WORKFLOW_QUICK_REFERENCE.md | SERVICES_AUDIT.md |
| Queue routing | WORKFLOW_QUICK_REFERENCE.md | — |
| Status transitions | WORKFLOW_QUICK_REFERENCE.md | SERVICES_AUDIT.md |
| Configuration | SERVICES_SUMMARY.txt | SERVICES_AUDIT.md |
| Environment setup | SERVICES_SUMMARY.txt | SERVICES_AUDIT.md |
| Debugging | WORKFLOW_QUICK_REFERENCE.md | SERVICES_AUDIT.md |

---

## Related Files

**Service Code:**
- `app/services/` — All 11 services
- `app/workers/` — Celery tasks
- `app/routes/` — FastAPI endpoints
- `app/celery_app.py` — Celery configuration

**Configuration:**
- `.env` — Environment variables
- MongoDB `agents` collection — Agent configs (EXTRACTION_AI, VALIDATION_AI, etc.)

**Databases:**
- MongoDB — All state, documents, audit trails
- AWS S3 — File storage (encrypted with AES256)

---

## Next Steps for Integration

1. Review **SERVICES_AUDIT.md** for complete function reference
2. Use **WORKFLOW_QUICK_REFERENCE.md** for visual understanding
3. Check **SERVICES_SUMMARY.txt** for checklist of completed vs. TODO items
4. Set up environment variables per SERVICES_SUMMARY.txt
5. Create agent configs in MongoDB per configuration section
6. Configure Celery Beat for follow_up_service
7. Deploy and test each pipeline stage

---

**Created:** 2026-03-14
**Last Updated:** 2026-03-14
**Audit Scope:** Complete backend services layer
