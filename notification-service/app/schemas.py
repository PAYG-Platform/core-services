# backend/notification-service/app/schemas.py
import uuid
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


# ==========================================
# NOTIFICATION INBOX SCHEMAS
# ==========================================

class NotificationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    message: str
    category: str
    link_url: Optional[str] = None
    is_seen: bool
    seen_at: Optional[datetime] = None
    requires_acknowledgement: bool = False
    is_acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PaginatedNotificationsResponse(BaseModel):
    items: List[NotificationResponse]
    total: int
    page: int
    limit: int
    total_pages: int
    unseen_count: int


class MarkSeenRequest(BaseModel):
    notification_ids: List[uuid.UUID]


# ==========================================
# NOTIFICATION PREFERENCES SCHEMAS
# ==========================================

class ClientNotificationSettingsResponse(BaseModel):
    notify_low_balance: bool
    low_balance_threshold: float
    currency: str = "IRT"
    notify_server_outage: bool
    notify_topup_approval: bool


class ClientNotificationSettingsUpdate(BaseModel):
    notify_low_balance: Optional[bool] = None
    low_balance_threshold: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = None
    notify_server_outage: Optional[bool] = None
    notify_topup_approval: Optional[bool] = None


class SellerNotificationSettingsResponse(BaseModel):
    notify_client_topup: bool
    notify_client_out_of_credit: bool
    notify_admin_debt_limit: bool
    debt_limit_threshold: float


class SellerNotificationSettingsUpdate(BaseModel):
    notify_client_topup: Optional[bool] = None
    notify_client_out_of_credit: Optional[bool] = None
    notify_admin_debt_limit: Optional[bool] = None
    debt_limit_threshold: Optional[float] = Field(None, ge=0)


# ==========================================
# TELEGRAM BOT & LINKING SCHEMAS
# ==========================================

class TelegramBotConfigResponse(BaseModel):
    bot_username: str
    bot_token: Optional[str] = None
    admin_telegram_id: Optional[str] = None


class TelegramBotConfigUpdate(BaseModel):
    bot_username: Optional[str] = None
    bot_token: Optional[str] = None
    admin_telegram_id: Optional[str] = None


class TelegramLinkResponse(BaseModel):
    telegram_link: str
    token: str
    is_connected: bool
    telegram_id: Optional[str] = None


class TelegramWebhookStart(BaseModel):
    start_token: str
    telegram_chat_id: str