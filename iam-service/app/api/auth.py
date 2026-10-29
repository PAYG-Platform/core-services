# backend/iam-service/app/api/auth.py
import os
import string
import secrets
import uuid
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import get_db
from app.models import User, RecoveryKey
from app.schemas import (
    UserCreate, 
    UserLogin, 
    UserResponse, 
    SignupResponse,
    LoginResponse,
    PasswordResetEmailRequest,
    PasswordResetConfirm
)
from app.core.security import (
    hash_password, 
    verify_password, 
    create_access_token, 
    decode_access_token
)
from app.events.publisher import publish_user_created_event, publish_password_changed_event
from mng_shared.schemas import RoleEnum

router = APIRouter(prefix="/auth", tags=["Authentication"])

COOKIE_NAME = "access_token"

# SECURITY CONFIGURATION (Dev vs Prod)
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_PROD = ENVIRONMENT == "production"

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "True" if IS_PROD else "False").lower() == "true"
COOKIE_HTTPONLY = os.getenv("COOKIE_HTTPONLY", "True" if IS_PROD else "False").lower() == "true"

EMAIL_DISCLAIMER = (
    "NOTICE: We do not send a verification email, but please ensure you use a valid email address. "
    "Without a valid email or your recovery keys, password recovery is virtually impossible."
)


def to_user_response(user_obj: User) -> UserResponse:
    """Safely converts SQLAlchemy User model to UserResponse schema across Pydantic v1 & v2."""
    if hasattr(UserResponse, "model_validate"):
        return UserResponse.model_validate(user_obj)
    return UserResponse.from_orm(user_obj)


def generate_recovery_keys(count: int = 12, length: int = 12) -> list[str]:
    """Generates 12 recovery keys, each 12 chars long with uppercase ASCII and digits (A-Z, 0-9)"""
    alphabet = string.ascii_uppercase + string.digits
    return [''.join(secrets.choice(alphabet) for _ in range(length)) for _ in range(count)]


