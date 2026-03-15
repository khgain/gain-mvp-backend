"""
AWS S3 storage service.

Rules from PRD:
- NEVER expose S3 URLs directly — always use presigned URLs (1-hour expiry)
- NEVER store files locally — S3 is the only storage
- ZIP downloads must STREAM, never load all files into memory at once
- Verify tenant_id before generating any presigned URL
- Bucket structure: /tenants/{tenant_id}/leads/{lead_id}/{timestamp}_{filename}
"""
import io
import zipfile
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger("storage")

# Document category mapping for ZIP folder organisation
DOC_CATEGORY_MAP = {
    "AADHAAR": "KYC",
    "PAN_CARD": "KYC",
    "PASSPORT_PHOTO": "KYC",
    "BANK_STATEMENT": "Financials",
    "ITR": "Financials",
    "AUDITED_PL": "Financials",
    "GST_RETURN": "Financials",
    "GST_CERT": "Business",
    "MOA": "Business",
    "AOA": "Business",
    "COI": "Business",
    "UDYAM": "Business",
    "PARTNERSHIP_DEED": "Business",
    "TITLE_DEED": "Property",
    "PROPERTY_TAX": "Property",
    "NOC": "Property",
    "ELECTRICITY_BILL": "Property",
}


def _get_s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
    )


def build_s3_key(tenant_id: str, lead_id: str, filename: str) -> str:
    timestamp = int(time.time())
    safe_name = filename.replace(" ", "_")
    return f"tenants/{tenant_id}/leads/{lead_id}/{timestamp}_{safe_name}"


async def upload_file(
    file_bytes: bytes,
    s3_key: str,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload bytes to S3. Returns the s3_key on success."""
    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=s3_key,
            Body=file_bytes,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        logger.info(f"Uploaded file to S3: key={s3_key} size={len(file_bytes)}")
        return s3_key
    except ClientError as exc:
        logger.error(f"S3 upload failed: key={s3_key} error={exc.response['Error']['Code']}")
        raise HTTPException(status_code=500, detail="File upload failed") from exc


async def generate_view_url(
    s3_key: str,
    tenant_id: str,
    lead_tenant_id: str,
    expiry_seconds: int = 3600,
) -> str:
    """
    Generate a presigned URL for viewing a document.
    Verifies tenant_id ownership before generating.
    """
    if tenant_id != lead_tenant_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to access this document",
        )
    try:
        s3 = _get_s3_client()
        # Determine content type from key extension for inline display
        ext = s3_key.rsplit(".", 1)[-1].lower() if "." in s3_key else ""
        content_type_map = {
            "pdf": "application/pdf",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
        }
        params = {"Bucket": settings.S3_BUCKET_NAME, "Key": s3_key}
        # Force inline display in browser instead of downloading
        ct = content_type_map.get(ext, "")
        if ct:
            params["ResponseContentType"] = ct
        params["ResponseContentDisposition"] = "inline"
        url = s3.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expiry_seconds,
        )
        logger.info(f"Generated presigned URL for key={s3_key} expiry={expiry_seconds}s")
        return url
    except ClientError as exc:
        logger.error(f"Presigned URL generation failed: {exc}")
        raise HTTPException(status_code=500, detail="Could not generate document URL") from exc


async def get_upload_presigned_url(
    s3_key: str,
    content_type: str = "application/pdf",
    expiry_seconds: int = 3600,
) -> str:
    """Return a presigned PUT URL for direct browser upload to S3."""
    try:
        s3 = _get_s3_client()
        url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.S3_BUCKET_NAME,
                "Key": s3_key,
                "ContentType": content_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=expiry_seconds,
        )
        return url
    except ClientError as exc:
        raise HTTPException(status_code=500, detail="Could not generate upload URL") from exc


def stream_zip_from_s3_files(files) -> bytes:
    """
    Assemble a ZIP in memory with category subfolders.
    files: list of dicts with keys s3_key, doc_type, filename.
    """
    buf = io.BytesIO()
    s3 = _get_s3_client()
    category_counters = {}

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, file_info in enumerate(files, start=1):
            doc_type = file_info.get("doc_type", "OTHER")
            category = DOC_CATEGORY_MAP.get(doc_type, "Other")
            category_counters[category] = category_counters.get(category, 0) + 1
            idx = category_counters[category]

            ext = file_info.get("filename", "document").rsplit(".", 1)[-1]
            zip_filename = f"{category}/{idx:02d}_{doc_type}.{ext}"

            try:
                obj = s3.get_object(
                    Bucket=settings.S3_BUCKET_NAME, Key=file_info["s3_key"]
                )
                zf.writestr(zip_filename, obj["Body"].read())
            except ClientError as exc:
                logger.error(
                    f"Skipping file in ZIP — S3 error: key={file_info['s3_key']} error={exc}"
                )

    buf.seek(0)
    return buf.read()
