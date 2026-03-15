"""
AI Service — document classification, data extraction, and validation using Claude.

All functions are synchronous (called inside asyncio.run() from Celery workers).
Uses Claude's native PDF and image support via the document/image block types.

Pipeline per physical file:
  1. classify_physical_file()  → doc_type, confidence, ambiguity_type
  2. extract_data()             → structured fields from logical_doc
  3. tier1_validate()           → per-document rules from ValidationAI agent config
  4. tier2_validate()           → cross-document rules (called once all T1 pass)
"""
import base64
import json
import re
from datetime import datetime, timezone
from typing import Optional

import boto3
from anthropic import Anthropic

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger("ai_service")

# Models — use haiku for speed/cost, sonnet for Tier 2 cross-doc reasoning
_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-5"

# Known document types for classification prompt
_ALL_DOC_TYPES = [
    "AADHAAR", "PAN_CARD", "BANK_STATEMENT", "ITR", "GST_CERT", "GST_RETURN",
    "AUDITED_PL", "TITLE_DEED", "PROPERTY_TAX", "NOC", "UDYAM", "MOA", "AOA",
    "COI", "ELECTRICITY_BILL", "PASSPORT_PHOTO", "PARTNERSHIP_DEED", "OTHER",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
    )


def _download_from_s3(s3_key: str) -> bytes:
    """Download a file from S3 and return its raw bytes."""
    s3 = _s3_client()
    obj = s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=s3_key)
    return obj["Body"].read()


def _detect_media_type(filename: str, file_bytes: bytes) -> str:
    """Detect media type from filename extension."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    mapping = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }
    return mapping.get(ext, "application/pdf")


def _build_document_block(file_bytes: bytes, media_type: str) -> dict:
    """Build a Claude API document or image block from file bytes."""
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    if media_type == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        }
    else:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }


def _parse_json_response(text: str) -> dict:
    """Extract JSON from a Claude response, handling markdown code blocks."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    # Find first { ... }
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(cleaned)


def _get_anthropic_client() -> Anthropic:
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _get_extraction_agent_config(db_sync) -> dict:
    """Fetch ExtractionAI agent config from DB (synchronous via Motor not available here — use default)."""
    # In Celery context we don't have async Motor. Return defaults — the worker
    # passes these through kwargs when possible, or we use the model defaults.
    return {}


# ---------------------------------------------------------------------------
# 1. Classification
# ---------------------------------------------------------------------------

def classify_physical_file(
    s3_key: str,
    original_filename: str,
    extraction_prompt_additions: str = "",
) -> dict:
    """
    Download file from S3 and classify using Claude.

    Returns:
        {
            "doc_type": "BANK_STATEMENT",
            "confidence": 92,
            "ambiguity_type": "NORMAL",   # NORMAL | BUNDLED | PARTIAL
            "reasoning": "...",
        }
    """
    _FAIL = {"doc_type": "OTHER", "confidence": 0, "ambiguity_type": "NORMAL"}

    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not configured — returning OTHER classification")
        return {**_FAIL, "reasoning": "API key not configured"}

    # Step A: Download from S3
    try:
        logger.info(f"[CLASSIFY] Downloading from S3: {s3_key}")
        file_bytes = _download_from_s3(s3_key)
        logger.info(f"[CLASSIFY] Downloaded {len(file_bytes)} bytes from S3")
    except Exception as exc:
        logger.error(f"[CLASSIFY] S3 download FAILED for {s3_key}: {exc}", exc_info=True)
        return {**_FAIL, "reasoning": f"S3 download failed: {exc}"}

    # Step B: Build document block
    try:
        media_type = _detect_media_type(original_filename, file_bytes)
        doc_block = _build_document_block(file_bytes, media_type)
        logger.info(f"[CLASSIFY] Built doc block: media_type={media_type} b64_size~{len(file_bytes)*4//3}")
    except Exception as exc:
        logger.error(f"[CLASSIFY] Doc block build FAILED: {exc}", exc_info=True)
        return {**_FAIL, "reasoning": f"Doc block build failed: {exc}"}

    doc_types_str = ", ".join(_ALL_DOC_TYPES)
    prompt = f"""You are a document classifier for an Indian SME lending platform.

Classify the document shown. Return ONLY a JSON object with these fields:
- "doc_type": one of [{doc_types_str}]
- "confidence": integer 0-100 (your confidence in the classification)
- "ambiguity_type": one of ["NORMAL", "BUNDLED", "PARTIAL"]
  - NORMAL: single document, single type
  - BUNDLED: multiple document types in one file (e.g. PAN + Aadhaar scanned together)
  - PARTIAL: one page/part of a multi-page document (e.g. only Jan-Jun of a bank statement)
- "reasoning": brief explanation (1-2 sentences)

Context: Indian SME lending. Documents may be in Hindi, English, or regional languages.
Financial years run April to March.
{extraction_prompt_additions}

Return ONLY the JSON object, no other text."""

    # Step C: Call Anthropic API
    try:
        logger.info(f"[CLASSIFY] Calling Anthropic API model={_HAIKU} for {original_filename}")
        client = _get_anthropic_client()
        response = client.messages.create(
            model=_HAIKU,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [doc_block, {"type": "text", "text": prompt}],
            }],
        )
        logger.info(f"[CLASSIFY] Anthropic API responded: stop_reason={response.stop_reason}")
    except Exception as exc:
        logger.error(f"[CLASSIFY] Anthropic API call FAILED for {original_filename}: {exc}", exc_info=True)
        return {**_FAIL, "reasoning": f"Anthropic API error: {exc}"}

    # Step D: Parse response
    try:
        result = _parse_json_response(response.content[0].text)
        result.setdefault("doc_type", "OTHER")
        result.setdefault("confidence", 50)
        result.setdefault("ambiguity_type", "NORMAL")
        result.setdefault("reasoning", "")
        logger.info(
            f"[CLASSIFY] Success: {original_filename} → type={result['doc_type']} "
            f"confidence={result['confidence']} ambiguity={result['ambiguity_type']}"
        )
        return result
    except Exception as exc:
        logger.error(f"[CLASSIFY] JSON parse error for {s3_key}: {exc} raw={response.content[0].text[:200]}")
        return {**_FAIL, "reasoning": str(exc)}


