from pydantic_settings import BaseSettings
from typing import Optional
import os


class Settings(BaseSettings):
    # MongoDB
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DATABASE: str = "gain_mvp"

    # JWT
    JWT_SECRET_KEY: str = "dev_secret_change_in_production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24
    JWT_REFRESH_EXPIRE_DAYS: int = 30

    # Fernet encryption (PAN + mobile at rest)
    FERNET_KEY: str = ""

    # AWS
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "ap-south-1"
    S3_BUCKET_NAME: str = "gain-mvp-documents"

    # SQS Queue URLs
    SQS_VOICE_QUEUE_URL: str = ""
    SQS_WHATSAPP_QUEUE_URL: str = ""
    SQS_EMAIL_QUEUE_URL: str = ""
    SQS_ZIP_QUEUE_URL: str = ""
    # AI queues — one per task type
    SQS_AI_CLASSIFICATION_QUEUE_URL: str = ""
    SQS_AI_EXTRACTION_QUEUE_URL: str = ""
    SQS_AI_TIER1_QUEUE_URL: str = ""
    SQS_AI_TIER2_QUEUE_URL: str = ""
    # Webhook HMAC verification
    WEBHOOK_SECRET: str = ""

    # ElevenLabs Conversational AI (outbound calling + voice synthesis)
    ELEVENLABS_API_KEY: Optional[str] = None
    ELEVENLABS_AGENT_ID: Optional[str] = None          # ID of the saved ElevenLabs agent in dashboard
    ELEVENLABS_PHONE_NUMBER_ID: Optional[str] = None   # Twilio phone number ID imported into ElevenLabs
    ELEVENLABS_WEBHOOK_SECRET: Optional[str] = None    # HMAC-SHA256 secret for verifying webhooks
    ELEVENLABS_VOICE_ID: Optional[str] = None          # Default voice ID (overrideable per VOICE_AI agent config)

    # WAHA (WhatsApp)
    WAHA_BASE_URL: Optional[str] = None
    WAHA_API_KEY: Optional[str] = None

    # Anthropic
    ANTHROPIC_API_KEY: Optional[str] = None

    # PAN Validation (post-MVP placeholder)
    PAN_VALIDATION_API_KEY: Optional[str] = None

    # SendGrid
    SENDGRID_API_KEY: Optional[str] = None
    SENDGRID_INBOUND_WEBHOOK_SECRET: Optional[str] = None
    LEADS_EMAIL_DOMAIN: Optional[str] = None

    # CORS — comma-separated list of allowed origins
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:5173,https://gain-mvp-frontend.vercel.app"

    # Environment
    ENVIRONMENT: str = "development"

    model_config = {"env_file": ".env", "case_sensitive": True, "extra": "ignore"}

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


settings = Settings()
