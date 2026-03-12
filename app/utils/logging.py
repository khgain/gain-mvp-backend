"""
Structured logger that masks sensitive fields before writing to logs.
PAN numbers, mobile numbers, Aadhaar, and document content must NEVER appear in logs.
Only IDs are logged, not values.
"""
import logging
import json
import re
import sys
from datetime import datetime


# Patterns that indicate a value contains sensitive data
_SENSITIVE_PATTERNS = [
    re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]"),        # PAN format
    re.compile(r"\b[6-9]\d{9}\b"),                  # Indian mobile numbers
    re.compile(r"\d{4}\s?\d{4}\s?\d{4}"),          # Aadhaar
]

_SENSITIVE_KEYS = {
    "pan", "mobile", "phone", "aadhaar", "password", "password_hash",
    "token", "access_token", "refresh_token", "secret", "api_key",
    "document_content", "file_content",
}


def _mask_value(key: str, value: object) -> object:
    """Return masked version of a value if the key is sensitive."""
    if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
        if isinstance(value, str) and len(value) > 0:
            return f"[MASKED:{len(value)}chars]"
    return value


def _mask_dict(data: dict) -> dict:
    """Recursively mask sensitive keys in a dict."""
    masked = {}
    for k, v in data.items():
        if isinstance(v, dict):
            masked[k] = _mask_dict(v)
        elif isinstance(v, list):
            masked[k] = [_mask_dict(i) if isinstance(i, dict) else i for i in v]
        else:
            masked[k] = _mask_value(k, v)
    return masked


class SensitiveMaskingFormatter(logging.Formatter):
    """Formatter that strips sensitive data from log records."""

    def format(self, record: logging.LogRecord) -> str:
        # Mask any dict args
        if isinstance(record.args, dict):
            record.args = _mask_dict(record.args)
        return super().format(record)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with masking formatter."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        SensitiveMaskingFormatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    # Remove default handlers to avoid duplicate logs
    root.handlers.clear()
    root.addHandler(handler)

    # Silence noisy third-party loggers
    for noisy in ("boto3", "botocore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"gain.{name}")
