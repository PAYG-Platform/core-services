# backend/billing-service/app/api/topups.py
import os
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from app.database import get_db
from app.models import (
    Account,
    WalletBucket,
    Transaction,
    LocationCapacity,
    SystemFinancialSettings,
)
from app.schemas import (
    BalanceAdjustmentRequest, 
    TransactionResponse, 
    TransactionCreate, 
    TransactionAction, 
    FiatTopupCreate,
    AdminGiftAdjustRequest,
    AdminDirectTopupRequest,
    TopupNoteUpdate,
)
from app.core.dependencies import get_current_user_id
from vpn_shared.jwt_auth import get_current_user_payload
from app.utils import save_base64_receipt_to_disk
from app.events.publisher import (
    publish_wallet_funded,
    publish_wallet_empty,
    publish_wallet_low,
    publish_payment_processed,
    publish_payment_submitted,
    publish_config_reservation_requested,
    publish_config_reservation_release_requested,
    publish_config_reservation_consume_requested,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/topups", tags=["Client Top-ups & Balance Adjustments"])


# =========================================================================
# HELPER FUNCTIONS (STRICTLY LOCAL TO BILLING_DB - ZERO HTTP CALLS)
# =========================================================================

def calculate_user_credit_state(user_id: uuid.UUID, db: Session) -> Dict[str, float]:
    """
    Calculates separated financial balances:
    - gift_balance: Promotional credit (always >= 0)
    - paid_balance: Real fiat/crypto balance (can be negative)
    - usable_credit: Credit available to route VPN traffic (gift + max(0, paid))
    """
    gift_bal = db.query(func.sum(WalletBucket.balance)).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type == "GIFT",
        WalletBucket.is_active == True
    ).scalar() or 0.0

    paid_bal = db.query(func.sum(WalletBucket.balance)).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type != "GIFT",
        WalletBucket.is_active == True
    ).scalar() or 0.0

    return {
        "gift_balance": max(0.0, float(gift_bal)),
        "paid_balance": float(paid_bal),
        "usable_credit": max(0.0, float(gift_bal)) + max(0.0, float(paid_bal))
    }


def get_user_min_topup_threshold(
    user_id: uuid.UUID,
    settings: SystemFinancialSettings,
    db: Session,
) -> float:
    """
    CALCULATES THE USER-SPECIFIC MINIMUM TOP-UP REQUIREMENT:
    1. Gifting does NOT count as a top-up. A user who signed up and has never topped up
       MUST pay >= Purchase Minimum Top-Up (New User) (e.g. 50,000 Toman or $2.00 USD).
    2. When the user's wallet becomes negative or zero:
       - If within grace period (empty_since >= cutoff): allows Existing/Grace minimum (10,000 T or $1.00 USD).
       - If grace period has passed (empty_since < cutoff or configs released): MUST pay >= Purchase Minimum Top-Up (New User).
    """
    acc = db.query(Account).filter(Account.user_id == user_id).first()
    is_usd = bool(acc and str(acc.billing_currency or "").upper() == "USD")

    if is_usd:
        new_min = float(getattr(settings, "new_region_min_topup_amount_usd", 2.0) or 2.0)
        existing_min = float(getattr(settings, "existing_region_min_topup_amount_usd", 1.0) or 1.0)
    else:
        new_min = float(settings.new_region_min_topup_amount or 50000.0)
        existing_min = float(settings.existing_region_min_topup_amount or 10000.0)

    # RULE 1: Check if user has EVER had an approved top-up transaction (Gift transactions do NOT count)
    has_prior_topup = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.tx_type == "Topup",
        Transaction.status.in_(["Approved", "Completed"])
    ).first() is not None

    if not has_prior_topup:
        # User is brand new (only has gift credit or 0 balance): force Purchase Minimum Top-Up (New User)
        return new_min

    # RULE 2: User has topped up before. Check if they currently hold positive PAID balance (excluding GIFT)
    has_positive_paid = db.query(WalletBucket).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type != "GIFT",
        WalletBucket.is_active == True,
        WalletBucket.balance > 0
    ).first() is not None

    if has_positive_paid:
        return existing_min

    # RULE 3: User's paid balance is zero or negative debt. Check grace period expiration:
    grace_days = int(settings.region_grace_period_days or 3)
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)

    empty_buckets = db.query(WalletBucket).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type != "GIFT",
        WalletBucket.is_active == True
    ).all()

    # If any empty bucket passed the grace period, force New User Minimum
    if empty_buckets:
        for b in empty_buckets:
            if b.empty_since:
                b_empty = b.empty_since.replace(tzinfo=timezone.utc) if b.empty_since.tzinfo is None else b.empty_since
                if b_empty < cutoff:
                    return new_min
            else:
                # Balance is zero or negative with no timestamp -> grace expired
                return new_min
        # All empty buckets are still within grace period
        return existing_min

    return new_min