def set_auth_cookie(response: Response, jwt_token: str):
    """
    Sets the HttpOnly authentication cookie:
    - HttpOnly=True (Protected against XSS attacks)
    - Secure=False in dev mode
    - SameSite=Lax (Standard cookie policy)
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=jwt_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
        max_age=60 * 60 * 24 * 7   # 7 days
    )


@router.get("/check-username")
def check_username_availability(username: str, db: Session = Depends(get_db)):
    """API endpoint to check if a username is unique/available"""
    clean_username = username.strip().lower()
    existing_user = db.query(User).filter(User.username == clean_username).first()
    return {"available": existing_user is None}


@router.get("/check-email")
def check_email_availability(email: str, db: Session = Depends(get_db)):
    """API endpoint to check if an email is unique/available"""
    clean_email = email.strip().lower()
    existing_user = db.query(User).filter(User.email == clean_email).first()
    return {"available": existing_user is None}


@router.post("/register", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def register_user(
    user_in: UserCreate, 
    response: Response, 
    db: Session = Depends(get_db)
):
    clean_username = user_in.username.strip().lower()
    clean_email = user_in.email.strip().lower()

    # 1. Check uniqueness
    if db.query(User).filter(User.username == clean_username).first():
        raise HTTPException(status_code=400, detail="Username is already taken.")
    if db.query(User).filter(User.email == clean_email).first():
        raise HTTPException(status_code=400, detail="Email is already registered.")

    # 2. Create User Record with explicit immutable billing_currency choice (Option B)
    clean_currency = "USD" if str(user_in.billing_currency or "").upper().strip() == "USD" else "IRT"
    hashed_pwd = hash_password(user_in.password)
    new_user = User(
        username=clean_username,
        email=clean_email,
        password_hash=hashed_pwd,
        role=RoleEnum.CLIENT,
        language=(user_in.language or "en").strip().lower()[:10],
        billing_currency=clean_currency,
        is_active=True,
        is_self_suspended=False
    )
    db.add(new_user)
    db.flush()  # Populate new_user.id UUID

    # Self-parenting fallback for Sellers & Admins
    if hasattr(new_user, "parent_id"):
        if not new_user.parent_id and new_user.role in [RoleEnum.SELLER, RoleEnum.ADMIN]:
            new_user.parent_id = new_user.id

    # 3. Generate 12 Recovery Keys
    raw_recovery_keys = generate_recovery_keys(count=12, length=12)
    for key_code in raw_recovery_keys:
        db.add(RecoveryKey(user_id=new_user.id, key_code=key_code))

    db.commit()
    db.refresh(new_user)

    role_str = new_user.role.value if hasattr(new_user.role, 'value') else str(new_user.role)

    token_payload = {
        "sub": str(new_user.id),
        "username": new_user.username,
        "email": new_user.email,
        "role": role_str,
        "language": getattr(new_user, "language", "en") or "en",
        "billing_currency": new_user.billing_currency,
        "parent_id": str(new_user.id)
    }
    jwt_token = create_access_token(data=token_payload)
    set_auth_cookie(response, jwt_token)

    # 4. Emit RabbitMQ event passing billing_currency to billing-service & node-service
    await publish_user_created_event(
        user_id=new_user.id,
        username=new_user.username,
        email=new_user.email,
        role=role_str,
        parent_id=None,
        gift_balance=0.0,
        billing_currency=new_user.billing_currency
    )

    return SignupResponse(
        message="Account created successfully. Initial gift credit issued.",
        disclaimer=EMAIL_DISCLAIMER,
        user=to_user_response(new_user),
        recovery_keys=raw_recovery_keys,
        access_token=jwt_token
    )


@router.post("/login", response_model=LoginResponse)
def login_user(
    credentials: UserLogin, 
    response: Response, 
    db: Session = Depends(get_db)
):
    identifier_clean = credentials.identifier.strip().lower()
    user = db.query(User).filter(
        or_(User.username == identifier_clean, User.email == identifier_clean)
    ).first()

    if not user or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid email/username or password."
        )

    # BLOCK LOGIN IF ACCOUNT IS SUSPENDED BY ADMIN
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended by an administrator. Please contact support."
        )

    role_str = user.role.value if hasattr(user.role, 'value') else str(user.role)

    token_payload = {
        "sub": str(user.id),
        "username": user.username,
        "email": user.email,
        "role": role_str,
        "language": getattr(user, "language", "en") or "en",
        "billing_currency": getattr(user, "billing_currency", "IRT") or "IRT",
        "parent_id": str(user.id)
    }
    jwt_token = create_access_token(data=token_payload)
    set_auth_cookie(response, jwt_token)

    return LoginResponse(
        message="Login successful.",
        user=to_user_response(user)
    )


@router.post("/logout")
def logout_user(response: Response):
    """Clears the authentication cookie"""
    response.delete_cookie(key=COOKIE_NAME)
    return {"message": "Logged out successfully."}


@router.post("/password-reset/request-email")
def request_password_reset_email(
    req: PasswordResetEmailRequest, 
    db: Session = Depends(get_db)
):
    clean_email = req.email.strip().lower()
    user = db.query(User).filter(User.email == clean_email).first()

    # Do not issue password reset keys if account is suspended by an administrator
    if user and user.is_active:
        temp_key = generate_recovery_keys(count=1, length=12)[0]
        db.add(RecoveryKey(user_id=user.id, key_code=temp_key))
        db.commit()
        print(f"\n==========================================")
        print(f"[MOCK EMAIL SENT TO: {user.email}]")
        print(f"Your Password Recovery Key is: {temp_key}")
        print(f"==========================================\n")

    return {
        "message": "If the email address exists in our system and is active, a recovery key has been sent. Please check your inbox."
    }


@router.post("/password-reset/confirm")
async def confirm_password_reset(
    req: PasswordResetConfirm, 
    db: Session = Depends(get_db)
):
    """Resets user password using a valid, unused recovery key and sends security alert"""
    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    key_code_clean = req.recovery_key.strip().upper()
    key_record = db.query(RecoveryKey).filter(
        RecoveryKey.key_code == key_code_clean,
        RecoveryKey.is_used == False
    ).first()

    if not key_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid or already used recovery key."
        )

    user = db.query(User).filter(User.id == key_record.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User account not found.")

    # BLOCK PASSWORD RESET IF SUSPENDED BY ADMIN
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been suspended by an administrator. Password reset is disabled."
        )

    user.password_hash = hash_password(req.new_password)
    key_record.is_used = True
    db.commit()

    # Trigger security notification on web and Telegram
    await publish_password_changed_event(user.id)

    return {"message": "Password has been reset successfully. You can now log in with your new password."}


@router.get("/me", response_model=UserResponse)
def get_current_user(request: Request, db: Session = Depends(get_db)):
    """Validates session token from cookie or Authorization header"""
    cookie_token = request.cookies.get(COOKIE_NAME)
    
    if not cookie_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            cookie_token = auth_header

    if not cookie_token or cookie_token in ["null", "undefined", '""', "''"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Authentication token is missing."
        )

    token_str = cookie_token.replace("Bearer ", "").strip()
    if token_str.startswith('"') and token_str.endswith('"'):
        token_str = token_str[1:-1]
    if token_str.startswith("'") and token_str.endswith("'"):
        token_str = token_str[1:-1]

    try:
        payload = decode_access_token(token_str)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail=f"Invalid or expired session token: {str(e)}"
        )

    sub_val = payload.get("sub")
    if not sub_val:
        raise HTTPException(status_code=401, detail="Invalid token subject.")

    user = db.query(User).filter(User.id == sub_val).first()
    if not user:
        raise HTTPException(status_code=404, detail="User account not found.")

    # IMMEDIATE SESSION REVOCATION IF SUSPENDED BY ADMIN
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended by an administrator."
        )

    return to_user_response(user)