# traffic-service/app/models.py
import uuid
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    BigInteger,
    Float,
    ForeignKey,
    DateTime,
    Date,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database import Base


class BaseModel(Base):
    """Abstract base model supplying timezone-aware created_at and updated_at timestamps for all tables"""
    __abstract__ = True

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now(), 
        nullable=False
    )


class Server(BaseModel):
    """Server node checkpoint record in traffic_db"""
    __tablename__ = "servers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String, nullable=False)
    api_url = Column(String, nullable=False)
    api_token = Column(String, nullable=False)
    xui_version = Column(String(50), default="v3.5.0", nullable=True)  # Added for 3X-UI v3.5.0 support
    target_inbound_id = Column(Integer, default=1, nullable=False)
    price_per_gb = Column(Float, default=1000.0, nullable=False)
    price_per_gb_usd = Column(Float, default=0.05, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    configs = relationship("UserConfig", back_populates="server", cascade="all, delete-orphan")


class UserConfig(BaseModel):
    """Traffic polling checkpoint for client byte tracking in traffic_db"""
    __tablename__ = "user_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    server_id = Column(UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    client_email = Column(String, index=True, nullable=False)
    client_uuid = Column(String, unique=True, nullable=False)
    last_traffic = Column(BigInteger, default=0, nullable=False)  # Cumulative byte counter
    last_ip = Column(String(50), nullable=True)                  # Last detected client IP
    last_isp = Column(String(100), default="Other", nullable=True)# Last matched ISP name
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    server = relationship("Server", back_populates="configs")


class DailyTrafficUsage(BaseModel):
    """
    Dedicated timeseries aggregated traffic usage model in the Metering & Traffic domain.
    Decoupled from billing_db and isolated per microservices architecture standards.
    """
    __tablename__ = "daily_traffic_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_traffic_user_daily"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    date = Column(Date, nullable=False, index=True)
    total_bytes = Column(BigInteger, default=0, nullable=False)


class UserIspTrafficUsage(BaseModel):
    """
    Cumulative bandwidth consumption segmented by User and IP address / ISP.
    A new row is added whenever a new client IP is observed.
    """
    __tablename__ = "user_isp_traffic_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "ip_address", name="uq_user_ip_usage"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    ip_address = Column(String(50), nullable=False, index=True, default="Direct / Unknown")
    last_ip = Column(String(50), nullable=True)
    isp_name = Column(String(100), nullable=False, index=True)
    country = Column(String(10), default="Unknown", nullable=False)
    total_bytes = Column(BigInteger, default=0, nullable=False)


class TrafficSettings(BaseModel):
    """Local settings for traffic-service (e.g. timeout settings)."""
    __tablename__ = "traffic_settings"

    key = Column(String(50), primary_key=True, index=True)
    value = Column(String(255), nullable=False)