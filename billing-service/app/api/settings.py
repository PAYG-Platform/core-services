# backend/billing-service/app/api/settings.py
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SystemFinancialSettings
from app.schemas import (
    SystemFinancialSettingsResponse,
    SystemFinancialSettingsUpdate,
)
from mng_shared.jwt_auth import get_current_user_payload

router = APIRouter(prefix="/settings", tags=["Financial Settings"])


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def get_or_create_system_financial_settings(db: Session) -> SystemFinancialSettings:
    settings = db.query(SystemFinancialSettings).first()

    if not settings:
        settings = SystemFinancialSettings(
            default_signup_gift_credit=20000.0,
            new_region_min_topup_amount=50000.0,
            existing_region_min_topup_amount=10000.0,
            region_grace_period_days=3,
            refund_cooldown_days=30,
            safety_cutoff_threshold=50.0,
            max_negative_balance=None,
            default_signup_gift_credit_usd=0.50,
            new_region_min_topup_amount_usd=2.00,
            existing_region_min_topup_amount_usd=1.00,
            safety_cutoff_threshold_usd=0.01,
            max_negative_balance_usd=None,
            usd_to_irt_exchange_rate=90000.0,
            admin_bank_cards=[],
            admin_crypto_wallets=[],
            admin_payment_instructions=(
                "Please include your username in the transfer memo "
                "and upload the receipt screenshot."
            ),
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)

    return settings


# ==========================================
# PUBLIC PRICING & MARKETING TERMS
# ==========================================

@router.get("/public-pricing")
def get_public_pricing_settings(db: Session = Depends(get_db)):
    """
    Public Endpoint: Allows visitors on the landing page to view
    the welcome gift credit, minimum deposit threshold, and billing model.
    No JWT required. Safe for unauthenticated usage.
    """
    s = get_or_create_system_financial_settings(db)
    return {
        "signup_gift_credit": float(s.default_signup_gift_credit or 20000.0),
        "signup_gift_credit_usd": float(getattr(s, "default_signup_gift_credit_usd", 0.50) or 0.50),
        "min_topup_amount": float(s.new_region_min_topup_amount or 50000.0),
        "min_topup_amount_usd": float(getattr(s, "new_region_min_topup_amount_usd", 2.00) or 2.00),
        "billing_model": "Pay-As-You-Go (Per GB)",
        "currency": "Toman"
    }


# ==========================================
# ADMIN SYSTEM FINANCIAL SETTINGS
# ==========================================

@router.get("/admin-financials", response_model=SystemFinancialSettingsResponse)
def get_admin_financial_settings(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Retrieves platform financial rules directly from billing_db.
    - Iranian (IRT) accounts see both Shetab bank cards and Crypto deposit wallets.
    - International (USD) accounts strictly see ONLY Crypto deposit wallets (Shetab cards hidden).
    """
    s = get_or_create_system_financial_settings(db)
    user_role = str(user_payload.get("role", "")).capitalize()
    user_currency = str(user_payload.get("billing_currency", "IRT") or "IRT").upper()

    # For International USD users (non-admin), hide Shetab cards completely
    if user_role != "Admin" and user_currency == "USD":
        filtered = SystemFinancialSettingsResponse.from_orm(s)
        filtered.admin_bank_cards = []
        return filtered

    return s


@router.put("/admin-financials", response_model=SystemFinancialSettingsResponse)
def update_admin_financial_settings(
    settings_in: SystemFinancialSettingsUpdate,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin-only: updates platform-wide regional rules, minimum thresholds, 
    grace period days, reservation TTL, and central deposit accounts.
    """
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Admin privileges required."
        )

    s = get_or_create_system_financial_settings(db)

    if settings_in.default_signup_gift_credit is not None:
        s.default_signup_gift_credit = settings_in.default_signup_gift_credit
    if settings_in.new_region_min_topup_amount is not None:
        s.new_region_min_topup_amount = settings_in.new_region_min_topup_amount
    if settings_in.existing_region_min_topup_amount is not None:
        s.existing_region_min_topup_amount = settings_in.existing_region_min_topup_amount
    if settings_in.region_grace_period_days is not None:
        s.region_grace_period_days = settings_in.region_grace_period_days
    if settings_in.refund_cooldown_days is not None:
        s.refund_cooldown_days = settings_in.refund_cooldown_days
        
    # Update Safety Cutoff & Negative Limit
    if settings_in.safety_cutoff_threshold is not None:
        s.safety_cutoff_threshold = max(0.0, float(settings_in.safety_cutoff_threshold))
        
    # Allows setting explicit float cap or passing None for Infinite
    s.max_negative_balance = settings_in.max_negative_balance

    # Update USD Settings & Thresholds
    if settings_in.default_signup_gift_credit_usd is not None:
        s.default_signup_gift_credit_usd = max(0.0, float(settings_in.default_signup_gift_credit_usd))
    if settings_in.new_region_min_topup_amount_usd is not None:
        s.new_region_min_topup_amount_usd = max(0.0, float(settings_in.new_region_min_topup_amount_usd))
    if settings_in.existing_region_min_topup_amount_usd is not None:
        s.existing_region_min_topup_amount_usd = max(0.0, float(settings_in.existing_region_min_topup_amount_usd))
    if settings_in.safety_cutoff_threshold_usd is not None:
        s.safety_cutoff_threshold_usd = max(0.0, float(settings_in.safety_cutoff_threshold_usd))
    if hasattr(settings_in, "max_negative_balance_usd"):
        s.max_negative_balance_usd = settings_in.max_negative_balance_usd
    if settings_in.usd_to_irt_exchange_rate is not None:
        s.usd_to_irt_exchange_rate = max(1.0, float(settings_in.usd_to_irt_exchange_rate))

    if settings_in.admin_payment_instructions is not None:
        s.admin_payment_instructions = settings_in.admin_payment_instructions.strip()
    if settings_in.admin_bank_cards is not None:
        s.admin_bank_cards = [
            c.model_dump() if hasattr(c, "model_dump") else c.dict() 
            for c in settings_in.admin_bank_cards
        ]
    if settings_in.admin_crypto_wallets is not None:
        s.admin_crypto_wallets = [
            w.model_dump() if hasattr(w, "model_dump") else w.dict() 
            for w in settings_in.admin_crypto_wallets
        ]

    db.commit()
    db.refresh(s)

    return s