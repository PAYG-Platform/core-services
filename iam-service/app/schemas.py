# backend/iam-service/app/schemas.py
import re
import uuid
from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict
from typing import List, Optional
from datetime import datetime
from app.core.passwords import is_common_weak_password


# ==========================================
# AUTHENTICATION & USER SCHEMAS
# ==========================================

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="Unique username")
    email: EmailStr = Field(..., description="Valid email address")
    password: str = Field(..., min_length=8, description="Password (min 8 characters)")
    confirm_password: str = Field(..., description="Must match password")
    language: Optional[str] = Field("en", max_length=10, description="Preferred language code e.g. en, fa, ru")
    billing_currency: Optional[str] = Field("IRT", description="Billing zone currency: IRT or USD")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*[a-zA-Z0-9]?$", v):
            raise ValueError(
                "Username must start with a letter and contain only "
                "alphanumeric characters and underscores."
            )
        return v.lower()

    @field_validator("password")
    @classmethod
    def validate_password_simplicity(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if is_common_weak_password(v):
            raise ValueError("Password is too simple or predictable. Please choose a stronger password.")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter (A-Z).")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter (a-z).")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one number (0-9).")
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>\-_=+\\/]", v):
            raise ValueError("Password must contain at least one special character (e.g. !@#$%^&*).")
        return v

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v: str, info) -> str:
        if "password" in info.data and v != info.data["password"]:
            raise ValueError("Passwords do not match.")
        return v


class UserLogin(BaseModel):
    identifier: str = Field(..., description="Email or Username")
    password: str = Field(..., description="Account password")


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: str
    role: str
    name: Optional[str] = None
    language: Optional[str] = "en"
    billing_currency: str = "IRT"
    parent_id: Optional[uuid.UUID] = None
    is_active: bool
    is_self_suspended: bool = False
    must_change_password: bool = False
    status_toggled_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ForcePasswordChangeRequest(BaseModel):
    new_password: str = Field(..., min_length=8)
    confirm_password: str = Field(...)

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v: str, info) -> str:
        if "new_password" in info.data and v != info.data["new_password"]:
            raise ValueError("New passwords do not match.")
        return v


class ForcePasswordChangeResponse(BaseModel):
    message: str
    recovery_keys: List[str]
    user: UserResponse


class PasswordVerificationRequest(BaseModel):
    password: str = Field(..., description="Current password for security verification")


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    name: Optional[str] = Field(None, max_length=100)


class PasswordResetEmailRequest(BaseModel):
    email: EmailStr = Field(..., description="User account email")


class PasswordResetConfirm(BaseModel):
    recovery_key: str = Field(..., min_length=12, max_length=12, description="12-character recovery key")
    new_password: str = Field(..., min_length=6, description="New password")
    confirm_password: str = Field(..., description="Confirm new password")

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v: str, info) -> str:
        if "new_password" in info.data and v != info.data["new_password"]:
            raise ValueError("New passwords do not match.")
        return v


class SignupResponse(BaseModel):
    message: str
    disclaimer: str
    user: UserResponse
    recovery_keys: List[str]  # Shown ONCE at registration
    access_token: str


class LoginResponse(BaseModel):
    message: str
    user: UserResponse


class TokenPayload(BaseModel):
    sub: str = Field(..., description="User UUID string")
    username: str
    email: str
    role: str
    parent_id: Optional[str] = None


# ==========================================
# CLIENT-SELLER CONNECTION SCHEMAS
# ==========================================

class SellerPublicResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: Optional[str] = None
    name: Optional[str] = None
    created_at: datetime


class PaginatedSellerDirectoryResponse(BaseModel):
    items: List[SellerPublicResponse]
    total: int
    page: int
    limit: int
    total_pages: int


class ConnectionRequestCreate(BaseModel):
    seller_id: uuid.UUID = Field(..., description="UUID of the seller to connect with")
    note: Optional[str] = Field(None, description="Note or description sent to the seller")


class ConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    seller_id: uuid.UUID
    status: str  # 'Pending', 'Active', 'Rejected', 'Removed'
    request_note: Optional[str] = None
    is_auto_connected: bool
    seller_username: Optional[str] = None
    client_username: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ConnectionProcessAction(BaseModel):
    action: str = Field(..., description="'Approve' or 'Reject'")


class DirectClientCreateBySeller(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6)


class UserProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: str
    role: str
    name: Optional[str] = None
    language: Optional[str] = "en"
    billing_currency: str = "IRT"
    parent_id: Optional[uuid.UUID] = None
    is_active: bool
    is_self_suspended: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UserProfileUpdate(BaseModel):
    email: Optional[EmailStr] = None
    name: Optional[str] = None
    language: Optional[str] = Field(None, max_length=10)


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(..., description="Current password")
    new_password: str = Field(..., min_length=6, description="New password")
    confirm_password: str = Field(..., description="Must match new_password")

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v: str, info) -> str:
        if "new_password" in info.data and v != info.data["new_password"]:
            raise ValueError("New passwords do not match.")
        return v


class RecoveryKeysResponse(BaseModel):
    message: str
    count: int
    keys: List[str]


class TelegramLinkResponse(BaseModel):
    telegram_link: str
    token: str
    is_connected: bool
    telegram_id: Optional[str] = None


class TelegramLinkConfirmRequest(BaseModel):
    token: str = Field(..., description="Single-use start linking token")
    telegram_id: str = Field(..., description="Telegram user/chat ID")


class AdminCreateClientRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6)
    initial_gift_balance: float = Field(default=0.0, ge=0.0)
    billing_currency: Optional[str] = Field(default="IRT", description="Account currency: IRT or USD")