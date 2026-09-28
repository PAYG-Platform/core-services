# backend/notification-service/app/models.py
import uuid
from sqlalchemy import Column, String, Boolean, DateTime, Text, Float, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.database import Base


class BaseModel(Base):
    """Abstract base model supplying created_at and updated_at timestamps."""
    __abstract__ = True

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class UserAccount(BaseModel):
    """Local replicated user and role store in notification_db."""
    __tablename__ = "user_accounts"

    user_id = Column(UUID(as_uuid=True), primary_key=True, index=True)
    username = Column(String(50), nullable=True)
    role = Column(String(20), nullable=False, default="Client")  # 'Admin' or 'Client'
    billing_currency = Column(String(10), default="IRT", nullable=False)


class Notification(BaseModel):
    """In-app notification message inbox item."""
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)  # References User UUID
    
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    category = Column(String(50), nullable=False, index=True)  # 'ticket', 'billing', 'server', 'system', 'unallocated_credit', 'server_replaced'
    link_url = Column(String(255), nullable=True)             # e.g., '/client/support', '/admin/topups'
    
    is_seen = Column(Boolean, default=False, nullable=False, index=True)
    seen_at = Column(DateTime(timezone=True), nullable=True)
    requires_acknowledgement = Column(Boolean, default=False, nullable=False, index=True)
    is_acknowledged = Column(Boolean, default=False, nullable=False, index=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)


class UserNotificationPreferences(BaseModel):
    """User alert toggles and threshold values."""
    __tablename__ = "user_notification_preferences"

    user_id = Column(UUID(as_uuid=True), primary_key=True, index=True)  # References User UUID

    # Client Notification Preferences
    currency = Column(String(10), default="IRT", nullable=False)
    notify_low_balance = Column(Boolean, default=True, nullable=False)
    low_balance_threshold = Column(Float, default=10000.0, nullable=False)  # Toman or USD based on currency
    is_low_balance_warned = Column(Boolean, default=False, nullable=False)  # Latch: True once warned until refilled
    is_empty_warned = Column(Boolean, default=False, nullable=False)        # Latch: True once warned for empty credit
    last_grace_day_notified = Column(Integer, default=0, nullable=False)    # Tracks last grace day reminder index
    notify_server_outage = Column(Boolean, default=True, nullable=False)
    notify_topup_approval = Column(Boolean, default=True, nullable=False)

    # Seller Notification Preferences
    notify_client_topup = Column(Boolean, default=True, nullable=True)
    notify_client_out_of_credit = Column(Boolean, default=True, nullable=True)
    notify_admin_debt_limit = Column(Boolean, default=True, nullable=True)
    debt_limit_threshold = Column(Float, default=50000.0, nullable=True)


class TelegramBotConfig(BaseModel):
    """Platform Telegram Bot API credentials managed by Admin."""
    __tablename__ = "telegram_bot_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_token = Column(String(255), nullable=True)
    bot_username = Column(String(100), default="PAYG_VPN_Bot", nullable=False)
    admin_telegram_id = Column(String(50), nullable=True)