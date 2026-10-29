# backend/config-service/app/models.py
import uuid
from sqlalchemy import Column, String, Boolean, DateTime, Text, Integer, Float
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.database import Base


class BaseModel(Base):
    """Abstract base model supplying timezone-aware created_at and updated_at timestamps."""
    __abstract__ = True

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now(), 
        nullable=False
    )


class PlatformSystemConfig(BaseModel):
    """Platform-wide brand metadata and global maintenance lockdown settings."""
    __tablename__ = "platform_system_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    
    # Public Branding & UI Defaults
    brand_name = Column(String(100), default="PAYG MNG", nullable=False)
    default_currency = Column(String(10), default="IRT", nullable=False)
    default_language = Column(String(10), default="en", nullable=False)
    default_theme = Column(String(10), default="dark", nullable=False)
    timezone = Column(String(50), default="Asia/Tehran", nullable=False)

    # PAYG Billing Sync Interval in Minutes
    billing_sync_interval = Column(Integer, default=5, nullable=False)

    # 3X-UI Request Timeout in Seconds
    xui_request_timeout = Column(Float, default=15.0, nullable=False)

    # Client Self-Service MNG Status Toggle Cooldown in Minutes (0 = Immediate / No Cooldown)
    client_status_cooldown_minutes = Column(Integer, default=60, nullable=False)

    # Route Replacement / Block Change Request Policy
    route_replace_cooldown_days = Column(Integer, default=0, nullable=False)
    disable_client_route_replacement = Column(Boolean, default=False, nullable=False)

    # System Maintenance Lockdown
    is_maintenance_mode = Column(Boolean, default=False, nullable=False)
    maintenance_message = Column(
        Text, 
        default="System is undergoing scheduled maintenance. Please check back shortly.", 
        nullable=False
    )