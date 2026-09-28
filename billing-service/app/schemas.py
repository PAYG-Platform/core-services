# backend/billing-service/app/schemas.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ==========================================
# PAYMENT ACCOUNTS & CARDS SCHEMAS
# ==========================================

class BankCardItem(BaseModel):
    id: str
    card_number: str
    cardholder_name: str
    bank_name: str
    memo: Optional[str] = None


class CryptoWalletItem(BaseModel):
    id: str
    network: str  # 'TRON (TRC20)' or 'TON'
    address: str
    label: Optional[str] = None
    memo: Optional[str] = None


# ==========================================
# SYSTEM FINANCIAL SETTINGS SCHEMAS
# ==========================================

class SystemFinancialSettingsResponse(BaseModel):
    id: uuid.UUID
    default_signup_gift_credit: float = 20000.0
    new_region_min_topup_amount: float = 50000.0
    existing_region_min_topup_amount: float = 10000.0
    region_grace_period_days: int = 3
    refund_cooldown_days: int = 30
    safety_cutoff_threshold: float = 50.0
    max_negative_balance: Optional[float] = None
    default_signup_gift_credit_usd: float = 0.50
    new_region_min_topup_amount_usd: float = 2.00
    existing_region_min_topup_amount_usd: float = 1.00
    safety_cutoff_threshold_usd: float = 0.01
    max_negative_balance_usd: Optional[float] = None
    usd_to_irt_exchange_rate: float = 90000.0
    admin_bank_cards: List[BankCardItem] = []
    admin_crypto_wallets: List[CryptoWalletItem] = []
    admin_payment_instructions: Optional[str] = None

    class Config:
        from_attributes = True


class SystemFinancialSettingsUpdate(BaseModel):
    default_signup_gift_credit: Optional[float] = Field(None, ge=0)
    new_region_min_topup_amount: Optional[float] = Field(None, gt=0)
    existing_region_min_topup_amount: Optional[float] = Field(None, gt=0)
    region_grace_period_days: Optional[int] = Field(None, ge=0)
    refund_cooldown_days: Optional[int] = Field(None, ge=1)
    safety_cutoff_threshold: Optional[float] = Field(None, ge=0)
    max_negative_balance: Optional[float] = None
    default_signup_gift_credit_usd: Optional[float] = Field(None, ge=0)
    new_region_min_topup_amount_usd: Optional[float] = Field(None, gt=0)
    existing_region_min_topup_amount_usd: Optional[float] = Field(None, gt=0)
    safety_cutoff_threshold_usd: Optional[float] = Field(None, ge=0)
    max_negative_balance_usd: Optional[float] = None
    usd_to_irt_exchange_rate: Optional[float] = Field(None, gt=0)
    admin_bank_cards: Optional[List[BankCardItem]] = None
    admin_crypto_wallets: Optional[List[CryptoWalletItem]] = None
    admin_payment_instructions: Optional[str] = None


# ==========================================
# WALLET BUCKET & ACCOUNT SCHEMAS
# ==========================================

class WalletBucketResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    bucket_type: str  # 'GIFT', 'FIAT', 'CRYPTO'
    location_id: Optional[uuid.UUID] = None
    location_name: Optional[str] = None
    balance: float
    currency: str = "IRT"
    is_active: bool
    empty_since: Optional[datetime] = None
    min_topup_required: float = 50000.0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AccountBalanceResponse(BaseModel):
    user_id: uuid.UUID
    role: str
    total_balance: float
    gift_balance: float
    currency: str = "IRT"
    buckets: List[WalletBucketResponse] = []

    class Config:
        from_attributes = True


class RefundBalanceResponse(BaseModel):
    available_balance: float
    can_refund: bool = True
    refund_cooldown_days: int = 30
    days_until_next_refund: int = 0

    class Config:
        from_attributes = True


# ==========================================
# TRANSACTION SCHEMAS
# ==========================================

