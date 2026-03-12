"""
ZIP extraction service — download a ZIP from S3, extract each file,
re-upload to S3, create PhysicalFile records, and enqueue classification.

Called by the extract_zip Celery worker.
"""
import io
import time
import zipfile
from datetime import datetime, timezone
from typing import Optional

import boto3
import motor.motor_asyncio  # type hint only

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger("zip_service")

# Max extracted file size to process (50 MB)
_MAX_FILE_BYTES = 50 * 1024 * 1024

# Allowed extract extensions
_ALLOWED_EXTS = {".pdf", ".jpg", ".jpeg", ".png"}


def _s3():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
    )


def extract_and_upload_zip(
    zip_s3_key: str,
    parent_phys_file_id: str,
    lead_id: str,
    tenant_id: str,
) -> list[dict]:
    """
    Download a ZIP from S3, extract its contents, upload each allowed file back to S3.

    Returns a list of dicts, one per extracted file:
        [{"s3_key": str, "filename": str, "file_size_bytes": int}, ...]
    """
    s3 = _s3()

    # Download the ZIP
    try:
        zip_obj = s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=zip_s3_key)
        zip_bytes = zip_obj["Body"].read()
    except Exception as exc:
        logger.error(f"ZIP download failed for key={zip_s3_key}: {exc}")
        raise

    extracted = []

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for member in zf.infolist():
                # Skip directories and hidden files
                if member.is_dir() or member.filename.startswith("."):
                    continue

                # Normalise: take only the base filename (strip nested dirs)
                base_filename = member.filename.rsplit("/", 1)[-1]
                if not base_filename:
                    continue

                # Extension check
                ext = "." + base_filename.lower().rsplit(".", 1)[-1] if "." in base_filename else ""
                if ext not in _ALLOWED_EXTS:
                    logger.info(f"ZIP: skipping {base_filename} (ext={ext} not allowed)")
                    continue

                # Size check (skip oversized files)
                if member.file_size > _MAX_FILE_BYTES:
                    logger.warning(
                        f"ZIP: skipping {base_filename} — too large ({member.file_size} bytes)"
                    )
                    continue

                try:
                    file_bytes = zf.read(member.filename)
                except Exception as exc:
                    logger.error(f"ZIP: could not read {member.filename}: {exc}")
                    continue

                # Upload extracted file to S3
                ts = int(time.time())
                safe_name = base_filename.replace(" ", "_")
                child_s3_key = (
                    f"tenants/{tenant_id}/leads/{lead_id}/{ts}_{safe_name}"
                )

                content_type = _ext_to_content_type(ext)
                try:
                    s3.put_object(
                        Bucket=settings.S3_BUCKET_NAME,
                        Key=child_s3_key,
                        Body=file_bytes,
                        ContentType=content_type,
                        ServerSideEncryption="AES256",
                    )
                except Exception as exc:
                    logger.error(f"ZIP: S3 upload failed for {base_filename}: {exc}")
                    continue

                extracted.append({
                    "s3_key": child_s3_key,
                    "filename": base_filename,
                    "file_size_bytes": len(file_bytes),
                })
                logger.info(
                    f"ZIP: extracted {base_filename} → {child_s3_key} ({len(file_bytes)} bytes)"
                )

    except zipfile.BadZipFile as exc:
        logger.error(f"Bad ZIP file at {zip_s3_key}: {exc}")
        raise

    logger.info(
        f"ZIP extraction complete: {len(extracted)} files from {zip_s3_key} "
        f"for lead_id={lead_id}"
    )
    return extracted


def _ext_to_content_type(ext: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(ext, "application/octet-stream")
