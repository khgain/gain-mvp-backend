"""
Fernet symmetric encryption for PAN and mobile numbers stored in MongoDB.
These fields are encrypted at rest — if the database is breached, raw values
are not exposed. Decrypt only when needed for display or validation.
"""
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings
import logging

logger = logging.getLogger("gain.encryption")

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.FERNET_KEY
        if not key:
            raise RuntimeError(
                "FERNET_KEY is not set. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(key.encode())
    return _fernet


def encrypt_field(value: str) -> str:
    """Encrypt a sensitive string field for storage in MongoDB."""
    if not value:
        return value
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_field(encrypted_value: str) -> str:
    """Decrypt a field retrieved from MongoDB."""
    if not encrypted_value:
        return encrypted_value
    try:
        return _get_fernet().decrypt(encrypted_value.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt field — token invalid or key mismatch")
        raise ValueError("Decryption failed — data may be corrupt or key has changed")


def is_encrypted(value: str) -> bool:
    """Check if a string looks like a Fernet token (for migration safety)."""
    try:
        _get_fernet().decrypt(value.encode())
        return True
    except Exception:
        return False
