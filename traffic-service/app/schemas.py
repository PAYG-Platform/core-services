# traffic-service/app/schemas.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional


class TrafficConsumedEventPayload(BaseModel):
    user_id: uuid.UUID = Field(..., description="Target user UUID")
    server_id: uuid.UUID = Field(..., description="Server UUID where traffic was consumed")
    used_bytes: int = Field(..., gt=0, description="Delta bytes consumed during sync interval")
    price_per_gb: float = Field(..., ge=0.0, description="Server price per GB in Toman")
    price_per_gb_usd: float = Field(default=0.05, ge=0.0, description="Server price per GB in USD")


class ServerCheckpoint(BaseModel):
    id: uuid.UUID
    name: str
    api_url: str
    xui_version: Optional[str] = "v3.5.0"
    target_inbound_id: Optional[int] = 1
    price_per_gb: float
    price_per_gb_usd: float = 0.05
    is_active: bool

    class Config:
        from_attributes = True


class UserConfigCheckpoint(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    server_id: uuid.UUID
    client_email: str
    client_uuid: str
    last_traffic: int
    is_active: bool

    class Config:
        from_attributes = True