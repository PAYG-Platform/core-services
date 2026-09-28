# backend/billing-service/app/utils.py
import os
import uuid
import base64
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Reads directory from environment with safe local fallback
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
try:
    RECEIPTS_DIR = os.path.join(UPLOAD_DIR, "receipts")
    os.makedirs(RECEIPTS_DIR, exist_ok=True)
except Exception:
    UPLOAD_DIR = "./uploads"
    RECEIPTS_DIR = os.path.join(UPLOAD_DIR, "receipts")
    os.makedirs(RECEIPTS_DIR, exist_ok=True)


def save_base64_receipt_to_disk(base64_data: Optional[str]) -> Optional[str]:
    """
    Decodes a base64 Data URI (e.g. data:image/png;base64,xxxx), saves the binary 
    file to the uploads/receipts directory, and returns the segmented path '/uploads/receipts/{filename}'.
    If it's already a clean URL or path, returns it as-is.
    """
    if not base64_data or not isinstance(base64_data, str):
        return None

    clean_data = base64_data.strip()

    # If it's already a relative path or remote URL, do not re-process
    if not clean_data.startswith("data:image/"):
        return clean_data

    try:
        # Match MIME type and extract base64 payload
        match = re.match(r"^data:image/([a-zA-Z0-9.+_-]+);base64,(.+)$", clean_data)
        if not match:
            return clean_data

        ext = match.group(1).lower()
        if ext == "jpeg":
            ext = "jpg"
        elif ext == "svg+xml":
            ext = "svg"

        raw_bytes = base64.b64decode(match.group(2))
        unique_filename = f"receipt_{uuid.uuid4().hex}.{ext}"
        file_path = os.path.join(RECEIPTS_DIR, unique_filename)

        with open(file_path, "wb") as f:
            f.write(raw_bytes)

        logger.info(f"Saved receipt image ({len(raw_bytes)} bytes) to {file_path}")
        return f"/uploads/receipts/{unique_filename}"
    except Exception as e:
        logger.error(f"Failed to decode and save base64 receipt to disk: {e}")
        return None