class TransactionCreate(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in Toman")
    location_id: Optional[uuid.UUID] = Field(None, description="Target location for the payment")
    location_name: Optional[str] = Field(None, description="Name of the target region")
    payment_method: Optional[str] = Field(None, description="e.g. Bank Card Transfer, USDT TRC20")
    reference_id: Optional[str] = Field(None, description="Custom reference ID or TxHash")
    receipt_url: Optional[str] = Field(None, description="Uploaded proof receipt URL")
    description: Optional[str] = Field(None, description="Optional description/notes")


class TransactionResponse(BaseModel):
    id: uuid.UUID
    reference_id: str
    user_id: uuid.UUID
    wallet_bucket_id: Optional[uuid.UUID] = None
    location_id: Optional[uuid.UUID] = None
    reservation_id: Optional[uuid.UUID] = None
    reservation_status: Optional[str] = None
    tx_type: str  # 'Topup', 'Deduction', 'Refund', 'Gift', 'Transfer'
    amount: float
    currency: str = "IRT"
    status: str   # 'Pending', 'Approved', 'Rejected', 'Completed'
    payment_method: Optional[str] = None
    receipt_url: Optional[str] = None
    description: Optional[str] = None
    admin_notes: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None
    updated_at: datetime

    class Config:
        from_attributes = True


class TransactionAction(BaseModel):
    action: str = Field(..., description="Action: 'Approve', 'ApproveUnallocated', or 'Reject'")
    amount_credited: Optional[float] = Field(None, description="Confirmed credited amount in Toman")
    notes: Optional[str] = Field(None, description="Internal processing notes")


class TopupNoteUpdate(BaseModel):
    notes: str = Field(..., description="Audit note or memo provided by the administrator")


class FiatTopupCreate(BaseModel):
    amount: float = Field(..., gt=0)
    location_id: Optional[uuid.UUID] = None  # Region is no longer required on top-up
    location_name: Optional[str] = None
    reference_id: Optional[str] = None
    receipt_url: Optional[str] = None
    payment_method: Optional[str] = "Bank Card Transfer"
    description: Optional[str] = None


# ==========================================
# REFUND & ADJUSTMENT SCHEMAS
# ==========================================

class RefundRequestCreate(BaseModel):
    amount: float = Field(..., gt=0, description="Requested refund amount in Toman")
    destination_account: str = Field(..., description="Target Shetab Bank Card number or account")
    reason: Optional[str] = Field(None, description="Optional withdrawal reason")


class RefundProcessRequest(BaseModel):
    action: str = Field(..., description="Action: 'Approve' or 'Reject'")
    tx_hash_or_ref: Optional[str] = Field(None, description="Bank Reference / Tracking Number")
    proof_receipt_url: Optional[str] = Field(None, description="Screenshot proof URL of payout")
    notes: Optional[str] = Field(None, description="Internal processing notes / feedback")


class BalanceAdjustmentRequest(BaseModel):
    client_id: uuid.UUID = Field(..., description="Target client user UUID")
    amount: float = Field(..., gt=0, description="Amount in Toman")
    action: str = Field(..., description="'Credit' (Add Funds) or 'Debit' (Decrease Funds)")
    reason: Optional[str] = Field(None, description="Optional adjustment reason or memo")


class AdminGiftAdjustRequest(BaseModel):
    client_id: uuid.UUID
    amount: float = Field(..., gt=0, description="Amount in Toman")
    action: str = Field(..., description="'Credit' (Add Gift) or 'Debit' (Deduct Gift)")
    reason: Optional[str] = None


class AdminDirectTopupRequest(BaseModel):
    client_id: uuid.UUID = Field(..., description="Target client user UUID")
    amount: float = Field(..., gt=0, description="Amount in Toman")
    payment_method: Optional[str] = Field("Direct Admin Deposit", description="Payment channel")
    description: Optional[str] = Field(None, description="Optional description / memo")


class AdminDirectRefundRequest(BaseModel):
    client_id: uuid.UUID = Field(..., description="Target client user UUID")
    amount: float = Field(..., gt=0, description="Refund amount in Toman")
    destination_account: Optional[str] = Field("Direct Admin Refund", description="Target Shetab card or account")
    reason: Optional[str] = Field(None, description="Optional refund memo")


# ==========================================
# ANALYTICS SCHEMAS
# ==========================================

class AdminAnalyticsSummaryResponse(BaseModel):
    total_deposits: float
    total_used_value: float
    total_refunds_approved: float
    total_refunds_pending: float
    total_active_wallets: int
    total_deposits_irt: Optional[float] = 0.0
    total_deposits_usd: Optional[float] = 0.0
    total_used_value_irt: Optional[float] = 0.0
    total_used_value_usd: Optional[float] = 0.0
    total_refunds_approved_irt: Optional[float] = 0.0
    total_refunds_approved_usd: Optional[float] = 0.0
    total_refunds_pending_irt: Optional[float] = 0.0
    total_refunds_pending_usd: Optional[float] = 0.0