# backend/iam-service/app/models.py
import uuid
from sqlalchemy import (
    Column,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Enum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from vpn_shared.schemas import RoleEnum
from app.database import Base


class BaseModel(Base):
    """Abstract base model supplying created_at and updated_at timestamps for all tables"""
    __abstract__ = True

    created_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now(), 
        nullable=False
    )


class User(BaseModel):
    """Core User account model for Clients and Admins"""
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(RoleEnum), default=RoleEnum.CLIENT, nullable=False)

    name = Column(String(100), nullable=True)
    language = Column(String(10), default="en", nullable=False)
    billing_currency = Column(String(10), default="IRT", nullable=False)

    is_active = Column(Boolean, default=True, nullable=False)
    is_self_suspended = Column(Boolean, default=False, nullable=False)
    status_toggled_at = Column(DateTime(timezone=True), nullable=True)
    must_change_password = Column(Boolean, default=False, nullable=False)

    # Telegram Identity & Account Linking
    telegram_id = Column(String(50), unique=True, index=True, nullable=True)
    telegram_link_token = Column(String(64), unique=True, index=True, nullable=True)

    # Relationships
    recovery_keys = relationship(
        "RecoveryKey", 
        back_populates="user", 
        cascade="all, delete-orphan"
    )


class RecoveryKey(BaseModel):
    """Password recovery key model for user self-service account recovery"""
    __tablename__ = "recovery_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True), 
        ForeignKey("users.id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )
    key_code = Column(String(12), unique=True, index=True, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="recovery_keys")


class IamSettings(BaseModel):
    """Local replicated settings in iam_db received via messaging."""
    __tablename__ = "iam_settings"

    key = Column(String(50), primary_key=True, index=True)
    value = Column(String(255), nullable=False)