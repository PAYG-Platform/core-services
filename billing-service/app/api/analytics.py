# backend/billing-service/app/api/analytics.py
import logging
import uuid
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone

from app.database import get_db
from app.models import WalletBucket, Transaction
from mng_shared.jwt_auth import get_current_user_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["Financial Analytics & Metrics"])

NUMBER_OF_PAST_DAYS = 30

@router.get("/client/daily-financial-trends")
def get_client_daily_financial_trends(
    days: int = Query(NUMBER_OF_PAST_DAYS, ge=7, le=90, description="Number of past days to query"),
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Returns daily approved top-up amounts and gift credits for the authenticated client.
    """
    try:
        user_id = uuid.UUID(str(user_payload["sub"]))
    except (KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user identification token."
        )

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)

    # 1. Approved / Completed Topups per day
    topup_records = db.query(
        func.date(Transaction.created_at).label("tx_date"),
        func.sum(Transaction.amount).label("daily_topups")
    ).filter(
        Transaction.user_id == user_id,
        Transaction.tx_type == "Topup",
        Transaction.status.in_(["Approved", "Completed"]),
        func.date(Transaction.created_at) >= start_date
    ).group_by(func.date(Transaction.created_at)).all()

    topup_map = {
        str(r.tx_date): float(r.daily_topups or 0.0)
        for r in topup_records
    }

    # 2. Gift credits per day
    gift_records = db.query(
        func.date(Transaction.created_at).label("tx_date"),
        func.sum(Transaction.amount).label("daily_gifts")
    ).filter(
        Transaction.user_id == user_id,
        Transaction.tx_type == "Gift",
        Transaction.status.in_(["Approved", "Completed"]),
        Transaction.amount > 0,
        func.date(Transaction.created_at) >= start_date
    ).group_by(func.date(Transaction.created_at)).all()

    gift_map = {
        str(r.tx_date): float(r.daily_gifts or 0.0)
        for r in gift_records
    }

    results = []
    for i in range(days):
        current_day = start_date + timedelta(days=i)
        d_str = current_day.isoformat()
        results.append({
            "day": current_day.strftime("%b %d"),
            "date_iso": d_str,
            "topup_toman": topup_map.get(d_str, 0.0),
            "gift_toman": gift_map.get(d_str, 0.0)
        })

    return results


@router.get("/admin/users/{target_user_id}/daily-financial-trends")
def get_admin_user_daily_financial_trends(
    target_user_id: uuid.UUID,
    days: int = Query(NUMBER_OF_PAST_DAYS, ge=7, le=90, description="Number of past days to query"),
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin-only: Returns daily approved top-up amounts and gift credits for a specific user.
    """
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required."
        )

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)

    topup_records = db.query(
        func.date(Transaction.created_at).label("tx_date"),
        func.sum(Transaction.amount).label("daily_topups")
    ).filter(
        Transaction.user_id == target_user_id,
        Transaction.tx_type == "Topup",
        Transaction.status.in_(["Approved", "Completed"]),
        func.date(Transaction.created_at) >= start_date
    ).group_by(func.date(Transaction.created_at)).all()

    topup_map = {
        str(r.tx_date): float(r.daily_topups or 0.0)
        for r in topup_records
    }

    gift_records = db.query(
        func.date(Transaction.created_at).label("tx_date"),
        func.sum(Transaction.amount).label("daily_gifts")
    ).filter(
        Transaction.user_id == target_user_id,
        Transaction.tx_type == "Gift",
        Transaction.status.in_(["Approved", "Completed"]),
        Transaction.amount > 0,
        func.date(Transaction.created_at) >= start_date
    ).group_by(func.date(Transaction.created_at)).all()

    gift_map = {
        str(r.tx_date): float(r.daily_gifts or 0.0)
        for r in gift_records
    }

    results = []
    for i in range(days):
        current_day = start_date + timedelta(days=i)
        d_str = current_day.isoformat()
        results.append({
            "day": current_day.strftime("%b %d"),
            "date_iso": d_str,
            "topup_toman": topup_map.get(d_str, 0.0),
            "gift_toman": gift_map.get(d_str, 0.0)
        })

    return results


