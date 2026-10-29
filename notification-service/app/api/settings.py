# backend/notification-service/app/api/settings.py
import secrets
import uuid
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import UserNotificationPreferences, TelegramBotConfig, UserAccount
from app.schemas import (
    ClientNotificationSettingsResponse,
    ClientNotificationSettingsUpdate,
    SellerNotificationSettingsResponse,
    SellerNotificationSettingsUpdate,
    TelegramLinkResponse,
    TelegramWebhookStart,
    TelegramBotConfigResponse,
    TelegramBotConfigUpdate
)
from mng_shared.jwt_auth import get_current_user_payload

router = APIRouter(prefix="/notifications/settings", tags=["Notification Settings & Telegram"])

BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "PAYG_MNG_Bot")


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def get_or_create_user_prefs(user_id: uuid.UUID, db: Session, user_currency: str = "IRT") -> UserNotificationPreferences:
    prefs = db.query(UserNotificationPreferences).filter(
        UserNotificationPreferences.user_id == user_id
    ).first()
    is_usd = str(user_currency or "").upper() == "USD"
    default_threshold = 1.00 if is_usd else 10000.0

    if not prefs:
        prefs = UserNotificationPreferences(
            user_id=user_id,
            currency="USD" if is_usd else "IRT",
            notify_low_balance=True,
            low_balance_threshold=default_threshold,
            is_low_balance_warned=False,
            notify_server_outage=True,
            notify_topup_approval=True,
            notify_client_topup=True,
            notify_client_out_of_credit=True,
            notify_admin_debt_limit=True,
            debt_limit_threshold=50000.0
        )
        db.add(prefs)
        db.commit()
        db.refresh(prefs)
    else:
        if is_usd and prefs.currency != "USD":
            prefs.currency = "USD"
            if prefs.low_balance_threshold >= 100.0:
                prefs.low_balance_threshold = 1.00
            db.commit()
            db.refresh(prefs)

    return prefs


# ==========================================
# ADMIN TELEGRAM BOT CONFIGURATION
# ==========================================