# ---------------------------------------------------------------------------
# 2. Data extraction
# ---------------------------------------------------------------------------

def extract_data(
    s3_key: str,
    original_filename: str,
    doc_type: str,
    fields_to_extract: list[str],
    extraction_prompt_additions: str = "",
) -> dict:
    """
    Extract structured fields from a document using Claude.

    Args:
        fields_to_extract: list of field names from ExtractionAI agent config
    Returns:
        dict of field_name → extracted_value (or null if not found)
    """
    if not settings.ANTHROPIC_API_KEY:
        return {field: None for field in fields_to_extract}

    file_bytes = _download_from_s3(s3_key)
    media_type = _detect_media_type(original_filename, file_bytes)
    doc_block = _build_document_block(file_bytes, media_type)

    fields_str = "\n".join(f'  - "{f}"' for f in fields_to_extract)
    prompt = f"""You are a data extraction specialist for an Indian SME lending platform.

Extract the following fields from this {doc_type.replace("_", " ")} document:

{fields_str}

Rules:
- Return ONLY a JSON object with the field names as keys
- Use null for fields that cannot be found or are not applicable
- Dates must be in ISO format: YYYY-MM-DD
- Monetary amounts in INR (numbers only, no currency symbols or commas)
- For bank statements with multiple months, compute averages across all months
- Account numbers: return only last 4 digits for security (e.g. "XXXX1234")

Context: Indian SME lending. Documents may be in Hindi or English.
{extraction_prompt_additions}

Return ONLY the JSON object."""

    client = _get_anthropic_client()
    response = client.messages.create(
        model=_HAIKU,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [doc_block, {"type": "text", "text": prompt}],
        }],
    )

    try:
        result = _parse_json_response(response.content[0].text)
        logger.info(f"Extracted {len(result)} fields from {doc_type} ({s3_key})")
        return result
    except Exception as exc:
        logger.error(f"Extraction JSON parse error for {s3_key}: {exc}")
        return {field: None for field in fields_to_extract}


# ---------------------------------------------------------------------------
# 3. Tier 1 validation — per-document rules
# ---------------------------------------------------------------------------

def evaluate_tier1_rule(
    rule: dict,
    extracted_data: dict,
    doc_type: str,
    file_size_bytes: Optional[int] = None,
) -> dict:
    """
    Evaluate a single Tier 1 rule against extracted document data.

    Returns:
        {"passed": bool, "message": str}
    """
    rule_id = rule["rule_id"]
    applicable_doc_type = rule.get("doc_type", "ANY")
    enabled = rule.get("enabled", True)

    # Skip disabled rules
    if not enabled:
        return {"passed": True, "message": "Rule disabled"}

    # Skip rules not applicable to this doc type
    if applicable_doc_type != "ANY" and applicable_doc_type != doc_type:
        return {"passed": True, "message": f"Rule {rule_id} not applicable to {doc_type}"}

    # T1-007: Minimum File Size — checked without Claude
    if rule_id == "T1-007" and file_size_bytes is not None:
        threshold = rule.get("threshold", 10240)
        if file_size_bytes < threshold:
            return {
                "passed": False,
                "message": f"File size {file_size_bytes} bytes is below minimum {threshold} bytes",
            }
        return {"passed": True, "message": f"File size {file_size_bytes} bytes OK"}

    if not settings.ANTHROPIC_API_KEY:
        return {"passed": True, "message": "ANTHROPIC_API_KEY not set — rule skipped"}

    # All other rules: ask Claude with extracted data + rule prompt
    custom_prompt = rule.get("custom_prompt", "")
    threshold = rule.get("threshold")

    extracted_str = json.dumps(extracted_data, indent=2, default=str)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""You are a document validation specialist for an Indian SME lending platform.

