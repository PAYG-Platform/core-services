# backend/iam-service/app/core/security.py
import os
import string
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
import jwt
import bcrypt

logger = logging.getLogger(__name__)

import sys

ALGORITHM = "RS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 Days


def _find_key_path(env_var_name: str, filename: str) -> Optional[str]:
    """Inspects environment variables and common container/host mount locations for the key file."""
    explicit_path = os.getenv(env_var_name, "").strip()
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path

    # Standard container and host candidate search paths
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    repo_root = os.path.dirname(base_dir)

    search_candidates = [
        f"/app/keys/{filename}",
        f"/keys/{filename}",
        os.path.join(base_dir, "keys", filename),
        os.path.join(repo_root, "keys", filename),
        os.path.join(os.getcwd(), "keys", filename),
        os.path.join(os.getcwd(), filename),
    ]

    for candidate in search_candidates:
        if os.path.isfile(candidate):
            return candidate

    return None


def _print_fatal_iam_key_error(key_type: str):
    """Outputs a prominent ANSI Red diagnostic error and halts the process."""
    filename = f"jwt_{key_type.lower()}.pem"
    env_var = f"JWT_{key_type.upper()}_PATH"
    configured_path = os.getenv(env_var, "").strip() or "NOT SPECIFIED IN .ENV"

    error_banner = (
        "\n\033[91m\033[1m"
        "================================================================================\n"
        f"[FATAL ERROR] MISSING RSA {key_type.upper()} FILE (IAM SERVICE)\n"
        "================================================================================\033[0m\033[91m\n"
        "• WHAT IS MISSING:\n"
        f"  The RSA {key_type} file could not be found.\n"
        f"  - Configured Path in {env_var}: '{configured_path}'\n"
        f"  - Container Search Paths: /app/keys/{filename}, /keys/{filename}\n\n"
        "• WHY THIS ERROR OCCURRED:\n"
        "  Hardcoded fallback keys have been disabled. Both development and production\n"
        "  environments strictly require genuine OpenSSL-generated RSA key files.\n"
        "  As the central Identity Authority, IAM must sign tokens with your real private key.\n\n"
        "• HOW TO RESOLVE IN 3 EASY STEPS:\n"
        "  1. In your project root on your host machine, create and generate the keys:\n"
        "       mkdir -p keys\n"
        "       openssl genrsa -out keys/jwt_private.pem 2048\n"
        "       openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem\n\n"
        "  2. In your docker-compose file (e.g. docker-compose.all.yml), mount the keys folder:\n"
        "       volumes:\n"
        "         - ./keys:/app/keys:ro\n\n"
        "  3. In your .env file, specify:\n"
        "       JWT_PRIVATE_KEY_PATH=/keys/jwt_private.pem\n"
        "       JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem\n"
        "================================================================================\033[0m\n"
    )
    print(error_banner, file=sys.stderr, flush=True)
    os._exit(1)


def get_jwt_private_key() -> str:
    """Loads RSA Private Key. Strictly aborts in all environments if missing."""
    raw_key = os.getenv("JWT_PRIVATE_KEY", "").strip()
    if raw_key and "-----BEGIN" in raw_key:
        return raw_key.replace("\\n", "\n").strip()

    resolved_path = _find_key_path("JWT_PRIVATE_KEY_PATH", "jwt_private.pem")
    if resolved_path:
        with open(resolved_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if "-----BEGIN" in content:
                return content

    _print_fatal_iam_key_error("PRIVATE_KEY")
    return ""


def get_jwt_public_key() -> str:
    """Loads RSA Public Key. Strictly aborts in all environments if missing."""
    raw_key = os.getenv("JWT_PUBLIC_KEY", "").strip()
    if raw_key and "-----BEGIN" in raw_key:
        return raw_key.replace("\\n", "\n").strip()

    resolved_path = _find_key_path("JWT_PUBLIC_KEY_PATH", "jwt_public.pem")
    if resolved_path:
        with open(resolved_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if "-----BEGIN" in content:
                return content

    _print_fatal_iam_key_error("PUBLIC_KEY")
    return ""


def hash_password(password: str) -> str:
    """Hashes a plain text password using bcrypt with a generated salt."""
    pwd_bytes = password.encode('utf-8')[:72]  # Truncate to 72 bytes per bcrypt spec
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain text password against a stored bcrypt hash."""
    pwd_bytes = plain_password.encode('utf-8')[:72]
    hashed_bytes = hashed_password.encode('utf-8')
    try:
        return bcrypt.checkpw(pwd_bytes, hashed_bytes)
    except Exception as e:
        logger.warning(f"Password verification check failed: {e}")
        return False


def generate_recovery_keys(count: int = 12, length: int = 12) -> List[str]:
    """Generates cryptographic recovery keys (uppercase ASCII + digits A-Z, 0-9)."""
    alphabet = string.ascii_uppercase + string.digits
    return [''.join(secrets.choice(alphabet) for _ in range(length)) for _ in range(count)]


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Signs a new JWT access token using the RS256 Private Key."""
    private_key = get_jwt_private_key()
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({
        "iat": now,
        "exp": expire
    })
    return jwt.encode(to_encode, private_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Verifies JWT signature using the RS256 Public Key and preserves UUID claims.
    Applies clock skew leeway of 60 seconds.
    """
    public_key = get_jwt_public_key()
    try:
        payload = jwt.decode(token, public_key, algorithms=[ALGORITHM], leeway=60)
        
        if "sub" in payload:
            payload["sub"] = str(payload["sub"])
            
        if "parent_id" in payload and payload["parent_id"] is not None:
            payload["parent_id"] = str(payload["parent_id"])
            
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("JWT Token expired.")
        raise
    except jwt.PyJWTError as e:
        logger.warning(f"JWT Token decoding failed: {e}")
        raise