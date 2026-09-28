# backend/billing-service/app/models.py
import uuid
from sqlalchemy import (
    Column,
    String,
    Float,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
    Integer,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database import Base


class BaseModel(Base):
    """Abstract Base Model providing automated created_at and updated_at timestamps"""
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


class Account(BaseModel):
    """
    Local financial account representation for a user in the Billing Service.
    Tracks user identities directly associated with platform wallets.
    """
    __tablename__ = "accounts"

    user_id = Column(UUID(as_uuid=True), primary_key=True, index=True)  # Matches IAM user.id UUID
    username = Column(String(50), nullable=True, index=True)  # Replicated from IAM via RabbitMQ
    role = Column(String(20), nullable=False, default="Client")  # 'Admin', 'Client'
    billing_currency = Column(String(10), default="IRT", nullable=False)  # 'IRT' or 'USD'

    # Relationships
    wallets = relationship("WalletBucket", back_populates="account", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="account", cascade="all, delete-orphan")


class WalletBucket(BaseModel):
    """
    Represents an isolated pool of funds within a user's wallet.
    
    Bucket Types:
    - 'GIFT': System promotional credit (non-refundable).
    - 'FIAT': Direct transfer to Platform Shetab/Bank accounts per region.
    - 'CRYPTO': Direct deposit to central Platform USDT wallet.
    """
    __tablename__ = "wallet_buckets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True), 
        ForeignKey("accounts.user_id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )

    bucket_type = Column(String(20), nullable=False)  # 'GIFT', 'FIAT', 'CRYPTO'

    # Location/Region partition
    location_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    location_name = Column(String(100), nullable=True)  # e.g., 'United States', 'Germany'

    balance = Column(Float, default=0.0, nullable=False)  # Current available balance in bucket currency
    currency = Column(String(10), default="IRT", nullable=False)  # 'IRT' or 'USD'
    empty_since = Column(DateTime(timezone=True), nullable=True)  # Tracks when balance hit 0 for grace period
    last_grace_day_notified = Column(Integer, default=0, nullable=False)  # Tracks grace day warning index (1, 2, 3...)
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    account = relationship("Account", back_populates="wallets")
    transactions = relationship("Transaction", back_populates="wallet_bucket")


class Transaction(BaseModel):
    """
    Immutable financial transaction record.
    Tracks all balance changes, top-ups, usage deductions, and refunds.
    """
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    reference_id = Column(String(50), unique=True, index=True, nullable=False)
    user_id = Column(
        UUID(as_uuid=True), 
        ForeignKey("accounts.user_id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )

    # Node-service configuration reservation tracking.
    # Links pending top-ups to atomic capacity holds in node-service.
    reservation_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    reservation_status = Column(
        String(30),
        default="NotRequired",
        nullable=False,
        index=True,
    )

    wallet_bucket_id = Column(
        UUID(as_uuid=True), 
        ForeignKey("wallet_buckets.id", ondelete="SET NULL"), 
        nullable=True, 
        index=True
    )

    tx_type = Column(String(20), nullable=False)  # 'Topup', 'Deduction', 'Refund', 'Gift', 'Transfer'
    amount = Column(Float, nullable=False)        # Amount in account currency
    currency = Column(String(10), default="IRT", nullable=False)  # 'IRT' or 'USD'
    location_id = Column(UUID(as_uuid=True), nullable=True, index=True)  # Target location for top-up/transfer
    status = Column(String(20), default="Pending", nullable=False)  # 'Pending', 'Approved', 'Rejected', 'Completed'

    payment_method = Column(String(50), nullable=True)
    receipt_url = Column(Text, nullable=True)
    description = Column(String(255), nullable=True)
    admin_notes = Column(String(255), nullable=True)

    processed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    account = relationship("Account", back_populates="transactions")
    wallet_bucket = relationship("WalletBucket", back_populates="transactions")


class SystemFinancialSettings(BaseModel):
    """
    Global platform financial settings, regional deposit rules, and admin payment gateways.
    """
    __tablename__ = "system_financial_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    # Automatic gift credit granted to new signups (Toman)
    default_signup_gift_credit = Column(
        Float,
        default=20000.0,
        nullable=False,
    )

    # Purchase minimum top-up (e.g. 50,000 Toman)
    new_region_min_topup_amount = Column(
        Float,
        default=50000.0,
        nullable=False,
    )

    # Existing region / grace-period purchase minimum (e.g. 10,000 Toman)
    existing_region_min_topup_amount = Column(
        Float,
        default=10000.0,
        nullable=False,
    )

    # Number of days after regional balance reaches zero before
    # the configuration is permanently released.
    region_grace_period_days = Column(
        Integer,
        default=3,
        nullable=False,
    )

    # Minimum duration (in days) a client must wait between refund requests
    refund_cooldown_days = Column(
        Integer,
        default=30,
        nullable=False,
    )

    # How long a first-purchase configuration reservation hold remains valid.
    config_reservation_ttl_minutes = Column(
        Integer,
        default=30,
        nullable=False,
    )
    # 1. Early Cutoff Safety Threshold in Toman (e.g. 50 Toman). Disables VPN when balance <= threshold.
    safety_cutoff_threshold = Column(Float, default=50.0, nullable=False)

    # 2. Maximum negative debt limit in Toman. NULL/None = Infinite negative (no limit).
    max_negative_balance = Column(Float, nullable=True)

    # USD settings and thresholds
    default_signup_gift_credit_usd = Column(Float, default=0.50, nullable=False)
    new_region_min_topup_amount_usd = Column(Float, default=2.00, nullable=False)
    existing_region_min_topup_amount_usd = Column(Float, default=1.00, nullable=False)
    safety_cutoff_threshold_usd = Column(Float, default=0.01, nullable=False)
    max_negative_balance_usd = Column(Float, nullable=True)
    usd_to_irt_exchange_rate = Column(Float, default=90000.0, nullable=False)
    
    # Central platform accounts for customer payments
    admin_bank_cards = Column(JSONB, default=list, nullable=False)
    admin_crypto_wallets = Column(JSONB, default=list, nullable=False)
    admin_payment_instructions = Column(Text, nullable=True)


class LocationCapacity(BaseModel):
    """
    Local replicated read-model of location capacity in billing_db.
    Updated asynchronously via RabbitMQ from node-service (Event-Carried State Transfer).
    Eliminates synchronous HTTP coupling between microservices.
    """
    __tablename__ = "location_capacities"

    location_id = Column(UUID(as_uuid=True), primary_key=True, index=True)
    location_name = Column(String(100), nullable=True)
    total_capacity = Column(Integer, default=0, nullable=False)
    used_capacity = Column(Integer, default=0, nullable=False)
    is_full = Column(Boolean, default=False, nullable=False)