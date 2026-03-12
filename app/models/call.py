"""
Call and WhatsApp message Pydantic models.

calls collection       — one record per outbound call attempt (PRD Section 5.8)
whatsapp_messages collection — one record per WA message sent or received (PRD Section 5.9)
"""
from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime
from enum import Enum


# ---------------------------------------------------------------------------
# Call models (PRD 5.8)
# ---------------------------------------------------------------------------

class CallStatus(str, Enum):
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"


class QualificationOutcome(str, Enum):
    QUALIFIED = "QUALIFIED"
    NOT_QUALIFIED = "NOT_QUALIFIED"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    NO_ANSWER = "NO_ANSWER"
    INCOMPLETE = "INCOMPLETE"


class TranscriptMessage(BaseModel):
    """One turn in the voice call transcript."""
    role: str           # "assistant" | "user"
    message: str        # Text of what was said
    timestamp: Optional[datetime] = None   # Absolute timestamp (derived from time_in_call_secs + call start)


class CallResponse(BaseModel):
    """
    Full call record — returned by GET /leads/:id/calls and GET /leads/:id/calls/:call_id.
    The list endpoint omits transcript and extracted_fields for brevity.
    """
    id: str
    lead_id: str
    tenant_id: str
    elevenlabs_conversation_id: Optional[str] = None
    status: CallStatus
    ended_reason: Optional[str] = None       # ElevenLabs ended reason string
    duration_seconds: int = 0
    recording_url: Optional[str] = None      # ElevenLabs recording URL (if enabled)
    transcript: list[TranscriptMessage] = [] # Full structured transcript
    transcript_raw: Optional[str] = None     # Raw transcript text (for Claude processing)
    ai_summary: Optional[str] = None         # ElevenLabs post-call AI summary
    qualification_outcome: Optional[QualificationOutcome] = None
    extracted_fields: dict[str, Any] = {}    # Claude-extracted: turnover, vintage, existing_emis, consent_given, callback_datetime
    attempt_number: int = 1
    initiated_at: datetime
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class CallListItem(BaseModel):
    """Condensed call record for the list view (no transcript content)."""
    id: str
    lead_id: str
    elevenlabs_conversation_id: Optional[str] = None
    status: CallStatus
    ended_reason: Optional[str] = None
    duration_seconds: int = 0
    qualification_outcome: Optional[QualificationOutcome] = None
    ai_summary: Optional[str] = None
    attempt_number: int = 1
    initiated_at: datetime
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# WhatsApp message models (PRD 5.9)
# ---------------------------------------------------------------------------

class MessageDirection(str, Enum):
    OUTBOUND = "OUTBOUND"   # Gain → Borrower
    INBOUND = "INBOUND"     # Borrower → Gain


class MessageType(str, Enum):
    TEXT = "TEXT"
    DOCUMENT = "DOCUMENT"
    IMAGE = "IMAGE"
    AUDIO = "AUDIO"
    TEMPLATE = "TEMPLATE"


class MessageStatus(str, Enum):
    SENT = "SENT"           # Sent to WAHA
    DELIVERED = "DELIVERED" # Delivered to recipient's device
    READ = "READ"           # Recipient opened the message
    FAILED = "FAILED"       # Delivery failed
    RECEIVED = "RECEIVED"   # Inbound message received


class WhatsAppMessageResponse(BaseModel):
    """
    One WhatsApp message in the conversation thread.
    Returned by GET /leads/:id/whatsapp-messages, sorted oldest-first
    to render as a chat window.
    """
    id: str
    lead_id: str
    tenant_id: str
    direction: MessageDirection
    message_type: MessageType
    content: Optional[str] = None          # Text content (for TEXT and TEMPLATE types)
    media_s3_key: Optional[str] = None     # S3 key if message contains a file
    media_filename: Optional[str] = None   # Original filename
    media_mime_type: Optional[str] = None  # e.g. "application/pdf", "image/jpeg"
    physical_file_id: Optional[str] = None # Links to PhysicalFile record if inbound doc was processed
    waha_message_id: Optional[str] = None  # WAHA's internal message ID
    sender_phone: Optional[str] = None     # Borrower's phone number (inbound only)
    status: MessageStatus
    template_name: Optional[str] = None    # e.g. "doc_checklist", "follow_up_day3"
    sent_at: datetime

    model_config = {"from_attributes": True}
