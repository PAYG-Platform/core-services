# backend/node-service/app/models.py
import uuid
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Float,
    BigInteger,
    ForeignKey,
    DateTime,
    UniqueConstraint,
    Index,
    text,
    Text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

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


class Location(BaseModel):
    """Region / Location definition (e.g. US, Germany, Netherlands)."""
    __tablename__ = "locations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(100), nullable=False)
    country_code = Column(String(10), nullable=False)
    flag = Column(String(10), default="🌐", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    servers = relationship("Server", back_populates="location")


class Server(BaseModel):
    """3x-ui VPN Server Node with regional location and protocol user capacity limits."""
    __tablename__ = "servers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    location_id = Column(UUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True)
    
    name = Column(String, nullable=False)
    api_url = Column(String, nullable=False)
    api_token = Column(String, nullable=False)
    xui_version = Column(String(50), default="v3.5.0", nullable=True)
    domain_or_ip = Column(String, nullable=True)
    port = Column(Integer, default=443, nullable=True)
    protocol_type = Column(String(20), default="vless", nullable=False)
    
    target_inbound_id = Column(Integer, default=1, nullable=False)
    inbound_limits = Column(JSONB, default=list, nullable=False)
    
    price_per_gb = Column(Float, default=1000.0, nullable=False)
    price_per_gb_usd = Column(Float, default=0.05, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_reachable = Column(Boolean, default=True, nullable=False)
    last_unreachable_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    # Relationships
    location = relationship("Location", back_populates="servers")
    configs = relationship("UserConfig", back_populates="server", cascade="all, delete-orphan")


class UserConfig(BaseModel):
    """
    VPN configuration assigned to a user.
    Users can have multiple configs in the same region if assigned by Admin.
    """
    __tablename__ = "user_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    location_id = Column(UUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, index=True)
    server_id = Column(UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    inbound_id = Column(Integer, default=1, nullable=False)
    protocol = Column(String(30), default="vless_reality", nullable=False)
    client_email = Column(String, index=True, nullable=False)
    client_uuid = Column(String, unique=True, nullable=False)
    uri = Column(Text, nullable=True)
    last_traffic = Column(BigInteger, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    server = relationship("Server", back_populates="configs")
    location = relationship("Location")


class ConfigReservation(BaseModel):
    """
    Temporary reservation of one concrete VPN capacity slot.
    Lifecycle: Reserved -> Consumed | Released | Expired.
    """
    __tablename__ = "config_reservations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    location_id = Column(UUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, index=True)
    server_id = Column(UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    inbound_id = Column(Integer, nullable=False, index=True)
    protocol = Column(String(30), nullable=False, index=True)
    status = Column(String(30), default="Reserved", nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    topup_transaction_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # Relationships
    server = relationship("Server")
    location = relationship("Location")

    # Partial index prevents simultaneous active reservations for the same user in the same region
    __table_args__ = (
        Index(
            "uq_active_config_reservation_user_location",
            "user_id",
            "location_id",
            unique=True,
            postgresql_where=text("status IN ('Pending', 'Reserved')"),
        ),
    )


class NodeSettings(BaseModel):
    """Local replicated settings in node_db received via messaging."""
    __tablename__ = "node_settings"

    key = Column(String(50), primary_key=True, index=True)
    value = Column(String(255), nullable=False)


class UserSubscription(BaseModel):
    """Stores persistent subscription token, replicated username, credit authorization, and language."""
    __tablename__ = "user_subscriptions"

    user_id = Column(UUID(as_uuid=True), primary_key=True, index=True)
    username = Column(String(50), nullable=True, index=True)
    language = Column(String(10), default="en", nullable=True)
    sub_token = Column(String(64), unique=True, index=True, nullable=False)
    billing_currency = Column(String(10), default="IRT", nullable=False)
    # Financial Credit Authorization (True if Gift + max(0, Paid) > safety_cutoff)
    has_credit = Column(Boolean, default=False, nullable=False)
    # Status Flags
    is_admin_suspended = Column(Boolean, default=False, nullable=False)
    is_self_suspended = Column(Boolean, default=False, nullable=False)


class ServerLog(BaseModel):
    """Audit log capturing real HTTP calls and responses to 3x-ui server panels."""
    __tablename__ = "server_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    server_id = Column(UUID(as_uuid=True), ForeignKey("servers.id", ondelete="SET NULL"), nullable=True, index=True)
    server_name = Column(String(100), nullable=True)
    endpoint = Column(String(255), nullable=False)
    method = Column(String(10), default="GET", nullable=False)
    status_code = Column(Integer, nullable=True)
    response_time_ms = Column(Integer, nullable=True)
    status = Column(String(20), default="Success", nullable=False)  # 'Success', 'Warning', 'Error'
    event_type = Column(String(50), default="http_call", nullable=False)
    description = Column(String(255), nullable=False)
    details = Column(JSONB, default=dict, nullable=False)


class UserBlockedRouteHistory(BaseModel):
    """
    Historical blacklist of inbounds and hosts that a user has flagged as blocked.
    Prevents the replacement algorithm from ever re-assigning these blocked nodes to the user.
    """
    __tablename__ = "user_blocked_route_histories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    location_id = Column(UUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, index=True)
    server_id = Column(UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    inbound_id = Column(Integer, nullable=False)
    protocol = Column(String(30), nullable=False)
    host_or_sni = Column(String(255), nullable=True)
    client_uuid = Column(String(255), nullable=True)
    client_email = Column(String(255), nullable=True)
    is_deleted_from_panel = Column(Boolean, default=False, nullable=False)

    # Relationships
    location = relationship("Location")
    server = relationship("Server")