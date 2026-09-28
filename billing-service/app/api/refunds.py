# backend/billing-service/app/api/refunds.py
import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.sql import func
from sqlalchemy.orm import Session
from sqlalchemy import or_
import math
from datetime import datetime, timezone, timedelta
from app.models import Account, WalletBucket, Transaction, SystemFinancialSettings
from app.events.publisher import (
    publish_payment_processed,
    publish_payment_submitted,
    publish_wallet_empty,
    publish_wallet_funded
)
from app.database import get_db
from app.utils import save_base64_receipt_to_disk
from app.schemas import (
    RefundBalanceResponse, 
    RefundRequestCreate, 
    TransactionResponse,
    RefundProcessRequest,
    TopupNoteUpdate,
    AdminDirectRefundRequest,
)
from app.core.dependencies import get_current_user_id
from vpn_shared.jwt_auth import get_current_user_payload

router = APIRouter(prefix="/refunds", tags=["Refunds"])


# ==========================================
# CLIENT REFUND ENDPOINTS
# ==========================================

def get_refund_cooldown_status(db: Session, user_id: uuid.UUID) -> dict:
    """
    ANTI-MONEY-LAUNDERING (AML) REFUND COOLDOWN:
    Cooldown is anchored to the validation/approval date (processed_at) of the
    user's latest approved top-up, as well as any prior approved/pending refunds.
    """
    settings = db.query(SystemFinancialSettings).first()
    cooldown_days = int(settings.refund_cooldown_days) if (settings and settings.refund_cooldown_days) else 30
    now = datetime.now(timezone.utc)

    # 1. LATEST APPROVED TOP-UP (ANCHORED TO PROCESSED_AT WHEN ADMIN APPROVED IT)
    latest_topup = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.tx_type == "Topup",
        Transaction.status.in_(["Approved", "Completed"])
    ).order_by(
        func.coalesce(Transaction.processed_at, Transaction.created_at).desc()
    ).first()

    # 2. LATEST PENDING OR APPROVED REFUND
    latest_refund = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.tx_type == "Refund",
        Transaction.status.in_(["Pending", "Approved", "Completed"])
    ).order_by(
        func.coalesce(Transaction.processed_at, Transaction.created_at).desc()
    ).first()

    anchor_dates = []

    if latest_topup:
        t_date = latest_topup.processed_at or latest_topup.created_at
        if t_date:
            anchor_dates.append(t_date.replace(tzinfo=timezone.utc) if t_date.tzinfo is None else t_date)

    if latest_refund:
        r_date = latest_refund.processed_at or latest_refund.created_at
        if r_date:
            anchor_dates.append(r_date.replace(tzinfo=timezone.utc) if r_date.tzinfo is None else r_date)

    # No approved deposits or refunds exist
    if not anchor_dates:
        return {
            "can_refund": True,
            "cooldown_days": cooldown_days,
            "refund_cooldown_days": cooldown_days,
            "days_until_next_refund": 0
        }

    latest_anchor = max(anchor_dates)
    elapsed_seconds = (now - latest_anchor).total_seconds()
    required_seconds = cooldown_days * 86400

    if elapsed_seconds < required_seconds:
        remaining_seconds = required_seconds - elapsed_seconds
        days_left = max(1, math.ceil(remaining_seconds / 86400))
        return {
            "can_refund": False,
            "cooldown_days": cooldown_days,
            "refund_cooldown_days": cooldown_days,
            "days_until_next_refund": days_left
        }

    return {
        "can_refund": True,
        "cooldown_days": cooldown_days,
        "refund_cooldown_days": cooldown_days,
        "days_until_next_refund": 0
    }


@router.get("/balances", response_model=RefundBalanceResponse)
def get_refundable_balances(
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id)
):
    """
    Returns the user's refundable deposit balance (FIAT)
    and current cooldown eligibility.
    """
    fiat_balance = db.query(func.sum(WalletBucket.balance)).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type == "FIAT",
        WalletBucket.is_active == True
    ).scalar() or 0.0

    cooldown_info = get_refund_cooldown_status(db, user_id)

    return RefundBalanceResponse(
        available_balance=float(fiat_balance),
        can_refund=cooldown_info["can_refund"],
        refund_cooldown_days=cooldown_info["cooldown_days"],
        days_until_next_refund=cooldown_info["days_until_next_refund"]
    )



@router.get("/eligibility")
def check_refund_eligibility(
    db: Session = Depends(get_db), 
    user_id: uuid.UUID = Depends(get_current_user_id)
):
    """Checks whether the client can submit a refund claim based on admin cooldown settings."""
    return get_refund_cooldown_status(db, user_id)


