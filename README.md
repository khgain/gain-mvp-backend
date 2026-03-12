# Gain AI — Backend

Lending operations automation platform. FastAPI + MongoDB + AWS.

## Quick Start

### 1. Python environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment variables
Edit `.env` with your real credentials. Required to start:
- `MONGODB_URL` — MongoDB Atlas connection string
- `JWT_SECRET_KEY` — already generated (64-char hex)
- `FERNET_KEY` — already generated (Fernet key for PAN/mobile encryption)

### 3. Seed the database
```bash
python scripts/seed.py
```
Creates: HDFC tenant, 4 users, 1 campaign, 1 agent, 3 sample leads.

### 4. Start the server
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
Swagger UI → http://localhost:8000/docs

## Login Credentials (after seed)

| User | Email | Password | Role |
|------|-------|----------|------|
| HDFC Admin | admin@hdfc.gain.ai | Admin@1234 | TENANT_ADMIN |
| HDFC Manager | manager@hdfc.gain.ai | Manager@1234 | CAMPAIGN_MANAGER |
| HDFC Agent | agent@hdfc.gain.ai | Agent@1234 | SALES_AGENT |
| Super Admin | superadmin@gain.ai | SuperAdmin@1234 | GAIN_SUPER_ADMIN |

## API Prefix
All endpoints: `/api/v1/*`

## Celery Workers (Day 2)
```bash
# In a separate terminal
celery -A app.celery_app worker --loglevel=info -Q voice,whatsapp,email,zip,ai
```

## Project Structure
```
gain-backend/
├── app/
│   ├── main.py           ← FastAPI app entry point
│   ├── config.py         ← All env variable loading
│   ├── database.py       ← MongoDB connection + indexes
│   ├── auth.py           ← JWT + bcrypt middleware
│   ├── celery_app.py     ← Celery + SQS config
│   ├── routes/           ← API endpoints
│   │   ├── auth.py       ← POST /auth/login|refresh|logout
│   │   ├── leads.py      ← All /leads endpoints
│   │   ├── campaigns.py  ← All /campaigns endpoints
│   │   ├── agents.py     ← All /agents endpoints
│   │   ├── tenants.py    ← Super admin tenant management
│   │   ├── webhooks.py   ← Voiceai + WAHA + email webhooks
│   │   └── dashboard.py  ← Stats + activity feed + monitor
│   ├── models/           ← Pydantic request/response models
│   ├── services/         ← Business logic
│   │   ├── pan_service.py        ← PAN verify (MVP placeholder)
│   │   ├── workflow_engine.py    ← Lead state machine
│   │   ├── voice_service.py      ← Voice call enqueueing
│   │   └── storage_service.py   ← S3 upload + presigned URLs
│   ├── workers/          ← Celery tasks (Day 2 full impl)
│   └── utils/
│       ├── encryption.py ← Fernet PAN/mobile encryption
│       └── logging.py    ← Sensitive-field masking logger
└── scripts/
    └── seed.py           ← Initial data
```

## Security Notes
- PAN and mobile numbers are Fernet-encrypted at rest
- Passwords are SHA-256 pre-hashed then bcrypt
- Every DB query filtered by `tenant_id` from JWT
- Refresh tokens stored in MongoDB with TTL expiry
- S3 bucket is private — presigned URLs only (1h expiry)
- Sensitive fields (PAN, mobile) never appear in logs
