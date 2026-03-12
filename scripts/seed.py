"""
Seed script — creates initial data for development and testing.

Run with:
  python scripts/seed.py

Creates:
  1. Tenant: HDFC Bank
  2. User: admin@hdfc.gain.ai (TENANT_ADMIN, password: Admin@1234)
  3. User: manager@hdfc.gain.ai (CAMPAIGN_MANAGER, password: Manager@1234)
  4. User: agent@hdfc.gain.ai (SALES_AGENT, password: Agent@1234)
  5. Gain Super Admin: superadmin@gain.ai (password: SuperAdmin@1234)
  6. Campaign: Loan Onboarding for HDFC
  7. VOICE_AI Agent — Hindi/Hinglish qualification script + ElevenLabs config
  8. DOC_COLLECTION Agent — WA/email templates + checklists for all 6 entity types
  9. EXTRACTION_AI Agent — field lists for all 14 doc types
  10. VALIDATION_AI Agent — all 8 Tier 1 rules + 6 Tier 2 rules
  11. 3 Sample leads
"""
import asyncio
import sys
import os
from datetime import datetime, timezone

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import certifi
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.auth import hash_password
from app.utils.encryption import encrypt_field
from app.models.campaign import LOAN_ONBOARDING_TEMPLATE


async def seed():
    print("🌱 Seeding Gain AI database...")

    client = AsyncIOMotorClient(settings.MONGODB_URL, tlsCAFile=certifi.where())
    db = client[settings.MONGODB_DATABASE]

    # -----------------------------------------------------------------------
    # Clean existing seed data (idempotent re-runs)
    # -----------------------------------------------------------------------
    await db.tenants.delete_many({"name": {"$in": ["HDFC Bank", "Gain AI Internal"]}})
    await db.users.delete_many(
        {"email": {"$in": [
            "admin@hdfc.gain.ai",
            "manager@hdfc.gain.ai",
            "agent@hdfc.gain.ai",
            "superadmin@gain.ai",
        ]}}
    )

    now = datetime.now(timezone.utc)

    # -----------------------------------------------------------------------
    # 1. Tenant — HDFC Bank
    # -----------------------------------------------------------------------
    hdfc = await db.tenants.insert_one({
        "name": "HDFC Bank",
        "type": "BANK",
        "products": ["term_loan", "lap", "scf"],
        "config": {
            "whatsapp_number": "+919999900000",
            "branding": {"primary_color": "#004C97"},
            "doc_checklist_email": "docs@hdfc.gain.ai",
        },
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    })
    hdfc_id = str(hdfc.inserted_id)
    print(f"  ✓ Tenant created: HDFC Bank (id={hdfc_id})")

    # Gain AI Internal tenant (for super admin)
    gain = await db.tenants.insert_one({
        "name": "Gain AI Internal",
        "type": "FINTECH",
        "products": [],
        "config": {},
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    })
    gain_id = str(gain.inserted_id)

    # -----------------------------------------------------------------------
    # 2. Users
    # -----------------------------------------------------------------------
    users = [
        {
            "tenant_id": hdfc_id,
            "name": "HDFC Admin",
            "email": "admin@hdfc.gain.ai",
            "phone": "9876500001",
            "role": "TENANT_ADMIN",
            "password_hash": hash_password("Admin@1234"),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "tenant_id": hdfc_id,
            "name": "HDFC Campaign Manager",
            "email": "manager@hdfc.gain.ai",
            "phone": "9876500002",
            "role": "CAMPAIGN_MANAGER",
            "password_hash": hash_password("Manager@1234"),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "tenant_id": hdfc_id,
            "name": "HDFC Sales Agent",
            "email": "agent@hdfc.gain.ai",
            "phone": "9876500003",
            "role": "SALES_AGENT",
            "password_hash": hash_password("Agent@1234"),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "tenant_id": gain_id,
            "name": "Gain Super Admin",
            "email": "superadmin@gain.ai",
            "phone": "9999900000",
            "role": "GAIN_SUPER_ADMIN",
            "password_hash": hash_password("SuperAdmin@1234"),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
    ]
    result = await db.users.insert_many(users)
    admin_id = str(result.inserted_ids[0])
    print(f"  ✓ Users created: {[u['email'] for u in users]}")

    # -----------------------------------------------------------------------
    # 3. Campaign — Loan Onboarding
    # -----------------------------------------------------------------------
    campaign = await db.campaigns.insert_one({
        "tenant_id": hdfc_id,
        "name": "SME Loan Onboarding — Q1 2025",
        "use_case": "LOAN_ONBOARDING",
        "lender_product": "term_loan",
        "status": "ACTIVE",
        "workflow_graph": LOAN_ONBOARDING_TEMPLATE,
        "assigned_agents": [],
        "lead_count": 0,
        "created_at": now,
        "updated_at": now,
    })
    campaign_id = str(campaign.inserted_id)
    print(f"  ✓ Campaign created: SME Loan Onboarding (id={campaign_id})")

    # -----------------------------------------------------------------------
    # 4a. VOICE_AI Agent — Hindi/Hinglish script, full ElevenLabs config
    # -----------------------------------------------------------------------
    voice_agent = await db.agents.insert_one({
        "tenant_id": hdfc_id,
        "name": "HDFC Qualification Agent",
        "type": "VOICE_AI",
        "status": "ACTIVE",
        "config": {
            # Script injected into ElevenLabs agent_overrides at call time.
            # Placeholders: {{borrower_name}}, {{loan_type}}, {{loan_amount}}, {{company_name}}
            "script_template": (
                "Aap ek friendly aur professional loan officer hain jo HDFC Bank ki taraf se "
                "{{borrower_name}} ji ko call kar rahe hain. Pehle unhe greet karein aur "
                "poochhein ki kya woh abhi baat kar sakte hain. "
                "Unka business naam {{company_name}} hai aur unhone {{loan_type}} ke liye "
                "{{loan_amount}} ka application diya tha. "
                "Unse politely confirm karein ki woh abhi bhi loan mein interested hain, "
                "aur phir qualification questions poochhein. "
                "Agar woh interested hain to unka consent lein aur batayein ki ek "
                "document checklist bheja jaayega. "
                "Agar woh busy hain to callback schedule karein. "
                "Hamesha Hindi ya Hinglish mein baat karein jab tak borrower English prefer na kare. "
                "Conversation 8 minutes se zyada na ho. "
                "Agar borrower rude ho ya line drop ho to gracefully end karein."
            ),
            # First sentence ElevenLabs speaks when call connects
            "opening_message": (
                "Namaste {{borrower_name}} ji! Main HDFC Bank se bol raha hoon. "
                "Kya aap abhi do minute baat kar sakte hain?"
            ),
            # ElevenLabs language code (mapped to "hi" for HINDI/HINGLISH, "en" for ENGLISH)
            "language": "HINGLISH",

            # ElevenLabs voice overrides — leave None to use dashboard default
            "elevenlabs_voice_id": None,
            "voice_stability": 0.55,
            "voice_similarity_boost": 0.75,

            # Questions the voice agent must ask (keys used by Claude for structured extraction)
            "qualification_questions": [
                {
                    "question_key": "monthly_turnover",
                    "question_text": "Aapke business ka average monthly turnover approx kitna hai? Jaise 5 lakh, 10 lakh, etc.",
                    "data_type": "number",
                    "required": True,
                },
                {
                    "question_key": "business_vintage_years",
                    "question_text": "Aapka business kitne saal purana hai?",
                    "data_type": "number",
                    "required": True,
                },
                {
                    "question_key": "existing_emis",
                    "question_text": (
                        "Kya aapke upar currently koi loan ki EMI chal rahi hai? "
                        "Agar haan, to approx total EMI amount kitna hai monthly?"
                    ),
                    "data_type": "text",
                    "required": True,
                },
                {
                    "question_key": "gst_registered",
                    "question_text": "Kya aapka business GST registered hai?",
                    "data_type": "boolean",
                    "required": True,
                },
                {
                    "question_key": "itr_filed",
                    "question_text": "Kya aapne pichhle saal ka Income Tax Return file kiya hai?",
                    "data_type": "boolean",
                    "required": True,
                },
                {
                    "question_key": "consent_given",
                    "question_text": (
                        "Kya aap is loan application ke liye aage badhna chahenge? "
                        "Main documents ki checklist WhatsApp pe bhej dunga."
                    ),
                    "data_type": "boolean",
                    "required": True,
                },
            ],

            # Call behaviour
            "max_call_duration_minutes": 8,
            "max_attempts": 3,
            "retry_after_hours": 2,
            "fallback_to_whatsapp_after_n_failures": 3,
            "call_schedule_window": {
                "start_hour": 9,   # 9 AM IST
                "end_hour": 18,    # 6 PM IST
            },
        },
        "created_at": now,
        "updated_at": now,
    })
    print(f"  ✓ Agent created: HDFC Qualification Agent / VOICE_AI (id={voice_agent.inserted_id})")

    # -----------------------------------------------------------------------
    # 4b. DOC_COLLECTION Agent — WA/email templates + doc checklists
    # -----------------------------------------------------------------------
    doc_agent = await db.agents.insert_one({
        "tenant_id": hdfc_id,
        "name": "HDFC Document Collection Agent",
        "type": "DOC_COLLECTION",
        "status": "ACTIVE",
        "config": {
            # WhatsApp template — {{borrower_name}}, {{doc_list}}, {{company_name}} injected
            "whatsapp_checklist_template": (
                "Hello {{borrower_name}} ji! 🙏\n\n"
                "HDFC Bank mein aapka loan application ({{company_name}}) process ho raha hai. "
                "Kripya neeche diye gaye documents WhatsApp pe bhejein:\n\n"
                "{{doc_list}}\n\n"
                "📌 Documents ek-ek karke ya ZIP file mein bhej sakte hain.\n"
                "📌 Har file clear aur readable honi chahiye.\n"
                "📌 PDF format preferred hai.\n\n"
                "Koi sawaal ho to reply HELP karein. Dhanyavad! 🙏"
            ),
            # Email templates
            "email_subject_template": (
                "Documents Required — {{loan_type}} Application — {{company_name}} — HDFC Bank"
            ),
            "email_body_template": (
                "Dear {{borrower_name}},\n\n"
                "Thank you for your interest in a {{loan_type}} from HDFC Bank.\n\n"
                "To process your loan application for {{company_name}}, we require the following documents:\n\n"
                "{{doc_list}}\n\n"
                "Please reply to this email with the documents attached, or upload them "
                "via the secure link shared separately.\n\n"
                "For queries, please contact your Relationship Manager.\n\n"
                "Regards,\n"
                "HDFC Bank — Loan Processing Team\n"
                "Powered by Gain AI"
            ),

            # Active channels
            "channels": ["WHATSAPP", "EMAIL"],

            # Nudge schedule (days after initial checklist send)
            "reminder_schedule_days": [1, 3, 5, 7],
            "reminder_whatsapp_template": (
                "Hi {{borrower_name}} ji, ek gentle reminder! 📋\n\n"
                "Aapka HDFC Bank loan application ({{company_name}}) pending hai.\n"
                "Abhi bhi inn documents ka intezaar hai:\n\n"
                "{{pending_docs}}\n\n"
                "Jitna jaldi bhejenge, utna jaldi processing hogi. "
                "Dhanyavad! 🙏"
            ),
            "escalate_to_rm_after_days": 7,

            # Document checklist per entity type — all 6 entity types covered
            "doc_checklist_by_entity_type": {
                "PROPRIETORSHIP": {
                    "required": [
                        "AADHAAR",
                        "PAN_CARD",
                        "BANK_STATEMENT",    # 12 months
                        "ITR",               # Last 2 years
                        "GST_CERT",
                        "UDYAM",
                    ],
                    "optional": [
                        "GST_RETURN",        # Last 6 months
                        "ELECTRICITY_BILL",  # Address proof (last 3 months)
                    ],
                },
                "PARTNERSHIP": {
                    "required": [
                        "AADHAAR",           # All partners
                        "PAN_CARD",          # All partners + firm PAN
                        "PARTNERSHIP_DEED",
                        "BANK_STATEMENT",    # 12 months
                        "ITR",               # Last 2 years
                        "GST_CERT",
                    ],
                    "optional": [
                        "GST_RETURN",
                        "AUDITED_PL",
                        "UDYAM",
                    ],
                },
                "PRIVATE_LIMITED": {
                    "required": [
                        "AADHAAR",           # All directors
                        "PAN_CARD",          # All directors + company PAN
                        "COI",               # Certificate of Incorporation
                        "MOA",               # Memorandum of Association
                        "AOA",               # Articles of Association
                        "BANK_STATEMENT",    # 12 months
                        "AUDITED_PL",        # Last 2 years
                        "GST_CERT",
                    ],
                    "optional": [
                        "GST_RETURN",
                        "ITR",
                    ],
                },
                "LLP": {
                    "required": [
                        "AADHAAR",           # All designated partners
                        "PAN_CARD",          # All designated partners + LLP PAN
                        "COI",
                        "MOA",               # LLP Agreement
                        "BANK_STATEMENT",    # 12 months
                        "AUDITED_PL",
                        "GST_CERT",
                    ],
                    "optional": [
                        "GST_RETURN",
                    ],
                },
                "PUBLIC_LIMITED": {
                    "required": [
                        "AADHAAR",           # Key directors
                        "PAN_CARD",          # Key directors + company PAN
                        "COI",
                        "MOA",
                        "AOA",
                        "BANK_STATEMENT",    # 12 months
                        "AUDITED_PL",        # Last 3 years
                        "GST_CERT",
                        "GST_RETURN",        # Last 12 months
                    ],
                    "optional": [
                        "ITR",
                    ],
                },
                "INDIVIDUAL": {
                    "required": [
                        "AADHAAR",
                        "PAN_CARD",
                        "BANK_STATEMENT",    # 12 months
                        "ITR",               # Last 2 years
                    ],
                    "optional": [
                        "ELECTRICITY_BILL",  # Address proof
                        "PROPERTY_TAX",      # For LAP
                        "TITLE_DEED",        # For LAP
                    ],
                },
            },

            # Upload rules
            "accepted_file_types": ["PDF", "JPG", "JPEG", "PNG", "ZIP"],
            "max_file_size_mb": 25,
        },
        "created_at": now,
        "updated_at": now,
    })
    print(f"  ✓ Agent created: HDFC Document Collection Agent / DOC_COLLECTION (id={doc_agent.inserted_id})")

    # -----------------------------------------------------------------------
    # 4c. EXTRACTION_AI Agent — extraction fields for all 14 doc types
    # -----------------------------------------------------------------------
    extraction_agent = await db.agents.insert_one({
        "tenant_id": hdfc_id,
        "name": "HDFC Document Extraction Agent",
        "type": "EXTRACTION_AI",
        "status": "ACTIVE",
        "config": {
            # Classification confidence threshold — below this → FLAG_FOR_REVIEW
            "classification_confidence_threshold": 75,

            # Fields to extract per document type — all 14 doc types
            "extraction_fields_by_doc_type": {
                "BANK_STATEMENT": [
                    "account_holder_name",
                    "bank_name",
                    "account_number_last4",
                    "ifsc_code",
                    "period_from",
                    "period_to",
                    "avg_monthly_balance",
                    "avg_monthly_credits",
                    "avg_monthly_debits",
                    "opening_balance",
                    "closing_balance",
                ],
                "ITR": [
                    "taxpayer_name",
                    "pan",
                    "assessment_year",
                    "gross_total_income",
                    "net_taxable_income",
                    "gross_turnover",
                    "net_profit",
                ],
                "GST_CERT": [
                    "gstin",
                    "legal_name",
                    "trade_name",
                    "registration_date",
                    "business_type",
                    "principal_place_of_business",
                ],
                "GST_RETURN": [
                    "gstin",
                    "legal_name",
                    "period_from",
                    "period_to",
                    "total_taxable_turnover",
                    "total_tax_paid",
                ],
                "AUDITED_PL": [
                    "business_name",
                    "financial_year",
                    "gross_revenue",
                    "total_expenses",
                    "net_profit",
                    "depreciation",
                ],
                "AADHAAR": [
                    "name",
                    "dob",
                    "gender",
                    "aadhaar_last4",
                    "address",
                ],
                "PAN_CARD": [
                    "name",
                    "fathers_name",
                    "dob",
                    "pan_number",
                ],
                "MOA": [
                    "company_name",
                    "cin",
                    "registered_office",
                    "directors",
                    "authorized_capital",
                ],
                "COI": [
                    "company_name",
                    "cin",
                    "date_of_incorporation",
                    "company_type",
                ],
                "UDYAM": [
                    "enterprise_name",
                    "udyam_registration_number",
                    "major_activity",
                    "social_category",
                    "date_of_registration",
                ],
                "TITLE_DEED": [
                    "property_address",
                    "owner_name",
                    "survey_number",
                    "registration_date",
                    "property_area",
                ],
                "ELECTRICITY_BILL": [
                    "consumer_name",
                    "address",
                    "bill_date",
                    "consumer_number",
                    "amount_due",
                ],
                "PARTNERSHIP_DEED": [
                    "firm_name",
                    "partners",
                    "profit_sharing_ratio",
                    "date_of_partnership",
                    "registered_address",
                ],
                "PROPERTY_TAX": [
                    "owner_name",
                    "property_address",
                    "assessment_year",
                    "tax_amount",
                ],
                "NOC": [
                    "borrower_name",
                    "issued_by",
                    "issue_date",
                    "property_description",
                ],
            },

            # Fallback behaviour when classification confidence < threshold
            "on_low_confidence_classification": "FLAG_FOR_REVIEW",
            # "FLAG_FOR_REVIEW" | "AUTO_REJECT" | "AUTO_ACCEPT_BEST_GUESS"

            # Lender-specific prompt additions (blank = use defaults)
            "classification_prompt_additions": (
                "This document is from an Indian SME loan application processed by HDFC Bank. "
                "Common doc types include Hindi/regional language versions of ITR, GST, and bank statements."
            ),
            "extraction_prompt_additions": (
                "All monetary amounts should be extracted in INR (rupees). "
                "Dates should be in ISO format (YYYY-MM-DD). "
                "For bank statements with multiple months, compute averages across all months."
            ),
        },
        "created_at": now,
        "updated_at": now,
    })
    print(f"  ✓ Agent created: HDFC Document Extraction Agent / EXTRACTION_AI (id={extraction_agent.inserted_id})")

    # -----------------------------------------------------------------------
    # 4d. VALIDATION_AI Agent — all 8 Tier 1 rules + 6 Tier 2 rules
    # -----------------------------------------------------------------------
    validation_agent = await db.agents.insert_one({
        "tenant_id": hdfc_id,
        "name": "HDFC Document Validation Agent",
        "type": "VALIDATION_AI",
        "status": "ACTIVE",
        "config": {
            # -------------------------------------------------------------------
            # Tier 1 — per-document rules, run immediately after extraction
            # -------------------------------------------------------------------
            "tier1_rules": [
                {
                    "rule_id": "T1-001",
                    "rule_name": "Bank Statement Coverage",
                    "doc_type": "BANK_STATEMENT",
                    "enabled": True,
                    "threshold": 12,    # months required
                    "custom_prompt": (
                        "Check that the bank statement covers a complete 12 consecutive months. "
                        "Compute the difference between period_to and period_from. "
                        "If less than 11 months (allow 1 month tolerance), return FAIL with the exact coverage found."
                    ),
                },
                {
                    "rule_id": "T1-002",
                    "rule_name": "Electricity Bill Recency",
                    "doc_type": "ELECTRICITY_BILL",
                    "enabled": True,
                    "threshold": 3,     # months; bill must be within last 3 months
                    "custom_prompt": (
                        "Check that the electricity bill issue date is within the last 3 months from today. "
                        "If the bill_date is more than 90 days old, return FAIL with the bill date found."
                    ),
                },
                {
                    "rule_id": "T1-003",
                    "rule_name": "ITR Financial Year",
                    "doc_type": "ITR",
                    "enabled": True,
                    "threshold": None,
                    "custom_prompt": (
                        "Check that the ITR is for the most recent complete financial year. "
                        "If the ITR assessment year is the current year or last year, return PASS. "
                        "If it is more than 2 years old, return FLAG_FOR_REVIEW (not FAIL) with the assessment year found. "
                        "Do not auto-reject old ITRs — flag them for ops review."
                    ),
                },
                {
                    "rule_id": "T1-004",
                    "rule_name": "GST Return Coverage",
                    "doc_type": "GST_RETURN",
                    "enabled": True,
                    "threshold": 6,     # months required
                    "custom_prompt": (
                        "Check that the GST returns cover at least the last 6 months. "
                        "Compute the coverage from period_from to period_to. "
                        "If less than 6 months, return FAIL with the actual months covered."
                    ),
                },
                {
                    "rule_id": "T1-005",
                    "rule_name": "KYC Document Readability",
                    "doc_type": "ANY",
                    "enabled": True,
                    "threshold": None,
                    "custom_prompt": (
                        "Check that the KYC document (Aadhaar or PAN card) is legible. "
                        "Return FAIL if: the image is blurry or dark, key fields are not readable, "
                        "or the document appears to be expired (for Aadhaar, check if it is an old non-UIDAI format). "
                        "Only apply this check to documents with type AADHAAR or PAN_CARD."
                    ),
                },
                {
                    "rule_id": "T1-006",
                    "rule_name": "Bank Statement — Scheduled Bank",
                    "doc_type": "BANK_STATEMENT",
                    "enabled": True,
                    "threshold": None,
                    "custom_prompt": (
                        "Check that the bank statement is from a Scheduled Commercial Bank listed by RBI. "
                        "Examples of ACCEPTED banks: SBI, HDFC, ICICI, Axis, Kotak, PNB, Canara, BOB, IndusInd, Yes Bank, IDFC First. "
                        "Co-operative banks, credit societies, and unrecognized local banks should be flagged for ops review (FLAG_FOR_REVIEW), not rejected. "
                        "Return PASS for any RBI-scheduled commercial bank."
                    ),
                },
                {
                    "rule_id": "T1-007",
                    "rule_name": "Minimum File Size",
                    "doc_type": "ANY",
                    "enabled": True,
                    "threshold": 10240,     # 10 KB in bytes
                    "custom_prompt": (
                        "This rule is checked at upload time using file size metadata. "
                        "Any file smaller than 10 KB (10240 bytes) is considered corrupt or a placeholder. "
                        "Return FAIL immediately — do not attempt extraction on tiny files."
                    ),
                },
                {
                    "rule_id": "T1-008",
                    "rule_name": "Partial Document Completeness",
                    "doc_type": "ANY",
                    "enabled": True,
                    "threshold": 48,    # hours before nudge is sent
                    "custom_prompt": (
                        "If this document is currently marked PARTIAL (e.g. only page 1 of a multi-page bank statement received), "
                        "check whether all remaining pages have now been received and linked as part of the same logical document. "
                        "If all pages are now present, return PASS and mark the logical doc as COMPLETE. "
                        "If still incomplete, check how many hours have elapsed since the first page was received. "
                        "If more than 48 hours, return FLAG_FOR_OPS to trigger a borrower nudge."
                    ),
                },
            ],

            # -------------------------------------------------------------------
            # Tier 2 — cross-document consistency rules, run after all T1 passes
            # -------------------------------------------------------------------
            "tier2_rules": [
                {
                    "rule_id": "T2-001",
                    "rule_name": "Partnership KYC Completeness",
                    "doc_type": "PARTNERSHIP_DEED",
                    "enabled": True,
                    "threshold": None,
                    "custom_prompt": (
                        "Extract the list of all partners named in the partnership deed. "
                        "For each partner, check whether both Aadhaar AND PAN card have been received and passed Tier 1. "
                        "Return FAIL with a list of partner names who are missing either document. "
                        "Return PASS only if every partner has both Aadhaar and PAN verified."
                    ),
                },
                {
                    "rule_id": "T2-002",
                    "rule_name": "GST vs ITR Turnover Consistency",
                    "doc_type": "ANY",
                    "enabled": True,
                    "threshold": 20,    # % variance allowed between GST and ITR turnover
                    "custom_prompt": (
                        "Compare the total taxable turnover declared in GST returns with the gross turnover declared in the ITR. "
                        "Both should be for the same financial year. "
                        "If the variance exceeds 20%, return FLAG_FOR_OPS with both figures and the computed variance %. "
                        "If data is not available for both, return SKIP."
                    ),
                },
                {
                    "rule_id": "T2-003",
                    "rule_name": "Bank Statement vs ITR Consistency",
                    "doc_type": "ANY",
                    "enabled": True,
                    "threshold": 40,    # % variance allowed between bank credits and ITR turnover
                    "custom_prompt": (
                        "Compare the annualised average monthly credits from the bank statement with the ITR gross turnover. "
                        "For the bank statement, multiply avg_monthly_credits by 12 to annualise it. "
                        "If the variance between this annualised figure and ITR turnover exceeds 40%, "
                        "return FLAG_FOR_OPS with both figures and the computed variance %. "
                        "A variance > 40% may indicate undeclared income or use of multiple accounts."
                    ),
                },
                {
                    "rule_id": "T2-004",
                    "rule_name": "Director KYC Completeness",
                    "doc_type": "MOA",
                    "enabled": True,
                    "threshold": None,
                    "custom_prompt": (
                        "Extract the list of all directors named in the MOA or COI (Certificate of Incorporation). "
                        "For each director, check whether both Aadhaar AND PAN card have been received and passed Tier 1. "
                        "Return FAIL with a list of director names who are missing either document. "
                        "Return PASS only if every director has both Aadhaar and PAN verified. "
                        "For large public companies with many directors, at minimum check the key managerial persons (MD, CEO, CFO)."
                    ),
                },
                {
                    "rule_id": "T2-005",
                    "rule_name": "Audited P&L vs ITR Net Profit",
                    "doc_type": "ANY",
                    "enabled": True,
                    "threshold": 15,    # % variance allowed between P&L and ITR net profit
                    "custom_prompt": (
                        "Compare the net profit reported in the Audited P&L with the net taxable income in the ITR for the same financial year. "
                        "If they differ by more than 15%, return FLAG_FOR_OPS with both figures and the computed variance %. "
                        "A significant variance may indicate tax evasion or accounting discrepancies. "
                        "If data is not available for both documents, return SKIP."
                    ),
                },
                {
                    "rule_id": "T2-006",
                    "rule_name": "No Future-Dated Documents",
                    "doc_type": "ANY",
                    "enabled": True,
                    "threshold": None,
                    "custom_prompt": (
                        "Check all extracted date fields across all documents for this lead. "
                        "Flag any document where a key date field (issue_date, bill_date, registration_date, "
                        "date_of_incorporation, period_to, etc.) is in the future relative to today. "
                        "Return FAIL with the document type and the future date found. "
                        "This catches forged or incorrectly scanned documents."
                    ),
                },
            ],

            # Actions on rule failures
            "on_tier1_failure_action": "NOTIFY_BORROWER_AND_CONTINUE",
            # "NOTIFY_BORROWER_AND_CONTINUE" | "BLOCK_LEAD" | "FLAG_FOR_OPS_REVIEW"
            "on_tier2_failure_action": "FLAG_FOR_OPS_REVIEW",
            # "FLAG_FOR_OPS_REVIEW" | "BLOCK_LEAD"

            # WhatsApp/email failure notification templates (sent to borrower for T1 failures)
            "tier1_failure_message_templates": {
                "T1-001": (
                    "Aapka bank statement sirf {{months_covered}} mahine ka hai. "
                    "Kripya ek complete 12 mahine ka bank statement bhejein ({{expected_period}} ke liye). "
                    "Dhanyavad! 🙏"
                ),
                "T1-002": (
                    "Aapka electricity bill {{bill_date}} ka hai — yeh 3 mahine se purana hai. "
                    "Kripya pichhle 3 mahine ka electricity bill bhejein. "
                    "Dhanyavad! 🙏"
                ),
                "T1-003": (
                    "Jo ITR aapne bheja hai woh thoda purana lag raha hai. "
                    "Agar aapke paas latest ITR available hai to kripya woh bhejein. "
                    "Dhanyavad! 🙏"
                ),
                "T1-004": (
                    "Aapka GST return sirf {{months_covered}} mahine ka hai. "
                    "Kripya pichhle 6 mahine ke GST returns bhejein. "
                    "Dhanyavad! 🙏"
                ),
                "T1-005": (
                    "Jo {{doc_type}} aapne bheja hai woh clearly readable nahi hai. "
                    "Kripya ek clear, high quality scan ya photo bhejein. "
                    "Dhanyavad! 🙏"
                ),
                "T1-006": (
                    "Aapne co-operative bank ka statement bheja hai. "
                    "Kripya ek scheduled commercial bank (jaise SBI, HDFC, ICICI, Axis, etc.) ka statement bhi bhejein. "
                    "Dhanyavad! 🙏"
                ),
                "T1-007": (
                    "Jo file aapne bheji hai woh corrupt ya incomplete lag rahi hai. "
                    "Kripya document dobara bhejein. "
                    "Dhanyavad! 🙏"
                ),
                "T1-008": (
                    "Aapke {{doc_type}} ka sirf kuch hissa mila hai. "
                    "Kripya poora document (sabhi pages) ek saath bhejein. "
                    "Dhanyavad! 🙏"
                ),
            },

            # Lender-specific prompt additions for validation Claude prompts
            "validation_prompt_additions": (
                "This is an Indian SME lending context (HDFC Bank). "
                "All financial figures are in Indian Rupees (INR). "
                "Financial years run April to March. "
                "Assessment year N means FY N-1 to N (e.g. AY 2024-25 = FY April 2023 to March 2024). "
                "Be lenient with very small businesses — a 1-2 person proprietorship may not have audited P&L."
            ),
        },
        "created_at": now,
        "updated_at": now,
    })
    print(f"  ✓ Agent created: HDFC Document Validation Agent / VALIDATION_AI (id={validation_agent.inserted_id})")

    # -----------------------------------------------------------------------
    # 5. Sample leads
    # -----------------------------------------------------------------------
    sample_leads = [
        {
            "tenant_id": hdfc_id,
            "campaign_id": campaign_id,
            "assigned_to": admin_id,
            "name": "Rajesh Kumar",
            "company_name": "Kumar Textiles Pvt Ltd",
            "pan": encrypt_field("BKRPM1234A"),
            "mobile": encrypt_field("9876543210"),
            "email": "rajesh@kumartextiles.com",
            "loan_type": "TERM_LOAN",
            "entity_type": "PRIVATE_LIMITED",
            "loan_amount_requested": 5000000 * 100,  # 50 lakh in paise
            "status": "NEW",
            "source": "DIRECT",
            "qualification_result": None,
            "validation_flags": [],
            "metadata": {"source_notes": "Referral from branch"},
            "created_at": now,
            "updated_at": now,
        },
        {
            "tenant_id": hdfc_id,
            "campaign_id": campaign_id,
            "assigned_to": admin_id,
            "name": "Priya Sharma",
            "company_name": "Sharma Trading Co",
            "pan": encrypt_field("CMNPS5678B"),
            "mobile": encrypt_field("9123456789"),
            "email": "priya@sharmatrading.com",
            "loan_type": "LAP",
            "entity_type": "PROPRIETORSHIP",
            "loan_amount_requested": 2500000 * 100,  # 25 lakh in paise
            "status": "PAN_VERIFIED",
            "source": "DIRECT",
            "pan_verified_by": admin_id,
            "pan_verified_at": now,
            "qualification_result": None,
            "validation_flags": [],
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        },
        {
            "tenant_id": hdfc_id,
            "campaign_id": campaign_id,
            "assigned_to": admin_id,
            "name": "Vikram Singh",
            "company_name": "Singh & Sons Partnership",
            "pan": encrypt_field("ELPVS9012C"),
            "mobile": encrypt_field("9988776655"),
            "email": "vikram@singhsons.com",
            "loan_type": "TERM_LOAN",
            "entity_type": "PARTNERSHIP",
            "loan_amount_requested": 10000000 * 100,  # 1 crore in paise
            "status": "DOC_COLLECTION",
            "source": "DIRECT",
            "pan_verified_by": admin_id,
            "pan_verified_at": now,
            "qualification_result": {
                "outcome": "QUALIFIED",
                "call_transcript": "Borrower confirmed interest. Turnover: 5 crore. Vintage: 8 years.",
                "key_data": {"turnover_crore": 5, "vintage_years": 8, "existing_loans": 1},
            },
            "validation_flags": [],
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        },
    ]
    leads_result = await db.leads.insert_many(sample_leads)
    print(f"  ✓ Sample leads created: {len(sample_leads)}")

    # Activity feed entries
    await db.activity_feed.insert_many([
        {
            "tenant_id": hdfc_id,
            "lead_id": str(leads_result.inserted_ids[0]),
            "event_type": "LEAD_CREATED",
            "message": "Lead 'Rajesh Kumar' created via DIRECT",
            "created_by": admin_id,
            "created_at": now,
        },
        {
            "tenant_id": hdfc_id,
            "lead_id": str(leads_result.inserted_ids[2]),
            "event_type": "QUALIFIED",
            "message": "Vikram Singh qualified — sending document checklist",
            "created_by": admin_id,
            "created_at": now,
        },
    ])

    print("\n✅ Seed complete!\n")
    print("─" * 60)
    print("Login credentials:")
    print("  Admin:        admin@hdfc.gain.ai / Admin@1234")
    print("  Manager:      manager@hdfc.gain.ai / Manager@1234")
    print("  Agent:        agent@hdfc.gain.ai / Agent@1234")
    print("  Super Admin:  superadmin@gain.ai / SuperAdmin@1234")
    print("─" * 60)
    print(f"  HDFC Tenant ID:  {hdfc_id}")
    print(f"  Campaign ID:     {campaign_id}")
    print("─" * 60)
    print("Agents created:")
    print(f"  VOICE_AI:       {voice_agent.inserted_id}")
    print(f"  DOC_COLLECTION: {doc_agent.inserted_id}")
    print(f"  EXTRACTION_AI:  {extraction_agent.inserted_id}")
    print(f"  VALIDATION_AI:  {validation_agent.inserted_id}")
    print("─" * 60)

    client.close()


if __name__ == "__main__":
    asyncio.run(seed())
