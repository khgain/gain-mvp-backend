# Gain AI - Session State (Paused March 14, 2026)

## What Was Just Completed
1. **Added diagnostic logging** to `voice_service.py` and `workflow_engine.py` to trace why automated WhatsApp/email fails after call qualifies
2. **Added 10-second auto-polling** to all 5 frontend tabs: LeadDetail overview, LeadDocuments, WhatsAppChat, EmailThread, LeadValidation
3. Both backend and frontend committed and pushed (backend commit c5f8a59, frontend commit 3ce9986)

## Critical Unsolved Bug: Automated Doc Collection Doesn't Send WhatsApp/Email
- **Symptom**: When a call completes and lead qualifies → status changes to DOC_COLLECTION BUT no WhatsApp or email is sent to borrower
- **Manual follow-up button WORKS** (same underlying service functions)
- **3 rewrites of `_trigger_doc_collection`** haven't fixed it
- **Most likely root causes** (now instrumented with logging):
  1. `elevenlabs_status` might not be in `{"completed", "done", "success"}` → `new_lead_status` becomes "INCOMPLETE" → workflow engine never fires
  2. Exception silently caught inside `_trigger_doc_collection` (decrypt_field failing, WAHA session lost, etc.)
  3. `process_lead()` might not be reached at all
- **Next step**: Run a test call, then check Railway logs for these log lines:
  - `[VOICE] Decision inputs` — shows elevenlabs_status and qualification_outcome
  - `[VOICE] Resolved new_lead_status` — shows what status was set
  - `[VOICE] About to check workflow trigger` — shows if condition was met
  - `[WF_ENGINE] Processing lead_id` — shows workflow engine entered
  - `[WF_ENGINE] → QUALIFIED branch` — shows doc collection triggered
  - `[WF_ENGINE] _trigger_doc_collection ENTERED` — confirms entry + mobile/email presence

## Key File Locations
- Backend: `/gain-backend/app/services/voice_service.py` (lines 305-390 — decision logic + workflow trigger)
- Backend: `/gain-backend/app/services/workflow_engine.py` (lines 48-65 — state machine, lines 74-184 — _trigger_doc_collection)
- Backend: `/gain-backend/app/routes/leads.py` (follow-up endpoint that WORKS — compare with automated path)
- Backend: `/gain-backend/app/services/validation_rules.py` (default doc checklist config)
- Frontend: All 4 tab components now have 10s polling via useCallback + setInterval

## Other Pending Items
- WAHA sessions lost on every Railway redeploy (no persistent volume) — needs WAHA Plus or recovery script
- Debug endpoint (`GET /leads/debug/connectivity`) is public — protect before production
- Backend push to Railway may need manual trigger (check if Railway auto-deploys from git)