def get_min_topup_for_location(
    user_id: uuid.UUID,
    location_id: Optional[uuid.UUID],
    settings: SystemFinancialSettings,
    db: Session,
) -> float:
    """Backward-compatible alias so existing bucket loops resolve without NameError."""
    return get_user_min_topup_threshold(user_id, settings, db)


def get_location_name(location_id: uuid.UUID, db: Session) -> Optional[str]:
    loc = db.query(LocationCapacity).filter(LocationCapacity.location_id == location_id).first()
    return loc.location_name if (loc and loc.location_name) else None


# =========================================================================
# CLIENT TOP-UP & PAYMENT SUBMISSION ENDPOINTS
# =========================================================================

@router.post("/fiat", response_model=TransactionResponse)
async def submit_fiat_topup(
    tx_in: FiatTopupCreate,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """
    Submits a wallet top-up (Shetab bank transfer receipt or Crypto deposit).
    Top-up funds the client's global wallet with no location selection required.
    """
    settings = db.query(SystemFinancialSettings).first()
    if not settings:
        settings = SystemFinancialSettings()
        db.add(settings)
        db.flush()

    client_acc = db.query(Account).filter(Account.user_id == user_id).first()
    if not client_acc:
        client_acc = Account(user_id=user_id, role="Client", billing_currency="IRT")
        db.add(client_acc)
        db.flush()

    user_currency = str(getattr(client_acc, "billing_currency", "IRT") or "IRT").upper()

    # ANTI-ARBITRAGE GUARD: International USD users are strictly barred from submitting Shetab card payments
    if user_currency == "USD":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Fiat Shetab bank card deposits are only available for Toman accounts. Please deposit using Crypto (USDT/TON).",
        )

    # DYNAMIC THRESHOLD: 10,000 T for existing/grace users vs 50,000 T for new/expired users
    min_amount = get_user_min_topup_threshold(user_id, settings, db)

    if tx_in.amount < min_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Minimum top-up amount is {min_amount:,.0f} Toman.",
        )

    saved_receipt_path = save_base64_receipt_to_disk(tx_in.receipt_url)
    ref_id = tx_in.reference_id or f"TOP-{uuid.uuid4().hex[:8].upper()}"

    tx = Transaction(
        reference_id=ref_id,
        user_id=user_id,
        location_id=tx_in.location_id,
        tx_type="Topup",
        amount=tx_in.amount,
        currency="IRT",
        status="Pending",
        reservation_status="NotRequired",
        payment_method=tx_in.payment_method or "Bank Card Transfer",
        receipt_url=saved_receipt_path,
        description=tx_in.description or "Wallet top-up request",
    )

    db.add(tx)
    db.commit()
    db.refresh(tx)

    # Notify Admins of incoming top-up submission
    await publish_payment_submitted(
        user_id=str(user_id),
        tx_type="Topup",
        amount=tx.amount,
        reference_id=tx.reference_id,
        location_name=None
    )

    return tx


@router.post("/crypto", response_model=TransactionResponse)
async def submit_crypto_topup(
    tx_in: FiatTopupCreate, 
    db: Session = Depends(get_db), 
    user_id: uuid.UUID = Depends(get_current_user_id)
):
    """
    Client submits a Crypto (USDT/TON) top-up receipt.
    - If account is USD: credited 1:1 in USD.
    - If account is IRT: converted to Toman using usd_to_irt_exchange_rate.
    """
    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    client_acc = db.query(Account).filter(Account.user_id == user_id).first()
    if not client_acc:
        client_acc = Account(user_id=user_id, role="Client", billing_currency="IRT")
        db.add(client_acc)
        db.flush()

    user_currency = str(getattr(client_acc, "billing_currency", "IRT") or "IRT").upper()
    min_amount = get_user_min_topup_threshold(user_id, settings, db)

    saved_receipt_path = save_base64_receipt_to_disk(tx_in.receipt_url)
    ref_id = tx_in.reference_id or f"CRYPTO-{uuid.uuid4().hex[:8].upper()}"

    if user_currency == "USD":
        if tx_in.amount < min_amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Minimum crypto deposit is ${min_amount:.2f} USD.",
            )
        final_amount = tx_in.amount
        desc = tx_in.description or f"Crypto deposit (${final_amount:.2f} USD)"
    else:
        # IRT account: convert USDT deposit amount to Toman
        rate = float(getattr(settings, "usd_to_irt_exchange_rate", 90000.0) or 90000.0)
        # If user entered dollar value <= 1000, convert to Toman; otherwise assume user entered Toman equivalent
        if tx_in.amount < 1000:
            final_amount = round(tx_in.amount * rate)
            desc = tx_in.description or f"Crypto deposit: {tx_in.amount} USDT converted @ {rate:,.0f} Toman"
        else:
            final_amount = tx_in.amount
            desc = tx_in.description or f"Crypto deposit in Toman"

        if final_amount < min_amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Minimum deposit is {min_amount:,.0f} Toman.",
            )

    tx = Transaction(
        reference_id=ref_id,
        user_id=user_id,
        location_id=tx_in.location_id,
        tx_type="Topup",
        amount=final_amount,
        currency=user_currency,
        status="Pending",
        reservation_status="NotRequired",
        payment_method=tx_in.payment_method or "USDT TRC20",
        receipt_url=saved_receipt_path,
        description=desc,
    )

    db.add(tx)
    db.commit()
    db.refresh(tx)

    await publish_payment_submitted(
        user_id=str(user_id),
        tx_type="Topup",
        amount=tx.amount,
        reference_id=tx.reference_id,
        location_name=None
    )

    return tx


