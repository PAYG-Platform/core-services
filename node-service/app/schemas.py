# backend/node-service/app/schemas.py
import uuid
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


# ==========================================
# LOCATION / REGION SCHEMAS
# ==========================================

class LocationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Location/Region name e.g. United States, Germany")
    country_code: str = Field(..., min_length=2, max_length=10, description="Country code e.g. US, DE")
    flag: str = Field(default="🌐", description="Emoji flag or icon representation e.g. 🇺🇸, 🇩🇪")


class LocationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    country_code: Optional[str] = Field(None, min_length=2, max_length=10)
    flag: Optional[str] = None
    is_active: Optional[bool] = None


class LocationResponse(BaseModel):
    id: uuid.UUID
    name: str
    country_code: str
    flag: str
    is_active: bool
    total_capacity: int = 0
    used_capacity: int = 0
    is_full: bool = False
    server_count: int = 0
    avg_price_per_gb: float = 0.0
    min_price_per_gb: float = 0.0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class PublicLocationSummary(BaseModel):
    name: str
    country_code: str
    flag: str
    status: str             # "Available", "High Demand", "Maintenance"
    base_rate_display: str  # e.g., "From ~1,000 T / GB"

# ==========================================
# INBOUND LIMIT SCHEMAS
# ==========================================

class InboundLimitItem(BaseModel):
    inbound_id: int = Field(default=1, ge=1, description="Inbound ID configured on the 3x-ui panel")
    tag: Optional[str] = Field(None, description="Descriptive label e.g. 'XHTTP Port 2087'")
    protocol: str = Field(..., description="'vless_reality', 'xhttp', 'trojan', or 'vmess'")
    port: int = Field(default=443, ge=1, le=65535, description="Port for this specific inbound")
    domain_or_ip: Optional[str] = Field(None, description="Custom domain or SNI for this inbound")
    path: Optional[str] = Field(None, description="Path for XHTTP or WS, e.g. '/xhttp'")
    user_limit: int = Field(default=10, ge=1, description="Max client capacity for this inbound")
    used_limit: Optional[int] = Field(0, description="Current assigned clients")


# ==========================================
# SERVER SCHEMAS
# ==========================================

class ServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Display name of the server node")
    location_id: Optional[uuid.UUID] = Field(None, description="Location/Region UUID the server belongs to")
    api_url: str = Field(..., description="Base API URL of 3x-ui panel e.g. https://xcp.moshaservice.eu.cc:2053")
    api_token: str = Field(..., description="API Secret Token or Bearer Token for 3x-ui")
    xui_version: Optional[str] = "v3.5.0"
    # Make domain_or_ip and port optional with auto-fallback defaults:
    domain_or_ip: Optional[str] = Field(default="", description="Optional fallback domain or IP")
    port: Optional[int] = Field(default=443, ge=1, le=65535, description="Optional fallback port")
    protocol_type: Optional[str] = Field(default="vless", description="Primary protocol type")
    target_inbound_id: Optional[int] = Field(default=1, ge=1, description="Target default inbound ID")
    inbound_limits: List[InboundLimitItem] = Field(default_factory=list)
    price_per_gb: float = Field(default=1000.0, ge=0.0, description="Price per GB in Toman")
    price_per_gb_usd: float = Field(default=0.05, ge=0.0, description="Price per GB in USD")


class ServerResponse(BaseModel):
    id: uuid.UUID
    location_id: Optional[uuid.UUID] = None
    name: str
    api_url: str
    api_token: str
    domain_or_ip: Optional[str] = ""
    port: Optional[int] = 443
    protocol_type: Optional[str] = "vless"
    target_inbound_id: Optional[int] = 1
    inbound_limits: List[InboundLimitItem] = []
    price_per_gb: float
    price_per_gb_usd: float = 0.05
    is_active: bool
    is_reachable: bool = True
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    location_id: Optional[uuid.UUID] = None
    api_url: Optional[str] = None
    api_token: Optional[str] = None
    xui_version: Optional[str] = None
    domain_or_ip: Optional[str] = None
    port: Optional[int] = None
    protocol_type: Optional[str] = None
    target_inbound_id: Optional[int] = None
    inbound_limits: Optional[List[InboundLimitItem]] = None
    price_per_gb: Optional[float] = Field(None, ge=0.0, description="Update rate per GB in Toman")
    price_per_gb_usd: Optional[float] = Field(None, ge=0.0, description="Update rate per GB in USD")
    is_active: Optional[bool] = None


class TestConnectionRequest(BaseModel):
    api_url: str = Field(..., description="Base API URL of the 3x-ui panel")
    api_token: str = Field(..., description="API Secret Token or Bearer Token for 3x-ui")


# ==========================================
# SERVER REPLACEMENT / MIGRATION SCHEMAS
# ==========================================

class ServerReplaceRequest(BaseModel):
    target_server_ids: List[uuid.UUID] = Field(
        ..., 
        min_length=1, 
        description="One or more target replacement server UUIDs"
    )
    deactivate_old_server: bool = Field(
        default=True, 
        description="Whether to set the old blocked server is_active=False"
    )
    reason: Optional[str] = Field(
        default="GFW IP Block / Route Filtered", 
        description="Migration reason explained to clients"
    )


class ServerReplaceResponse(BaseModel):
    old_server_id: uuid.UUID
    old_server_name: str
    migrated_users_count: int
    target_servers: List[dict]
    unmigrated_count: int
    message: str


# ==========================================
# SUBSCRIPTION & CONFIG SCHEMAS
# ==========================================

class ClientConfigResponse(BaseModel):
    id: uuid.UUID
    server_id: uuid.UUID
    server_name: str
    location: str
    protocol: str
    price_per_gb: float
    price_per_gb_toman: Optional[float] = None  # Backward-compatible alias during transition
    currency: str = "IRT"
    uri: str
    data_used_bytes: int = 0

    class Config:
        from_attributes = True


class UserConfigsListResponse(BaseModel):
    subscription_url: str
    sub_token: str
    configs: List[ClientConfigResponse]


class ManualReassignRequest(BaseModel):
    config_id: Optional[uuid.UUID] = Field(None, description="Specific configuration UUID to reassign")
    location_id: uuid.UUID = Field(..., description="Target region/location UUID")
    server_id: uuid.UUID = Field(..., description="Target server UUID")
    inbound_id: int = Field(..., description="Target inbound ID on the server")
    protocol: Optional[str] = Field(None, description="Protocol type")
    reason: Optional[str] = Field("Admin manual re-assignment", description="Reason memo")


class AdminAddUserConfigRequest(BaseModel):
    location_id: uuid.UUID = Field(..., description="Target location UUID")
    server_id: uuid.UUID = Field(..., description="Target server UUID")
    inbound_id: int = Field(..., description="Target inbound ID")
    protocol: Optional[str] = Field(None, description="Protocol override")


class PublicLocationSummary(BaseModel):
    name: str
    country_code: str
    flag: str
    status: str             # "Available", "High Demand", "Maintenance"
    base_rate_display: str  # e.g., "From ~1,000 T / GB"


class ReplaceBlockedConfigRequest(BaseModel):
    client_isp: Optional[str] = Field(None, description="Client network provider / ISP (e.g. Irancell, MCI, Shatel)")
    language: Optional[str] = Field(None, description="User language code (en, fa, ru)")


class RoutePolicySyncRequest(BaseModel):
    route_replace_cooldown_days: Optional[int] = 0
    disable_client_route_replacement: Optional[bool] = False
    xui_request_timeout: Optional[float] = 15.0
