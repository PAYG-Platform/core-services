# backend/support-service/app/core/dependencies.py
import uuid
from typing import Dict, Any
from fastapi import Depends, Request
from vpn_shared.jwt_auth import (
    get_current_user_payload as shared_get_current_user_payload,
    extract_token_from_request,
    verify_jwt_token,
)


def get_current_user_payload(request: Request) -> Dict[str, Any]:
    """
    FastAPI dependency that extracts and statelessly verifies the incoming RS256 JWT
    from HttpOnly cookies or the 'Authorization: Bearer <token>' header.
    Preserves sub (User UUID string), role ('Admin' | 'Client'), and billing metadata.
    """
    return shared_get_current_user_payload(request)


def get_current_user_id(payload: Dict[str, Any] = Depends(get_current_user_payload)) -> uuid.UUID:
    """
    Extracts and converts the user's 'sub' claim from the verified JWT payload into a valid UUID object.
    Matches the pattern used across billing-service and iam-service.
    """
    raw_sub = payload.get("sub")
    return uuid.UUID(str(raw_sub))


def get_current_user(request: Request) -> Dict[str, Any]:
    """
    Convenience alias for ticket endpoints expecting a dictionary user payload.
    """
    return shared_get_current_user_payload(request)