@router.post("/admin-direct-refund", response_model=TransactionResponse)
async def admin_direct_refund(
    req_in: AdminDirectRefundRequest,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin directly creates and completes a refund transaction without a client request.
    Verifies that the refund amount does not exceed available paid credit.
    """
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    if req_in.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Refund amount must be greater than zero.")

    fiat_bucket = db.query(WalletBucket).filter(
        WalletBucket.user_id == req_in.client_id,
        WalletBucket.bucket_type == "FIAT",
        WalletBucket.is_active == True
    ).first()

    total_avail = float(fiat_bucket.balance) if fiat_bucket else 0.0

    if total_avail <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Client has no refundable paid credit. Gift balances are non-refundable."
        )

    if req_in.amount > total_avail:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Refund amount ({req_in.amount:,.0f} Toman) exceeds client's available paid credit ({total_avail:,.0f} Toman)."
        )

    fiat_bucket.balance -= req_in.amount
    if fiat_bucket.balance <= 0 and not fiat_bucket.empty_since:
        fiat_bucket.empty_since = datetime.now(timezone.utc)

    total_remaining = db.query(func.sum(WalletBucket.balance)).filter(
        WalletBucket.user_id == req_in.client_id,
        WalletBucket.is_active == True
    ).scalar() or 0.0

    settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
    safety_cutoff = float(settings.safety_cutoff_threshold or 0.0)

    if total_remaining <= safety_cutoff:
        await publish_wallet_empty(str(req_in.client_id))

    ref_id = f"RFD-{uuid.uuid4().hex[:8].upper()}"
    tx = Transaction(
        reference_id=ref_id,
        user_id=req_in.client_id,
        wallet_bucket_id=fiat_bucket.id,
        tx_type="Refund",
        amount=req_in.amount,
        status="Approved",
        payment_method=req_in.destination_account or "Direct Admin Refund",
        description=req_in.reason or "Direct refund issued by administrator",
        admin_notes="Directly issued by Administrator",
        processed_at=datetime.now(timezone.utc)
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    await publish_payment_processed(
        user_id=str(req_in.client_id),
        tx_type="Refund",
        status="Approved",
        amount=req_in.amount,
        reference_id=tx.reference_id,
        admin_notes=tx.admin_notes,
        currency=getattr(tx, "currency", "IRT") or "IRT"
    )

    return tx


@router.post("/request", response_model=TransactionResponse)
async def request_refund(
    req_in: RefundRequestCreate,
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id)
):
    """
    Client submits a refund request for their unconsumed deposit balance.
    - USD users provide a Crypto wallet address (USDT/TON).
    - IRT users provide a 16-digit Shetab bank card number.
    """
    acc = db.query(Account).filter(Account.user_id == user_id).first()
    user_currency = str(getattr(acc, "billing_currency", "IRT") or "IRT").upper()

    cooldown_info = get_refund_cooldown_status(db, user_id)
    if not cooldown_info["can_refund"]:
        raise HTTPException(
            status_code=400,
            detail=f"Refund limit reached: You can submit 1 refund request every {cooldown_info['cooldown_days']} days. Next refund available in {cooldown_info['days_until_next_refund']} day(s)."
        )

    # For USD users, funds are in CRYPTO or FIAT bucket matching user_currency
    target_bucket = db.query(WalletBucket).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type != "GIFT",
        WalletBucket.is_active == True
    ).first()

    total_avail = float(target_bucket.balance) if target_bucket else 0.0
    unit_label = "$" if user_currency == "USD" else "Toman"

    if req_in.amount <= 0 or req_in.amount > total_avail:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid refund amount ({req_in.amount:.2f} {unit_label} requested, {total_avail:.2f} {unit_label} available)."
        )

    # Deduct amount from the refundable bucket
    target_bucket.balance -= req_in.amount
    if target_bucket.balance <= 0 and not target_bucket.empty_since:
        target_bucket.empty_since = datetime.now(timezone.utc)

    # Check if total wallet balance across all buckets is depleted
    total_remaining_balance = db.query(func.sum(WalletBucket.balance)).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.is_active == True
    ).scalar() or 0.0

    if total_remaining_balance <= 0:
        await publish_wallet_empty(str(user_id))

    ref_id = f"RFD-{uuid.uuid4().hex[:8].upper()}"

    tx = Transaction(
        reference_id=ref_id,
        user_id=user_id,
        wallet_bucket_id=target_bucket.id if target_bucket else None,
        tx_type="Refund",
        amount=req_in.amount,
        currency=user_currency,
        status="Pending",
        payment_method=req_in.destination_account,
        description=req_in.reason or f"Refund request ({user_currency}) to {req_in.destination_account}"
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    await publish_payment_submitted(
        user_id=str(user_id),
        tx_type="Refund",
        amount=tx.amount,
        reference_id=tx.reference_id
    )

    return tx


@router.get("/my", response_model=List[TransactionResponse])
def get_my_refund_requests(
    status: Optional[str] = Query(None, description="Pending, Approved, Rejected"),
    min_amount: Optional[float] = Query(None),
    max_amount: Optional[float] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_id: uuid.UUID = Depends(get_current_user_id)
):
    """Returns filtered refund requests submitted by the authenticated client."""
    query = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.tx_type == "Refund"
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


# ==========================================
# ADMIN REFUND AUDIT & PROCESSING ENDPOINTS
# ==========================================

@router.get("/admin/all", response_model=List[TransactionResponse])
def get_all_refunds_admin(
    status: Optional[str] = Query(None, description="Pending, Approved, Rejected, or All"),
    search: Optional[str] = Query(None),
    min_amount: Optional[float] = Query(None),
    max_amount: Optional[float] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """Admin retrieves all client refund requests across the system."""
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    query = db.query(Transaction).filter(Transaction.tx_type == "Refund")

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

    # Attach usernames for admin view
    client_ids = list({t.user_id for t in results if t.user_id})
    if client_ids:
        accounts = db.query(Account.user_id, Account.username).filter(Account.user_id.in_(client_ids)).all()
        user_map = {acc.user_id: acc.username for acc in accounts if acc.username}
        for t in results:
            if t.user_id in user_map:
                setattr(t, "client_username", user_map[t.user_id])

    return results


@router.get("/pending", response_model=List[TransactionResponse])
def get_pending_refunds(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """Admin views pending refund claims."""
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    return db.query(Transaction).filter(
        Transaction.tx_type == "Refund", 
        Transaction.status == "Pending"
    ).order_by(Transaction.created_at.desc()).all()


@router.post("/{tx_id}/process", response_model=TransactionResponse)
async def process_refund(
    tx_id: uuid.UUID,
    process_in: RefundProcessRequest,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin processes a pending Refund request with proof.
    - If Approved: Status = 'Approved', payout receipt and tracking ref recorded.
    - If Rejected: Status = 'Rejected', locked funds returned back to user's WalletBucket.
    """
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Admin privileges required to process refund requests."
        )

    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, 
        Transaction.tx_type == "Refund",
        Transaction.status == "Pending"
    ).first()

    if not tx:
        raise HTTPException(status_code=404, detail="Pending refund request not found.")

    action_clean = process_in.action.strip().capitalize()
    saved_receipt_path = save_base64_receipt_to_disk(process_in.proof_receipt_url)

    if action_clean == "Approve":
        # ENFORCE REQUIRED RECEIPT IMAGE
        if not saved_receipt_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A payout receipt screenshot is required to approve this refund."
            )
            
        tx.status = "Approved"
        if process_in.tx_hash_or_ref:
            tx.payment_method = f"{tx.payment_method} (Ref: {process_in.tx_hash_or_ref.strip()})"
        tx.receipt_url = saved_receipt_path

    elif action_clean == "Reject":
        tx.status = "Rejected"
        if saved_receipt_path:
            tx.receipt_url = saved_receipt_path
        
        # Return locked funds back to client's bucket
        if tx.wallet_bucket_id:
            bucket = db.query(WalletBucket).filter(WalletBucket.id == tx.wallet_bucket_id).first()
            if bucket:
                total_before = db.query(func.sum(WalletBucket.balance)).filter(
                    WalletBucket.user_id == tx.user_id,
                    WalletBucket.is_active == True
                ).scalar() or 0.0

                bucket.balance += tx.amount

                # Re-enable VPN configs if wallet was empty but now has positive balance
                if total_before <= 0 and (total_before + tx.amount) > 0:
                    await publish_wallet_funded(str(tx.user_id))
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid action. Must be 'Approve' or 'Reject'."
        )

    tx.admin_notes = process_in.notes
    tx.processed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(tx)

    await publish_payment_processed(
        user_id=str(tx.user_id),
        tx_type="Refund",
        status=tx.status,
        amount=tx.amount,
        reference_id=tx.reference_id,
        admin_notes=tx.admin_notes,
        currency=getattr(tx, "currency", "IRT") or "IRT",
        tracking_ref=process_in.tx_hash_or_ref.strip() if process_in.tx_hash_or_ref else None
    )

    return tx


@router.patch("/{tx_id}/note", response_model=TransactionResponse)
def update_refund_note(
    tx_id: uuid.UUID,
    note_in: TopupNoteUpdate,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """Admin updates audit/processing notes on a refund transaction without altering its status."""
    if str(user_payload.get("role", "")).capitalize() != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.tx_type == "Refund").first()
    if not tx:
        raise HTTPException(status_code=404, detail="Refund transaction not found.")

    tx.admin_notes = note_in.notes.strip()
    db.commit()
    db.refresh(tx)
    return tx