@router.get("/telegram-bot", response_model=TelegramBotConfigResponse)
def get_telegram_bot_config(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """Admin retrieves the platform Telegram Bot credentials from the database."""
    role = str(user_payload.get("role", "")).capitalize()
    if role != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    config = db.query(TelegramBotConfig).first()
    if not config:
        config = TelegramBotConfig(
            bot_username=BOT_USERNAME,
            bot_token=None
        )
        db.add(config)
        db.commit()
        db.refresh(config)

    return TelegramBotConfigResponse(
        bot_username=config.bot_username or BOT_USERNAME,
        bot_token=config.bot_token,
        admin_telegram_id=config.admin_telegram_id
    )


@router.put("/telegram-bot", response_model=TelegramBotConfigResponse)
def update_telegram_bot_config(
    bot_in: TelegramBotConfigUpdate,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """Admin updates the platform Telegram Bot username and token, saving directly in the database."""
    role = str(user_payload.get("role", "")).capitalize()
    if role != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    config = db.query(TelegramBotConfig).first()
    if not config:
        config = TelegramBotConfig(
            bot_username=BOT_USERNAME,
            bot_token=None
        )
        db.add(config)
        db.flush()

    if bot_in.bot_username is not None:
        clean_user = bot_in.bot_username.strip().lstrip("@")
        if clean_user:
            config.bot_username = clean_user

    if bot_in.bot_token is not None:
        config.bot_token = bot_in.bot_token.strip()

    if bot_in.admin_telegram_id is not None:
        config.admin_telegram_id = bot_in.admin_telegram_id.strip() or None

    db.commit()
    db.refresh(config)

    return TelegramBotConfigResponse(
        bot_username=config.bot_username,
        bot_token=config.bot_token,
        admin_telegram_id=config.admin_telegram_id
    )


# ==========================================
# ENDPOINTS: CLIENT PREFERENCES
# ==========================================

@router.get("/client-preferences", response_model=ClientNotificationSettingsResponse)
def get_client_preferences(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_id = uuid.UUID(str(user_payload["sub"]))
    user_acc = db.query(UserAccount).filter(UserAccount.user_id == user_id).first()
    user_currency = str(
        user_payload.get("billing_currency")
        or (user_acc.billing_currency if user_acc else None)
        or "IRT"
    ).upper()

    prefs = get_or_create_user_prefs(user_id, db, user_currency=user_currency)
    return ClientNotificationSettingsResponse(
        notify_low_balance=prefs.notify_low_balance,
        low_balance_threshold=prefs.low_balance_threshold,
        currency=prefs.currency or user_currency,
        notify_server_outage=prefs.notify_server_outage,
        notify_topup_approval=prefs.notify_topup_approval
    )


@router.put("/client-preferences", response_model=ClientNotificationSettingsResponse)
def update_client_preferences(
    prefs_in: ClientNotificationSettingsUpdate,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_id = uuid.UUID(str(user_payload["sub"]))
    user_acc = db.query(UserAccount).filter(UserAccount.user_id == user_id).first()
    user_currency = str(
        prefs_in.currency
        or user_payload.get("billing_currency")
        or (user_acc.billing_currency if user_acc else None)
        or "IRT"
    ).upper()

    prefs = get_or_create_user_prefs(user_id, db, user_currency=user_currency)

    if prefs_in.notify_low_balance is not None:
        prefs.notify_low_balance = prefs_in.notify_low_balance
    if prefs_in.low_balance_threshold is not None:
        prefs.low_balance_threshold = float(prefs_in.low_balance_threshold)
        # Re-arm warning trigger when user sets a new threshold
        prefs.is_low_balance_warned = False
    if prefs_in.currency is not None:
        prefs.currency = prefs_in.currency.upper().strip()
    if prefs_in.notify_server_outage is not None:
        prefs.notify_server_outage = prefs_in.notify_server_outage
    if prefs_in.notify_topup_approval is not None:
        prefs.notify_topup_approval = prefs_in.notify_topup_approval

    db.commit()
    db.refresh(prefs)
    return ClientNotificationSettingsResponse(
        notify_low_balance=prefs.notify_low_balance,
        low_balance_threshold=prefs.low_balance_threshold,
        currency=prefs.currency or user_currency,
        notify_server_outage=prefs.notify_server_outage,
        notify_topup_approval=prefs.notify_topup_approval
    )


# ==========================================
# ENDPOINTS: SELLER PREFERENCES
# ==========================================

@router.get("/seller-preferences", response_model=SellerNotificationSettingsResponse)
def get_seller_preferences(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_id = uuid.UUID(str(user_payload["sub"]))
    prefs = get_or_create_user_prefs(user_id, db)
    return SellerNotificationSettingsResponse(
        notify_client_topup=prefs.notify_client_topup,
        notify_client_out_of_credit=prefs.notify_client_out_of_credit,
        notify_admin_debt_limit=prefs.notify_admin_debt_limit,
        debt_limit_threshold=prefs.debt_limit_threshold
    )


@router.put("/seller-preferences", response_model=SellerNotificationSettingsResponse)
def update_seller_preferences(
    prefs_in: SellerNotificationSettingsUpdate,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_id = uuid.UUID(str(user_payload["sub"]))
    prefs = get_or_create_user_prefs(user_id, db)

    if prefs_in.notify_client_topup is not None:
        prefs.notify_client_topup = prefs_in.notify_client_topup
    if prefs_in.notify_client_out_of_credit is not None:
        prefs.notify_client_out_of_credit = prefs_in.notify_client_out_of_credit
    if prefs_in.notify_admin_debt_limit is not None:
        prefs.notify_admin_debt_limit = prefs_in.notify_admin_debt_limit
    if prefs_in.debt_limit_threshold is not None:
        prefs.debt_limit_threshold = prefs_in.debt_limit_threshold

    db.commit()
    db.refresh(prefs)
    return get_seller_preferences(db, user_payload)