# =========================================================================
# CLIENT QUERIES (MY TOPUPS & BALANCES)
# =========================================================================

@router.get("/my", response_model=List[TransactionResponse])
def get_my_topup_requests(
    status: Optional[str] = Query(None, description="Pending, Approved, Rejected, Completed"),
    min_amount: Optional[float] = Query(None),
    max_amount: Optional[float] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id)
):
    query = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.tx_type == "Topup"
    )

    if status and status.lower() != "all":
        query = query.filter(Transaction.status.ilike(status.strip()))
    if min_amount is not None and min_amount > 0:
        query = query.filter(Transaction.amount >= min_amount)
    if max_amount is not None and max_amount > 0:
        query = query.filter(Transaction.amount <= max_amount)

    if start_date:
        try:
            st_dt = datetime.fromisoformat(start_date)
            query = query.filter(Transaction.created_at >= st_dt)
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date)
            query = query.filter(Transaction.created_at <= end_dt)
        except ValueError:
            pass

    if search and search.strip():
        s_term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Transaction.reference_id.ilike(s_term),
                Transaction.description.ilike(s_term),
                Transaction.payment_method.ilike(s_term)
            )
        )

    return query.order_by(Transaction.created_at.desc()).all()


@router.get("/wallet-movements", response_model=List[TransactionResponse])
def get_my_wallet_movements(
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id)
):
    return db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.status.in_(["Completed", "Approved"])
    ).order_by(Transaction.created_at.desc()).all()


@router.get("/balance")
def get_my_wallet_balance(
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id)
):
    buckets = db.query(WalletBucket).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.is_active == True
    ).all()

    def bucket_sort_key(b: WalletBucket):
        type_order = 0 if b.bucket_type == "FIAT" else 1 if b.bucket_type == "CRYPTO" else 2
        created_dt = b.created_at or datetime.min.replace(tzinfo=timezone.utc)
        return (type_order, created_dt)

    sorted_buckets = sorted(buckets, key=bucket_sort_key)

    credit_state = calculate_user_credit_state(user_id, db)
    paid_bal = credit_state["paid_balance"]
    gift_bal = credit_state["gift_balance"]
    usable_credit = credit_state["usable_credit"]

    acc = db.query(Account).filter(Account.user_id == user_id).first()
    user_currency = str(getattr(acc, "billing_currency", "IRT") or "IRT").upper()

    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    
    if user_currency == "USD":
        safety_cutoff = float(getattr(settings, "safety_cutoff_threshold_usd", 0.01) or 0.01)
        max_neg = getattr(settings, "max_negative_balance_usd", None)
    else:
        safety_cutoff = float(settings.safety_cutoff_threshold or 50.0)
        max_neg = settings.max_negative_balance

    display_paid_balance = float(paid_bal)
    if max_neg is not None and max_neg >= 0:
        debt_floor = -float(max_neg)
        if display_paid_balance < debt_floor:
            display_paid_balance = debt_floor

    # If user has debt (paid_balance < 0), display debt directly so adding gifts does not hide or wipe out debt
    if display_paid_balance < 0:
        display_total_balance = display_paid_balance
    else:
        display_total_balance = display_paid_balance + gift_bal

    frozen_refund_amount = db.query(func.sum(Transaction.amount)).filter(
        Transaction.user_id == user_id,
        Transaction.tx_type == "Refund",
        Transaction.status == "Pending"
    ).scalar() or 0.0

    result_buckets = []
    for idx, b in enumerate(sorted_buckets, 1):
        min_req = get_min_topup_for_location(user_id, b.location_id, settings, db)

        resolved_loc_name = b.location_name
        if not resolved_loc_name and b.location_id:
            loc_cap = db.query(LocationCapacity).filter(LocationCapacity.location_id == b.location_id).first()
            if loc_cap and loc_cap.location_name:
                resolved_loc_name = loc_cap.location_name

        if not resolved_loc_name:
            if b.bucket_type == "GIFT":
                resolved_loc_name = "Gift Credit (All Regions)"
            elif b.bucket_type == "CRYPTO":
                resolved_loc_name = "Platform Crypto (Global)"
            elif b.location_id is None:
                resolved_loc_name = "Unallocated Store Credit"
            else:
                resolved_loc_name = "Primary Region"

        result_buckets.append({
            "id": str(b.id),
            "bucket_type": b.bucket_type,
            "priority": idx,
            "location_id": str(b.location_id) if b.location_id else None,
            "location_name": resolved_loc_name,
            "balance": float(b.balance),
            "empty_since": b.empty_since.isoformat() if b.empty_since else None,
            "is_active": b.is_active,
            "min_topup_required": min_req,
            "created_at": b.created_at.isoformat() if b.created_at else None
        })

    user_min_req = get_user_min_topup_threshold(user_id, settings, db)

    return {
        "total_balance": float(display_total_balance),
        "paid_balance": float(display_paid_balance),
        "gift_balance": float(gift_bal),
        "usable_credit": float(usable_credit),
        "raw_balance": float(paid_bal + gift_bal),
        "frozen_refund_amount": float(frozen_refund_amount),
        "safety_cutoff_threshold": float(safety_cutoff),
        "max_negative_balance": float(max_neg) if max_neg is not None else None,
        "user_min_topup_amount": float(user_min_req),
        "currency": user_currency,
        "buckets": result_buckets
    }


