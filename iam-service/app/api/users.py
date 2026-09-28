# backend/iam-service/app/api/users.py
import os
import math
import uuid
import secrets
import logging
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_

from vpn_shared.schemas import RoleEnum
from vpn_shared import EventKeys
from app.events.publisher import (
    broker, 
    publish_user_created_event,
    publish_user_telegram_linked_event,
    publish_user_telegram_unlinked_event,
    publish_password_changed_event,
    publish_recovery_keys_regenerated_event,
    publish_user_status_changed_event
)
from app.database import get_db
from app.models import User, RecoveryKey, IamSettings
from app.events.consumer import get_current_cooldown_minutes
from app.schemas import (
    UserProfileResponse,
    UserProfileUpdate,
    PasswordChangeRequest,
    RecoveryKeysResponse,
    ForcePasswordChangeRequest,
    ForcePasswordChangeResponse,
    PasswordVerificationRequest,
    AdminCreateClientRequest,
    TelegramLinkResponse,
    TelegramLinkConfirmRequest
)
from app.core.security import (
    hash_password,
    verify_password,
    decode_access_token,
    generate_recovery_keys
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["User Profile Management"])

COOKIE_NAME = "access_token"


@router.get("/bulk-info", response_model=List[UserProfileResponse])
def get_bulk_users_info(ids: str, db: Session = Depends(get_db)):
    """Fetch basic user info for a comma-separated list of UUIDs."""
    uuid_list = []
    for id_str in ids.split(","):
        id_str = id_str.strip()
        if id_str:
            try:
                uuid_list.append(uuid.UUID(id_str))
            except ValueError:
                pass
    if not uuid_list:
        return []
    users = db.query(User).filter(User.id.in_(uuid_list)).all()
    return [UserProfileResponse.from_orm(u) for u in users]


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Extracts JWT token from HttpOnly cookie or Bearer header and returns User model."""
    token = request.cookies.get(COOKIE_NAME)
    
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header

    if not token or token in ["null", "undefined", '""', "''"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in."
        )

    token_str = token.replace("Bearer ", "").strip().strip('"').strip("'")
    payload = decode_access_token(token_str)

    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token."
        )

    user = db.query(User).filter(User.id == payload["sub"]).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found."
        )

    return user


@router.get("/me", response_model=UserProfileResponse)
def get_my_profile(current_user: User = Depends(get_current_user)):
    """Retrieve current user's profile details."""
    return UserProfileResponse.from_orm(current_user)


@router.put("/me", response_model=UserProfileResponse)
def update_my_profile(
    profile_in: UserProfileUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update profile details (email, optional display name, language).
    Note: Username and billing_currency are strictly immutable and cannot be updated.
    """
    if profile_in.email and profile_in.email.lower() != current_user.email:
        new_email = profile_in.email.lower()
        existing_email_user = db.query(User).filter(
            User.email == new_email, 
            User.id != current_user.id
        ).first()
        
        if existing_email_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email address is already in use by another account."
            )
        current_user.email = new_email

    if profile_in.name is not None:
        current_user.name = profile_in.name.strip() if profile_in.name.strip() else None

    if profile_in.language is not None:
        current_user.language = profile_in.language.strip().lower()[:10]

    db.commit()
    db.refresh(current_user)

    return UserProfileResponse.from_orm(current_user)


@router.post("/me/force-change-password", response_model=ForcePasswordChangeResponse)
async def force_change_password(
    req: ForcePasswordChangeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Mandatory password change for seller-created accounts on first login:
    - Clears the must_change_password flag.
    - Revokes old keys and generates 12 fresh recovery keys.
    """
    if not current_user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Account does not require forced password change."
        )

    current_user.password_hash = hash_password(req.new_password)
    current_user.must_change_password = False

    db.query(RecoveryKey).filter(RecoveryKey.user_id == current_user.id).delete()
    new_raw_keys = generate_recovery_keys(count=12, length=12)
    for k in new_raw_keys:
        db.add(RecoveryKey(user_id=current_user.id, key_code=k))

    db.commit()
    db.refresh(current_user)

    # Trigger security notification on web and Telegram
    await publish_password_changed_event(current_user.id)

    return ForcePasswordChangeResponse(
        message="Password updated successfully and recovery keys generated.",
        recovery_keys=new_raw_keys,
        user=UserProfileResponse.from_orm(current_user)
    )