@router.get("/admin/summary")
def get_admin_financial_summary(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    user_role = str(user.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Admin privileges required."
        )

    # 1. TOMAN (IRT) SEPARATED METRICS
    gross_deposits_irt = db.query(func.sum(Transaction.amount)).outerjoin(
        WalletBucket, Transaction.wallet_bucket_id == WalletBucket.id
    ).filter(
        Transaction.tx_type == "Topup",
        Transaction.status.in_(["Approved", "Completed"]),
        func.upper(func.coalesce(Transaction.currency, 'IRT')) == "IRT",
        (WalletBucket.bucket_type != "GIFT") | (Transaction.wallet_bucket_id == None)
    ).scalar() or 0.0

    refunds_approved_irt = db.query(func.sum(Transaction.amount)).filter(
        Transaction.tx_type == "Refund",
        Transaction.status == "Approved",
        func.upper(func.coalesce(Transaction.currency, 'IRT')) == "IRT"
    ).scalar() or 0.0

    refunds_pending_irt = db.query(func.sum(Transaction.amount)).filter(
        Transaction.tx_type == "Refund",
        Transaction.status == "Pending",
        func.upper(func.coalesce(Transaction.currency, 'IRT')) == "IRT"
    ).scalar() or 0.0

    net_deposits_irt = max(0.0, gross_deposits_irt - refunds_approved_irt - refunds_pending_irt)

    paid_used_irt = db.query(func.sum(Transaction.amount)).join(
        WalletBucket, Transaction.wallet_bucket_id == WalletBucket.id
    ).filter(
        Transaction.tx_type == "Deduction",
        Transaction.status == "Completed",
        func.upper(func.coalesce(Transaction.currency, 'IRT')) == "IRT",
        WalletBucket.bucket_type != "GIFT",
        ~Transaction.description.ilike("%(GIFT)%")
    ).scalar() or 0.0

    # 2. DOLLAR (USD) SEPARATED METRICS
    gross_deposits_usd = db.query(func.sum(Transaction.amount)).outerjoin(
        WalletBucket, Transaction.wallet_bucket_id == WalletBucket.id
    ).filter(
        Transaction.tx_type == "Topup",
        Transaction.status.in_(["Approved", "Completed"]),
        func.upper(func.coalesce(Transaction.currency, '')) == "USD",
        (WalletBucket.bucket_type != "GIFT") | (Transaction.wallet_bucket_id == None)
    ).scalar() or 0.0

    refunds_approved_usd = db.query(func.sum(Transaction.amount)).filter(
        Transaction.tx_type == "Refund",
        Transaction.status == "Approved",
        func.upper(func.coalesce(Transaction.currency, '')) == "USD"
    ).scalar() or 0.0

    refunds_pending_usd = db.query(func.sum(Transaction.amount)).filter(
        Transaction.tx_type == "Refund",
        Transaction.status == "Pending",
        func.upper(func.coalesce(Transaction.currency, '')) == "USD"
    ).scalar() or 0.0

    net_deposits_usd = max(0.0, gross_deposits_usd - refunds_approved_usd - refunds_pending_usd)

    paid_used_usd = db.query(func.sum(Transaction.amount)).join(
        WalletBucket, Transaction.wallet_bucket_id == WalletBucket.id
    ).filter(
        Transaction.tx_type == "Deduction",
        Transaction.status == "Completed",
        func.upper(func.coalesce(Transaction.currency, '')) == "USD",
        WalletBucket.bucket_type != "GIFT",
        ~Transaction.description.ilike("%(GIFT)%")
    ).scalar() or 0.0

    total_active_wallets = db.query(func.count(func.distinct(WalletBucket.user_id))).filter(
        WalletBucket.is_active == True,
        WalletBucket.balance > 0
    ).scalar() or 0

    return {
        "total_deposits": round(float(net_deposits_irt)),
        "total_deposits_irt": round(float(net_deposits_irt)),
        "total_deposits_usd": round(float(net_deposits_usd), 2),
        "total_used_value": round(float(paid_used_irt)),
        "total_used_value_irt": round(float(paid_used_irt)),
        "total_used_value_usd": round(float(paid_used_usd), 2),
        "total_refunds_approved_irt": round(float(refunds_approved_irt)),
        "total_refunds_approved_usd": round(float(refunds_approved_usd), 2),
        "total_refunds_pending_irt": round(float(refunds_pending_irt)),
        "total_refunds_pending_usd": round(float(refunds_pending_usd), 2),
        "total_refunds_approved": round(float(refunds_approved_irt)),
        "total_refunds_pending": round(float(refunds_pending_irt)),
        "total_active_wallets": int(total_active_wallets)
    }


@router.get("/admin/daily-trends")
def get_admin_daily_financial_trends(
    days: int = Query(NUMBER_OF_PAST_DAYS, ge=7, le=90, description="Number of past days to aggregate financial metrics"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    user_role = str(user.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)

    # 1. Query IRT (Toman) Deposits
    deposit_records_irt = db.query(
        func.date(Transaction.created_at).label("tx_date"),
        func.sum(Transaction.amount).label("daily_deposits")
    ).outerjoin(
        WalletBucket, Transaction.wallet_bucket_id == WalletBucket.id
    ).filter(
        Transaction.tx_type == "Topup",
        Transaction.status.in_(["Approved", "Completed"]),
        func.upper(func.coalesce(Transaction.currency, 'IRT')) == 'IRT',
        func.date(Transaction.created_at) >= start_date,
        (WalletBucket.bucket_type != "GIFT") | (Transaction.wallet_bucket_id == None)
    ).group_by(func.date(Transaction.created_at)).all()

    deposit_irt_map = {
        str(r.tx_date): float(r.daily_deposits or 0.0)
        for r in deposit_records_irt
    }

    # 2. Query USD ($) Deposits
    deposit_records_usd = db.query(
        func.date(Transaction.created_at).label("tx_date"),
        func.sum(Transaction.amount).label("daily_deposits")
    ).outerjoin(
        WalletBucket, Transaction.wallet_bucket_id == WalletBucket.id
    ).filter(
        Transaction.tx_type == "Topup",
        Transaction.status.in_(["Approved", "Completed"]),
        func.upper(func.coalesce(Transaction.currency, '')) == 'USD',
        func.date(Transaction.created_at) >= start_date,
        (WalletBucket.bucket_type != "GIFT") | (Transaction.wallet_bucket_id == None)
    ).group_by(func.date(Transaction.created_at)).all()

    deposit_usd_map = {
        str(r.tx_date): float(r.daily_deposits or 0.0)
        for r in deposit_records_usd
    }

    usage_toman_records = db.query(
        func.date(Transaction.created_at).label("tx_date"),
        func.sum(Transaction.amount).label("daily_used_toman")
    ).join(
        WalletBucket, Transaction.wallet_bucket_id == WalletBucket.id
    ).filter(
        Transaction.tx_type == "Deduction",
        Transaction.status == "Completed",
        WalletBucket.bucket_type != "GIFT",
        func.date(Transaction.created_at) >= start_date
    ).group_by(func.date(Transaction.created_at)).all()

    usage_toman_map = {
        str(r.tx_date): float(r.daily_used_toman or 0.0)
        for r in usage_toman_records
    }

    results = []
    for i in range(days):
        current_day = start_date + timedelta(days=i)
        d_str = current_day.isoformat()
        deposits_toman = deposit_irt_map.get(d_str, 0.0)
        deposits_usd_val = deposit_usd_map.get(d_str, 0.0)
        used_toman = usage_toman_map.get(d_str, 0.0)

        results.append({
            "day": current_day.strftime("%b %d"),
            "date_iso": d_str,
            "deposits": deposits_toman,
            "deposits_irt": deposits_toman,
            "deposits_usd": round(deposits_usd_val, 2),
            "paid_usage_toman": used_toman
        })

    return results