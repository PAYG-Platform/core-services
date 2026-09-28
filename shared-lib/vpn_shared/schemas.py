import enum
import uuid
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class RoleEnum(str, enum.Enum):
    """System-wide Role Enum for Admin and Client users."""
    CLIENT = "Client"
    ADMIN = "Admin"


class CurrencyEnum(str, enum.Enum):
    """System-wide Currency Enum for dual-currency billing."""
    IRT = "IRT"
    USD = "USD"


class UserCreatedEvent(BaseModel):
    """Event emitted by IAM Service when a new user registers or is created."""
    user_id: uuid.UUID = Field(..., description="Unique User UUID from IAM database")
    username: str = Field(..., description="User's unique username")
    email: str = Field(..., description="User's email address")
    role: RoleEnum = Field(default=RoleEnum.CLIENT, description="User role in system")
    parent_id: Optional[uuid.UUID] = Field(default=None, description="Seller UUID if this user is a client")
    gift_balance: float = Field(default=0.0, description="Initial non-refundable gift credit in user account currency")
    billing_currency: str = Field(default="IRT", description="Immutable account billing currency: IRT or USD")


class TrafficConsumedEvent(BaseModel):
    """Event emitted by Traffic Worker when bandwidth consumption is detected."""
    user_id: uuid.UUID = Field(..., description="Target User UUID")
    server_id: uuid.UUID = Field(..., description="Server UUID where traffic occurred")
    used_bytes: int = Field(..., gt=0, description="Data used in bytes during interval")
    price_per_gb: float = Field(..., description="Dynamic server rate per GB in Toman stored in DB")
    price_per_gb_usd: float = Field(default=0.05, ge=0.0, description="Dynamic server rate per GB in USD stored in DB")


class WalletEmptyEvent(BaseModel):
    """Event emitted by Billing Service when a client's balance drops to 0 or below."""
    user_id: uuid.UUID = Field(..., description="User UUID whose wallet is empty")


class WalletFundedEvent(BaseModel):
    """Event emitted by Billing Service when a top-up is approved and wallet has balance."""
    user_id: uuid.UUID = Field(..., description="User UUID whose wallet received funds")
    amount_credited: float = Field(..., description="Amount added in account currency")
    currency: str = Field(default="IRT", description="Currency of credited funds: IRT or USD")


class ServerOfflineEvent(BaseModel):
    """Event emitted when a 3x-ui server is unreachable or offline."""
    server_id: uuid.UUID = Field(..., description="UUID of the offline server")
    server_name: str = Field(..., description="Name/Label of the server")
    api_url: str = Field(..., description="API Endpoint URL of the server")


class SystemAuditLogEvent(BaseModel):
    """Event emitted by any microservice to log operational activities into Audit Service."""
    actor: str = Field(..., description="Entity performing action e.g., 'System Cron', 'Seller: Ali'")
    actor_type: str = Field(..., description="'System', 'Seller', or 'Admin'")
    event_type: str = Field(..., description="Type of event e.g., 'sync_billing', 'add_client'")
    description: str = Field(..., description="Human-readable event description")
    status: str = Field(default="Success", description="'Success', 'Warning', or 'Error'")
    details: Optional[Dict[str, Any]] = Field(default=None, description="Additional metadata / payload")