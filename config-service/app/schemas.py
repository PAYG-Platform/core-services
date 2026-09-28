# backend/config-service/app/schemas.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class PublicPlatformConfigResponse(BaseModel):
    brand_name: str
    default_currency: str
    default_language: str
    default_theme: str
    timezone: str
    billing_sync_interval: int = 5
    xui_request_timeout: float = 15.0
    client_status_cooldown_minutes: int = 60
    route_replace_cooldown_days: int = 0
    disable_client_route_replacement: bool = False
    is_maintenance_mode: bool
    maintenance_message: str

    class Config:
        from_attributes = True


class PlatformConfigUpdate(BaseModel):
    brand_name: Optional[str] = None
    default_currency: Optional[str] = None
    default_language: Optional[str] = None
    default_theme: Optional[str] = None
    timezone: Optional[str] = None
    billing_sync_interval: Optional[int] = 5
    xui_request_timeout: Optional[float] = Field(None, ge=1.0)
    client_status_cooldown_minutes: Optional[int] = Field(None, ge=0)
    route_replace_cooldown_days: Optional[int] = Field(None, ge=0)
    disable_client_route_replacement: Optional[bool] = None
    is_maintenance_mode: Optional[bool] = None
    maintenance_message: Optional[str] = None