@router.post("/me/password")
async def change_password(
    password_in: PasswordChangeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Change user password after verifying current old password and trigger security alert."""
    if not verify_password(password_in.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect current password."
        )

    current_user.password_hash = hash_password(password_in.new_password)
    db.commit()

    # Trigger security notification on web and Telegram
    await publish_password_changed_event(current_user.id)

    return {"message": "Password changed successfully."}


@router.post("/me/recovery-keys/view", response_model=RecoveryKeysResponse)
def get_my_recovery_keys(
    req: PasswordVerificationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Verifies user password and retrieves active (unused) 12-character recovery keys."""
    if not verify_password(req.password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect password. Verification failed."
        )

    keys = db.query(RecoveryKey).filter(
        RecoveryKey.user_id == current_user.id,
        RecoveryKey.is_used == False
    ).all()

    key_codes = [k.key_code for k in keys]

    return RecoveryKeysResponse(
        message="Recovery keys retrieved successfully.",
        count=len(key_codes),
        keys=key_codes
    )


@router.post("/me/recovery-keys/regenerate", response_model=RecoveryKeysResponse)
async def regenerate_recovery_keys(
    req: PasswordVerificationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Verifies user password, revokes existing keys, generates 12 new recovery keys,
    and publishes a security notification to Web and Telegram.
    """
    if not verify_password(req.password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect password. Verification failed."
        )

    db.query(RecoveryKey).filter(RecoveryKey.user_id == current_user.id).delete()

    new_raw_keys = generate_recovery_keys(count=12, length=12)

    for key_code in new_raw_keys:
        db.add(RecoveryKey(user_id=current_user.id, key_code=key_code))

    db.commit()

    # Trigger security notification on web app and Telegram
    await publish_recovery_keys_regenerated_event(current_user.id)

    return RecoveryKeysResponse(
        message="Previous recovery keys invalidated. New recovery keys generated successfully.",
        count=len(new_raw_keys),
        keys=new_raw_keys
    )


@router.post("/me/toggle-status")
async def self_toggle_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Client Self-Service VPN Suspension / Pause:
    - Cooldown duration (in minutes) configured dynamically by Admin.
    - If set to 0, immediate activation/deactivation with zero cooldown.
    - Publishes an asynchronous event to Node Service.
    """
    now = datetime.now(timezone.utc)
    cooldown_mins = get_current_cooldown_minutes(db)

    if cooldown_mins > 0 and current_user.status_toggled_at:
        toggled_at = current_user.status_toggled_at
        if toggled_at.tzinfo is None:
            toggled_at = toggled_at.replace(tzinfo=timezone.utc)
            
        elapsed = now - toggled_at
        limit_delta = timedelta(minutes=cooldown_mins)

        if elapsed < limit_delta:
            seconds_remaining = (limit_delta - elapsed).total_seconds()
            minutes_remaining = math.ceil(seconds_remaining / 60)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit reached: You can toggle your VPN status once every {cooldown_mins} minute(s). Please try again in ~{minutes_remaining} minute(s)."
            )

    current_user.is_self_suspended = not current_user.is_self_suspended
    current_user.status_toggled_at = now
    db.commit()
    db.refresh(current_user)

    # Publish event to RabbitMQ for Node Service
    try:
        if current_user.is_self_suspended:
            await broker.publish({"user_id": str(current_user.id)}, queue=EventKeys.USER_VPN_PAUSED)
        else:
            await broker.publish({"user_id": str(current_user.id)}, queue=EventKeys.USER_VPN_RESUMED)
    except Exception as e:
        logger.error(f"Failed to publish VPN toggle event to message broker: {e}")

    action_label = "Paused (VPN Disabled)" if current_user.is_self_suspended else "Active (VPN Resumed)"
    return {
        "message": f"VPN status changed to {action_label}.",
        "is_active": current_user.is_active,
        "is_self_suspended": current_user.is_self_suspended,
        "status_toggled_at": current_user.status_toggled_at
    }


@router.patch("/{user_id}/status")
async def admin_toggle_user_status(
    user_id: uuid.UUID,
    is_active: bool,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Admin Account Suspension / Activation:
    - If Suspended (is_active=False): Locks user account and publishes USER_SUSPENDED to RabbitMQ.
    - If Activated (is_active=True): Unlocks user account and publishes USER_ACTIVATED to RabbitMQ.
    - Completely decoupled via event-driven messaging.
    """
    role_val = current_user.role.value if hasattr(current_user.role, 'value') else str(current_user.role)
    if role_val.capitalize() != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to override user status."
        )

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User account not found.")

    target_user.is_active = is_active
    target_user.status_toggled_at = datetime.now(timezone.utc)
    db.commit()

    # Publish decoupled event to node-service, notification-service, and Telegram
    await publish_user_status_changed_event(target_user.id, is_active)

    return {
        "message": f"User account {'Activated' if is_active else 'Suspended'} by Admin.",
        "is_active": target_user.is_active
    }


@router.get("/admin/list-paginated")
def admin_list_users_paginated(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None),
    status: Optional[str] = Query("All"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Admin endpoint to view all registered users across all roles."""
    if current_user.role != RoleEnum.ADMIN:
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    query = db.query(User)

    if search and search.strip():
        s = f"%{search.strip().lower()}%"
        query = query.filter(or_(User.username.ilike(s), User.email.ilike(s)))

    if status and status.capitalize() in ["Active", "Suspended"]:
        is_act = status.capitalize() == "Active"
        query = query.filter(User.is_active == is_act)

    total = query.count()
    total_pages = math.ceil(total / limit) if total > 0 else 1
    offset = (page - 1) * limit
    users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "items": [UserProfileResponse.from_orm(u) for u in users],
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages
    }


@router.post("/admin/create-client", status_code=status.HTTP_201_CREATED)
async def admin_create_standalone_client(
    client_in: AdminCreateClientRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Admin creates a standalone Client account.
    Auto-generates 12 recovery keys and forces password change on first login.
    """
    if current_user.role != RoleEnum.ADMIN:
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    clean_username = client_in.username.strip().lower()
    clean_email = client_in.email.strip().lower()
    chosen_currency = "USD" if str(client_in.billing_currency or "").upper().strip() == "USD" else "IRT"

    if db.query(User).filter(User.username == clean_username).first():
        raise HTTPException(status_code=400, detail="Username is already taken.")
    if db.query(User).filter(User.email == clean_email).first():
        raise HTTPException(status_code=400, detail="Email is already registered.")

    new_user = User(
        username=clean_username,
        email=clean_email,
        password_hash=hash_password(client_in.password),
        role=RoleEnum.CLIENT,
        billing_currency=chosen_currency,
        must_change_password=True,
        is_active=True
    )
    db.add(new_user)
    db.flush()

    raw_keys = generate_recovery_keys(12, 12)
    for k in raw_keys:
        db.add(RecoveryKey(user_id=new_user.id, key_code=k))

    db.commit()
    db.refresh(new_user)

    role_str = new_user.role.value if hasattr(new_user.role, 'value') else str(new_user.role)

    await publish_user_created_event(
        user_id=new_user.id,
        username=new_user.username,
        email=new_user.email,
        role=role_str,
        parent_id=None,
        gift_balance=client_in.initial_gift_balance,
        billing_currency=new_user.billing_currency
    )

    return {
        "message": f"Client @{new_user.username} created successfully.",
        "user": UserProfileResponse.from_orm(new_user)
    }


@router.post("/internal/cooldown-sync")
def sync_cooldown_internal(
    minutes: int = Query(..., ge=0),
    db: Session = Depends(get_db)
):
    """Synchronizes cooldown minutes into iam_settings."""
    from app.events.consumer import set_current_cooldown_minutes
    set_current_cooldown_minutes(minutes)
    row = db.query(IamSettings).filter(IamSettings.key == "client_status_cooldown_minutes").first()
    if not row:
        db.add(IamSettings(key="client_status_cooldown_minutes", value=str(minutes)))
    else:
        row.value = str(minutes)
    db.commit()
    return {"status": "success", "client_status_cooldown_minutes": minutes}


# ==========================================
# TELEGRAM ACCOUNT LINKING (IAM DOMAIN)
# ==========================================

TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "PAYG_VPN_Bot")


@router.get("/me/telegram-link", response_model=TelegramLinkResponse)
def get_my_telegram_link(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Step 2: Returns single-use start token & deep link for user to click 'Connect Telegram'.
    """
    if not current_user.telegram_link_token:
        current_user.telegram_link_token = f"tk_{secrets.token_urlsafe(24)}"
        db.commit()
        db.refresh(current_user)

    bot_handle = TELEGRAM_BOT_USERNAME.strip().lstrip("@")
    deep_link = f"https://t.me/{bot_handle}?start={current_user.telegram_link_token}"

    return TelegramLinkResponse(
        telegram_link=deep_link,
        token=current_user.telegram_link_token,
        is_connected=current_user.telegram_id is not None,
        telegram_id=current_user.telegram_id
    )


@router.post("/me/telegram-link/revoke", response_model=TelegramLinkResponse)
def revoke_my_telegram_link(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Generates a fresh linking token and invalidates the previous one."""
    current_user.telegram_link_token = f"tk_{secrets.token_urlsafe(24)}"
    db.commit()
    db.refresh(current_user)

    bot_handle = TELEGRAM_BOT_USERNAME.strip().lstrip("@")
    deep_link = f"https://t.me/{bot_handle}?start={current_user.telegram_link_token}"

    return TelegramLinkResponse(
        telegram_link=deep_link,
        token=current_user.telegram_link_token,
        is_connected=current_user.telegram_id is not None,
        telegram_id=current_user.telegram_id
    )


@router.post("/me/telegram/disconnect")
async def disconnect_my_telegram(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Unlinks Telegram account from this user in IAM and emits USER_TELEGRAM_UNLINKED."""
    old_tg_id = current_user.telegram_id
    current_user.telegram_id = None
    current_user.telegram_link_token = f"tk_{secrets.token_urlsafe(24)}"
    db.commit()

    if old_tg_id:
        await publish_user_telegram_unlinked_event(str(current_user.id), old_tg_id)

    return {"message": "Telegram account disconnected successfully."}


@router.post("/telegram/link")
async def confirm_telegram_linking(
    req: TelegramLinkConfirmRequest,
    db: Session = Depends(get_db)
):
    """
    Steps 4 & 5: Called by telegram-service when user hits /start tk_xxx.
    Verifies token, binds telegram_id to user, rotates token, and emits USER_TELEGRAM_LINKED.
    """
    clean_token = req.token.strip()
    clean_tg_id = str(req.telegram_id).strip()

    target_user = db.query(User).filter(User.telegram_link_token == clean_token).first()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid or expired Telegram linking token."
        )

    # Unlink telegram_id from any other user if previously linked
    existing = db.query(User).filter(User.telegram_id == clean_tg_id, User.id != target_user.id).first()
    if existing:
        existing.telegram_id = None

    target_user.telegram_id = clean_tg_id
    # Rotate token so it cannot be reused
    target_user.telegram_link_token = f"tk_{secrets.token_urlsafe(24)}"
    db.commit()
    db.refresh(target_user)

    await publish_user_telegram_linked_event(
        user_id=str(target_user.id),
        username=target_user.username,
        telegram_id=clean_tg_id
    )

    return {
        "message": "Telegram account linked successfully!",
        "user_id": str(target_user.id),
        "username": target_user.username,
        "telegram_id": clean_tg_id
    }


@router.patch("/me/language")
def update_my_language_preference(
    language: str = Query(..., min_length=2, max_length=10),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Directly persists the user's language selection to the database.
    Called when changing language from the top bar navigation.
    """
    clean_lang = language.strip().lower()[:10]
    current_user.language = clean_lang
    db.commit()
    db.refresh(current_user)

    logger.info(f"Updated user @{current_user.username} language preference to '{clean_lang}'.")
    return {
        "message": "Language preference updated successfully.",
        "language": clean_lang
    }