# =========================================================================
# REGION CREDITS (SIMPLIFIED PLATFORM WIDE)
# =========================================================================

@router.get("/region-credits")
def get_client_region_credits(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Returns all regional wallet buckets belonging to the client."""
    base_query = db.query(WalletBucket).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type == "FIAT",
        WalletBucket.is_active == True,
    )

    total = base_query.count()
    offset = (page - 1) * limit
    buckets = base_query.order_by(
        WalletBucket.balance.desc(),
        WalletBucket.created_at.asc()
    ).offset(offset).limit(limit).all()

    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    items = []

    for bucket in buckets:
        resolved_name = bucket.location_name
        if not resolved_name and bucket.location_id:
            loc_cap = db.query(LocationCapacity).filter(LocationCapacity.location_id == bucket.location_id).first()
            if loc_cap and loc_cap.location_name:
                resolved_name = loc_cap.location_name

        if not resolved_name:
            resolved_name = "Primary Region"

        min_req = get_min_topup_for_location(user_id, bucket.location_id, settings, db)

        items.append({
            "id": str(bucket.id),
            "bucket_type": bucket.bucket_type,
            "location_id": str(bucket.location_id) if bucket.location_id else None,
            "location_name": resolved_name,
            "balance": float(bucket.balance),
            "empty_since": bucket.empty_since.isoformat() if bucket.empty_since else None,
            "is_active": bool(bucket.is_active),
            "min_topup_required": min_req,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": max(1, (total + limit - 1) // limit),
    }


# =========================================================================
# ADMIN TOP-UP MANAGEMENT & APPROVAL WORKFLOW
# =========================================================================

@router.get("/admin/pending-counts")
def get_admin_pending_counts(
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    """Returns count of pending top-up receipts and refund requests for Admin badge/indicator."""
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    topups_count = db.query(Transaction).filter(
        Transaction.tx_type == "Topup", 
        Transaction.status == "Pending"
    ).count()

    refunds_count = db.query(Transaction).filter(
        Transaction.tx_type == "Refund", 
        Transaction.status == "Pending"
    ).count()

    return {
        "pending_topups_count": topups_count,
        "pending_refunds_count": refunds_count,
        "total_pending": topups_count + refunds_count
    }


@router.get("/admin/all", response_model=List[TransactionResponse])
def get_all_topups_admin(
    status: Optional[str] = Query(None, description="Pending, Approved, Rejected, or All"),
    search: Optional[str] = Query(None),
    min_amount: Optional[float] = Query(None),
    max_amount: Optional[float] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    """Admin retrieves all top-up transactions across all clients."""
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    query = db.query(Transaction).filter(Transaction.tx_type == "Topup")

    if status and status.lower() != "all":
        query = query.filter(Transaction.status.ilike(status.strip()))
    if min_amount is not None and min_amount > 0:
        query = query.filter(Transaction.amount >= min_amount)
    if max_amount is not None and max_amount > 0:
        query = query.filter(Transaction.amount <= max_amount)

    if start_date:
        try:
            st_dt = datetime.fromisoformat(start_date)
            query = query.filter(Transaction.created_at >= st_dt)
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date)
            query = query.filter(Transaction.created_at <= end_dt)
        except ValueError:
            pass

    if search and search.strip():
        s_term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Transaction.reference_id.ilike(s_term),
                Transaction.description.ilike(s_term),
                Transaction.payment_method.ilike(s_term)
            )
        )

    results = query.order_by(Transaction.created_at.desc()).all()

    client_ids = list({t.user_id for t in results if t.user_id})
    if client_ids:
        accounts = db.query(Account.user_id, Account.username).filter(Account.user_id.in_(client_ids)).all()
        user_map = {acc.user_id: acc.username for acc in accounts if acc.username}
        for t in results:
            if t.user_id in user_map:
                setattr(t, "client_username", user_map[t.user_id])

    return results


@router.get("/pending", response_model=List[TransactionResponse])
def get_pending_topups(
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    """Admin views all pending top-up verification slips."""
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    return db.query(Transaction).filter(
        Transaction.tx_type == "Topup", 
        Transaction.status == "Pending"
    ).order_by(Transaction.created_at.desc()).all()


@router.post("/{tx_id}/process")
async def process_topup(
    tx_id: uuid.UUID, 
    action_in: TransactionAction, 
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Admin privileges required to process top-up requests."
        )

    tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.tx_type == "Topup").first()
    if not tx or tx.status != "Pending":
        raise HTTPException(status_code=404, detail="Pending top-up transaction not found.")

    action_raw = action_in.action.strip().capitalize()
    credited_amount = action_in.amount_credited if action_in.amount_credited is not None else tx.amount
    if action_raw in ["Approve", "Approveunallocated"] and credited_amount <= 0:
        raise HTTPException(status_code=400, detail="Credited amount must be greater than zero.")

    # 1. State before processing
    state_before = calculate_user_credit_state(tx.user_id, db)
    usable_before = state_before["usable_credit"]

    if action_raw in ["Approve", "Approveunallocated"]:
        tx.status = "Approved"
        tx.amount = credited_amount

        bucket = db.query(WalletBucket).filter(
            WalletBucket.user_id == tx.user_id,
            WalletBucket.bucket_type == "FIAT"
        ).first()

        if not bucket:
            bucket = WalletBucket(
                user_id=tx.user_id,
                location_name="Primary Region",
                bucket_type="FIAT",
                balance=credited_amount,
                is_active=True
            )
            db.add(bucket)
            db.flush()
        else:
            bucket.balance += credited_amount
            bucket.is_active = True
            bucket.empty_since = None

        tx.wallet_bucket_id = bucket.id
        tx.processed_at = datetime.now(timezone.utc)

        if tx.reservation_status == "Reserved" and tx.reservation_id:
            await publish_config_reservation_consume_requested(
                reservation_id=str(tx.reservation_id),
                user_id=str(tx.user_id),
                location_id=str(tx.location_id),
                topup_transaction_id=str(tx.id)
            )

    elif action_raw == "Reject":
        tx.status = "Rejected"
        tx.processed_at = datetime.now(timezone.utc)
        if tx.reservation_status == "Reserved" and tx.reservation_id:
            await publish_config_reservation_release_requested(
                reservation_id=str(tx.reservation_id),
                user_id=str(tx.user_id),
                location_id=str(tx.location_id),
                topup_transaction_id=str(tx.id)
            )
    else:
        raise HTTPException(status_code=400, detail="Invalid action.")

    tx.admin_notes = action_in.notes.strip() if action_in.notes and action_in.notes.strip() else None
    db.commit()

    # 2. State after processing
    state_after = calculate_user_credit_state(tx.user_id, db)
    usable_after = state_after["usable_credit"]

    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    safety_cutoff = float(settings.safety_cutoff_threshold or 0.0)

    # Immediately enable configs if usable credit rises above safety cutoff
    if action_raw in ["Approve", "Approveunallocated"]:
        if usable_before <= safety_cutoff and usable_after > safety_cutoff:
            await publish_wallet_funded(str(tx.user_id), str(tx.location_id) if tx.location_id else None)

    await publish_payment_processed(
        user_id=str(tx.user_id),
        tx_type="Topup",
        status=tx.status,
        amount=tx.amount,
        reference_id=tx.reference_id,
        admin_notes=tx.admin_notes,
        currency=getattr(tx, "currency", "IRT") or "IRT"
    )

    return {"message": f"Top-up request {tx.status.lower()} successfully."}


@router.patch("/{tx_id}/note", response_model=TransactionResponse)
def update_topup_note(
    tx_id: uuid.UUID,
    note_in: TopupNoteUpdate,
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    """Admin updates audit/processing notes on a top-up transaction."""
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.tx_type == "Topup").first()
    if not tx:
        raise HTTPException(status_code=404, detail="Top-up transaction not found.")

    tx.admin_notes = note_in.notes.strip()
    db.commit()
    db.refresh(tx)
    return tx


# ==========================================
# ADMIN BALANCE & GIFT ADJUSTMENTS
# ==========================================

@router.post("/admin-direct-topup", response_model=TransactionResponse)
async def admin_direct_topup(
    req_in: AdminDirectTopupRequest,
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    """
    Admin directly credits a top-up transaction for a client without a prior client request.
    Enforces the user's specific minimum top-up requirement based on currency and grace period.
    """
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    client_acc = db.query(Account).filter(Account.user_id == req_in.client_id).first()
    client_curr = str(getattr(client_acc, "billing_currency", "IRT") or "IRT").upper() if client_acc else "IRT"

    # Enforce exact minimum top-up requirement (New User vs Existing/Grace)
    min_amount = get_user_min_topup_threshold(req_in.client_id, settings, db)

    if req_in.amount < min_amount:
        unit_str = f"${min_amount:.2f} USD" if client_curr == "USD" else f"{min_amount:,.0f} Toman"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Minimum required top-up amount for this user is {unit_str}."
        )

    # 1. State before topup
    state_before = calculate_user_credit_state(req_in.client_id, db)
    usable_before = state_before["usable_credit"]

    client_acc = db.query(Account).filter(Account.user_id == req_in.client_id).first()
    if not client_acc:
        client_acc = Account(user_id=req_in.client_id, role="Client")
        db.add(client_acc)
        db.flush()

    bucket = db.query(WalletBucket).filter(
        WalletBucket.user_id == req_in.client_id,
        WalletBucket.bucket_type == "FIAT"
    ).first()

    if not bucket:
        bucket = WalletBucket(
            user_id=req_in.client_id,
            location_name="Primary Region",
            bucket_type="FIAT",
            balance=req_in.amount,
            is_active=True
        )
        db.add(bucket)
        db.flush()
    else:
        bucket.balance += req_in.amount
        bucket.is_active = True
        bucket.empty_since = None

    ref_id = f"TOP-{uuid.uuid4().hex[:8].upper()}"
    client_curr = str(getattr(client_acc, "billing_currency", "IRT") or "IRT").upper()
    tx = Transaction(
        reference_id=ref_id,
        user_id=req_in.client_id,
        wallet_bucket_id=bucket.id,
        tx_type="Topup",
        amount=req_in.amount,
        currency=client_curr,
        status="Approved",
        reservation_status="NotRequired",
        payment_method=req_in.payment_method or "Direct Admin Deposit",
        description=req_in.description or "Direct top-up by administrator",
        admin_notes="Directly issued by Administrator",
        processed_at=datetime.now(timezone.utc)
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # 2. State after topup
    state_after = calculate_user_credit_state(req_in.client_id, db)
    usable_after = state_after["usable_credit"]
    safety_cutoff = float(settings.safety_cutoff_threshold or 0.0)

    if usable_before <= safety_cutoff and usable_after > safety_cutoff:
        await publish_wallet_funded(str(req_in.client_id))

    await publish_payment_processed(
        user_id=str(req_in.client_id),
        tx_type="Topup",
        status="Approved",
        amount=req_in.amount,
        reference_id=tx.reference_id,
        admin_notes=tx.admin_notes,
        currency=tx.currency
    )

    return tx


@router.post("/adjust")
async def admin_adjust_client_balance(
    adj_in: BalanceAdjustmentRequest,
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    """Admin directly credits or debits a client's regional wallet balance."""
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    action_clean = adj_in.action.strip().capitalize()
    if action_clean not in ["Credit", "Debit"]:
        raise HTTPException(status_code=400, detail="Action must be 'Credit' or 'Debit'.")

    if adj_in.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")

    # 1. State before adjustment
    state_before = calculate_user_credit_state(adj_in.client_id, db)
    usable_before = state_before["usable_credit"]

    client_account = db.query(Account).filter(Account.user_id == adj_in.client_id).first()
    if not client_account:
        client_account = Account(user_id=adj_in.client_id, role="Client", billing_currency="IRT")
        db.add(client_account)
        db.flush()

    # FIX: Ensure client_curr is universally defined in outer scope
    client_curr = str(getattr(client_account, "billing_currency", "IRT") or "IRT").upper()

    bucket = db.query(WalletBucket).filter(
        WalletBucket.user_id == adj_in.client_id,
        WalletBucket.bucket_type == "FIAT",
        WalletBucket.is_active == True
    ).first()

    if action_clean == "Credit":
        if not bucket:
            bucket = WalletBucket(
                user_id=adj_in.client_id,
                bucket_type="FIAT",
                location_name="Primary Region",
                balance=adj_in.amount,
                currency=client_curr,
                is_active=True
            )
            db.add(bucket)
            db.flush()
        else:
            bucket.balance += adj_in.amount
            bucket.empty_since = None
    elif action_clean == "Debit":
        available_in_bucket = bucket.balance if bucket else 0.0
        settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
        max_neg_limit = settings.max_negative_balance if (settings and settings.max_negative_balance is not None) else None

        # Validate against database floor: cannot debit past -max_negative_balance
        if max_neg_limit is not None and max_neg_limit >= 0:
            debt_floor = -float(max_neg_limit)
            if (available_in_bucket - adj_in.amount) < debt_floor:
                allowed_debit = max(0.0, available_in_bucket - debt_floor)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot debit {adj_in.amount:,.0f} Toman. System negative credit floor is -{max_neg_limit:,.0f} Toman (Maximum debit allowed: {allowed_debit:,.0f} Toman)."
                )

        bucket.balance -= adj_in.amount
        if bucket.balance <= 0 and not bucket.empty_since:
            bucket.empty_since = datetime.now(timezone.utc)

    ref_id = f"ADJ-{uuid.uuid4().hex[:8].upper()}"
    tx = Transaction(
        reference_id=ref_id,
        user_id=adj_in.client_id,
        wallet_bucket_id=bucket.id if bucket else None,
        tx_type="Topup" if action_clean == "Credit" else "Deduction",
        amount=adj_in.amount if action_clean == "Credit" else -adj_in.amount,
        currency=client_curr,
        status="Completed",
        payment_method="Admin Adjustment",
        description=adj_in.reason or f"Admin manual {action_clean.lower()} adjustment",
        processed_at=datetime.now(timezone.utc)
    )
    db.add(tx)
    db.commit()

    # 2. State after adjustment
    state_after = calculate_user_credit_state(adj_in.client_id, db)
    usable_after = state_after["usable_credit"]

    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    safety_cutoff = float(
        getattr(settings, "safety_cutoff_threshold_usd", 0.01) if client_curr == "USD"
        else getattr(settings, "safety_cutoff_threshold", 50.0) or 0.0
    )

    if usable_before <= safety_cutoff and usable_after > safety_cutoff:
        await publish_wallet_funded(str(adj_in.client_id))
    elif usable_before > safety_cutoff and usable_after <= safety_cutoff:
        await publish_wallet_empty(str(adj_in.client_id), currency=client_curr)
    elif usable_after > safety_cutoff:
        # FIX: threshold is safely passed
        await publish_wallet_low(str(adj_in.client_id), balance=usable_after, threshold=safety_cutoff, currency=client_curr)

    # Publish notification event for user (Web App inbox, SSE & Telegram)
    await publish_payment_processed(
        user_id=str(adj_in.client_id),
        tx_type="Adjustment",
        status="Completed",
        amount=adj_in.amount,
        reference_id=ref_id,
        admin_notes=adj_in.reason or "",
        currency=client_curr,
        action=action_clean
    )

    return {
        "message": f"Successfully {action_clean.lower()}ed {adj_in.amount:,.0f} Toman to client wallet.",
        "bucket_balance": bucket.balance if bucket else 0.0,
        "paid_balance": state_after["paid_balance"],
        "usable_credit": usable_after,
        "reference_id": ref_id
    }


