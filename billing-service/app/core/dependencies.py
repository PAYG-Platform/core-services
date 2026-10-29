# backend/billing-service/app/core/dependencies.py
import uuid
from fastapi import Depends
from mng_shared.jwt_auth import get_current_user_payload


def get_current_user_id(payload: dict = Depends(get_current_user_payload)) -> uuid.UUID:
    """
    Extracts the user's UUID from the authenticated JWT session payload.
    Automatically handles JWT extraction from HttpOnly Cookies or Authorization headers.
    """
    raw_sub = payload["sub"]
    return uuid.UUID(str(raw_sub))