Today's date: {today_str}

Document type: {doc_type}
Rule ID: {rule_id}
Rule name: {rule.get("rule_name", "")}
{f"Threshold: {threshold}" if threshold is not None else ""}

Extracted document data:
{extracted_str}

Validation instruction:
{custom_prompt}

Respond with ONLY a JSON object:
{{"passed": true/false, "message": "brief explanation of outcome (1 sentence)"}}"""

    client = _get_anthropic_client()
    try:
        response = client.messages.create(
            model=_HAIKU,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        result = _parse_json_response(response.content[0].text)
        return {
            "passed": bool(result.get("passed", True)),
            "message": str(result.get("message", "")),
        }
    except Exception as exc:
        logger.error(f"Tier1 rule {rule_id} evaluation error: {exc}")
        return {"passed": True, "message": f"Rule evaluation error (skipped): {exc}"}


def run_tier1_rules(
    doc_type: str,
    extracted_data: dict,
    tier1_rules: list[dict],
    file_size_bytes: Optional[int] = None,
) -> dict:
    """
    Run all Tier 1 rules applicable to a document.

    Returns:
        {
            "passed": bool,                          # True only if ALL applicable rules pass
            "rule_results": [...],                   # One entry per evaluated rule
            "failed_rule_ids": [...]
        }
    """
    rule_results = []
    failed_rule_ids = []

    for rule in tier1_rules:
        result = evaluate_tier1_rule(rule, extracted_data, doc_type, file_size_bytes)
        rule_results.append({
            "rule_id": rule["rule_id"],
            "rule_name": rule.get("rule_name", ""),
            "passed": result["passed"],
            "message": result["message"],
        })
        if not result["passed"]:
            failed_rule_ids.append(rule["rule_id"])

    overall_passed = len(failed_rule_ids) == 0
    return {
        "passed": overall_passed,
        "rule_results": rule_results,
        "failed_rule_ids": failed_rule_ids,
    }


# ---------------------------------------------------------------------------
# 4. Tier 2 validation — cross-document rules
# ---------------------------------------------------------------------------

def run_tier2_rules(
    lead_summary: dict,
    tier2_rules: list[dict],
    validation_prompt_additions: str = "",
) -> dict:
    """
    Run all Tier 2 cross-document consistency rules via Claude.

    Args:
        lead_summary: {
            "lead_id": str,
            "borrower_name": str,
            "entity_type": str,
            "logical_docs": [{"doc_type": str, "extracted_data": dict}, ...]
        }
        tier2_rules: list of ValidationRule dicts from ValidationAI agent config

    Returns:
        {
            "passed": bool,
            "rule_results": [...],
            "failed_rule_ids": [...]
        }
    """
    if not settings.ANTHROPIC_API_KEY:
        return {"passed": True, "rule_results": [], "failed_rule_ids": []}

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lead_data_str = json.dumps(lead_summary, indent=2, default=str)
    rules_str = json.dumps(tier2_rules, indent=2)

    prompt = f"""You are a cross-document validation specialist for an Indian SME lending platform.

Today's date: {today_str}
Entity type: {lead_summary.get("entity_type", "UNKNOWN")}

All extracted document data for this lead application:
{lead_data_str}

Validation rules to evaluate:
{rules_str}

{validation_prompt_additions}

For each rule, determine if the cross-document data passes or fails the rule.
If data needed for a rule is not available, return "passed": true with message "SKIP - data not available".

Return ONLY a JSON object:
{{
  "rule_results": [
    {{"rule_id": "T2-001", "passed": true/false, "message": "brief explanation"}}
  ]
}}"""

    client = _get_anthropic_client()
    try:
        response = client.messages.create(
            model=_SONNET,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = _parse_json_response(response.content[0].text)
        rule_results = parsed.get("rule_results", [])

        # Add rule names from the config
        rule_name_map = {r["rule_id"]: r.get("rule_name", "") for r in tier2_rules}
        for rr in rule_results:
            rr.setdefault("rule_name", rule_name_map.get(rr["rule_id"], ""))

        failed_rule_ids = [rr["rule_id"] for rr in rule_results if not rr.get("passed", True)]
        return {
            "passed": len(failed_rule_ids) == 0,
            "rule_results": rule_results,
            "failed_rule_ids": failed_rule_ids,
        }
    except Exception as exc:
        logger.error(f"Tier 2 validation error for lead {lead_summary.get('lead_id')}: {exc}")
        return {"passed": False, "rule_results": [], "failed_rule_ids": [], "error": str(exc)}