@router.post("/admin-gift-adjust")
async def admin_adjust_gift_credit(
    adj_in: AdminGiftAdjustRequest,
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    action_clean = adj_in.action.strip().capitalize()
    if action_clean not in ["Credit", "Debit"]:
        raise HTTPException(status_code=400, detail="Action must be 'Credit' or 'Debit'.")

    # FIX: Retrieve client account & currency upfront
    client_acc = db.query(Account).filter(Account.user_id == adj_in.client_id).first()
    client_curr = str(getattr(client_acc, "billing_currency", "IRT") or "IRT").upper()

    # 1. State before adjustment
    state_before = calculate_user_credit_state(adj_in.client_id, db)
    usable_before = state_before["usable_credit"]

    bucket = db.query(WalletBucket).filter(
        WalletBucket.user_id == adj_in.client_id,
        WalletBucket.bucket_type == "GIFT"
    ).first()

    if action_clean == "Credit":
        if not bucket:
            bucket = WalletBucket(
                user_id=adj_in.client_id,
                bucket_type="GIFT",
                location_name="Gift Credit (All Regions)",
                balance=adj_in.amount,
                currency=client_curr,
                is_active=True
            )
            db.add(bucket)
            db.flush()
        else:
            bucket.balance += adj_in.amount
            bucket.is_active = True
            bucket.empty_since = None
    elif action_clean == "Debit":
        available_gift = bucket.balance if bucket else 0.0
        if not bucket or available_gift < adj_in.amount:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot deduct {adj_in.amount:,.0f} Toman. Client only has {available_gift:,.0f} Toman gift credit."
            )
        bucket.balance -= adj_in.amount
        if bucket.balance <= 0 and not bucket.empty_since:
            bucket.empty_since = datetime.now(timezone.utc)

    ref_id = f"GIFT-{uuid.uuid4().hex[:8].upper()}"
    tx = Transaction(
        reference_id=ref_id,
        user_id=adj_in.client_id,
        wallet_bucket_id=bucket.id if bucket else None,
        tx_type="Gift",
        amount=adj_in.amount if action_clean == "Credit" else -adj_in.amount,
        currency=client_curr,
        status="Completed",
        payment_method="System Gift Credit",
        description=adj_in.reason or f"Admin gift {action_clean.lower()} of {adj_in.amount:,.0f} Toman",
        processed_at=datetime.now(timezone.utc)
    )
    db.add(tx)
    db.commit()

    # 2. State after adjustment (Separated Usable Credit)
    state_after = calculate_user_credit_state(adj_in.client_id, db)
    usable_after = state_after["usable_credit"]

    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    safety_cutoff = float(
        getattr(settings, "safety_cutoff_threshold_usd", 0.01) if client_curr == "USD"
        else getattr(settings, "safety_cutoff_threshold", 50.0) or 0.0
    )

    # IMMEDIATELY ENABLE CONFIGS IF USABLE CREDIT RISES ABOVE CUTOFF
    if usable_before <= safety_cutoff and usable_after > safety_cutoff:
        logger.info(f"[Topup] Gift added: User {adj_in.client_id} usable credit ({usable_after}) > cutoff ({safety_cutoff}). Emitting WALLET_FUNDED.")
        await publish_wallet_funded(str(adj_in.client_id))
    elif usable_before > safety_cutoff and usable_after <= safety_cutoff:
        logger.info(f"[Topup] Gift debited: User {adj_in.client_id} usable credit <= cutoff. Emitting WALLET_EMPTY.")
        await publish_wallet_empty(str(adj_in.client_id), currency=client_curr)
    elif usable_after > safety_cutoff:
        # FIX: threshold is safely passed
        await publish_wallet_low(str(adj_in.client_id), balance=usable_after, threshold=safety_cutoff, currency=client_curr)

    # Publish notification event for user (Web App inbox, SSE & Telegram)
    await publish_payment_processed(
        user_id=str(adj_in.client_id),
        tx_type="Gift",
        status="Completed",
        amount=adj_in.amount,
        reference_id=ref_id,
        admin_notes=adj_in.reason or "",
        currency=client_curr,
        action=action_clean
    )

    return {
        "message": f"Successfully {action_clean.lower()}ed {adj_in.amount:,.0f} Toman gift credit. Configs enabled immediately.",
        "gift_balance": bucket.balance if bucket else 0.0,
        "paid_balance": state_after["paid_balance"],
        "usable_credit": usable_after,
        "reference_id": ref_id
    }


@router.get("/admin/users-balances")
def get_admin_users_balances(
    db: Session = Depends(get_db),
    user_payload: Dict[str, Any] = Depends(get_current_user_payload)
):
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    buckets = db.query(WalletBucket).filter(WalletBucket.is_active == True).all()
    user_map: Dict[str, Dict[str, float]] = {}

    pending_refunds = db.query(
        Transaction.user_id,
        func.sum(Transaction.amount)
    ).filter(
        Transaction.tx_type == "Refund",
        Transaction.status == "Pending"
    ).group_by(Transaction.user_id).all()

    frozen_map = {str(uid): float(amt) for uid, amt in pending_refunds if uid}

    # Fetch used credit (Deductions) per user
    consumed_credits = db.query(
        Transaction.user_id,
        func.sum(Transaction.amount)
    ).filter(
        Transaction.tx_type == "Deduction",
        Transaction.status == "Completed"
    ).group_by(Transaction.user_id).all()
    
    used_map = {str(uid): float(amt) for uid, amt in consumed_credits if uid}

    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    max_neg = settings.max_negative_balance

    for b in buckets:
        uid = str(b.user_id)
        if uid not in user_map:
            user_map[uid] = {
                "paid_balance": 0.0, 
                "gift_balance": 0.0, 
                "total_balance": 0.0,
                "frozen_amount": frozen_map.get(uid, 0.0),
                "used_credit": used_map.get(uid, 0.0),
                "min_topup_amount": get_user_min_topup_threshold(b.user_id, settings, db)
            }

        if b.bucket_type == "GIFT":
            user_map[uid]["gift_balance"] += float(b.balance)
        else:
            user_map[uid]["paid_balance"] += float(b.balance)

        user_map[uid]["total_balance"] += float(b.balance)

    all_uids = set(frozen_map.keys()).union(set(used_map.keys()))
    for uid in all_uids:
        if uid not in user_map:
            try:
                user_uuid_val = uuid.UUID(uid)
                computed_min = get_user_min_topup_threshold(user_uuid_val, settings, db)
            except Exception:
                computed_min = 50000.0
            user_map[uid] = {
                "paid_balance": 0.0, 
                "gift_balance": 0.0, 
                "total_balance": 0.0,
                "frozen_amount": frozen_map.get(uid, 0.0),
                "used_credit": used_map.get(uid, 0.0),
                "min_topup_amount": computed_min
            }

    # If max_neg is set, clamp paid_balance and total_balance floor for admin display
    if max_neg is not None and max_neg >= 0:
        debt_floor = -float(max_neg)
        for uid, data in user_map.items():
            if data["paid_balance"] < debt_floor:
                data["paid_balance"] = debt_floor
            if data["total_balance"] < debt_floor:
                data["total_balance"] = debt_floor

    return user_map


@router.get("/internal/balances")
def get_internal_user_balances(db: Session = Depends(get_db)):
    """
    Internal endpoint: returns user balances along with the safety cutoff threshold
    and negative balance floor for Node Service.
    """
    settings = db.query(SystemFinancialSettings).first()
    safety_cutoff = float(settings.safety_cutoff_threshold or 0.0) if (settings and settings.safety_cutoff_threshold is not None) else 0.0
    max_neg = settings.max_negative_balance if (settings and settings.max_negative_balance is not None) else None

    buckets = db.query(WalletBucket).filter(WalletBucket.is_active == True).all()
    user_balances: Dict[str, float] = {}
    for b in buckets:
        uid = str(b.user_id)
        user_balances[uid] = user_balances.get(uid, 0.0) + float(b.balance)

    # Enforce negative debt floor across internal balance map if configured
    if max_neg is not None and max_neg >= 0:
        debt_floor = -float(max_neg)
        for uid in user_balances:
            if user_balances[uid] < debt_floor:
                user_balances[uid] = debt_floor
        
    return {
        "balances": user_balances,
        "safety_cutoff_threshold": safety_cutoff,
        "max_negative_balance": max_neg
    }