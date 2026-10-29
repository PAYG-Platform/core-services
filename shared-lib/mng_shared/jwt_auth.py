import os
import sys
import logging
import jwt
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, status

logger = logging.getLogger(__name__)

COOKIE_NAME = "access_token"
ALGORITHM = "RS256"


def _find_public_key_path() -> Optional[str]:
    """Inspects environment variables and common container/host mount locations for the public key."""
    explicit_path = os.getenv("JWT_PUBLIC_KEY_PATH", "").strip()
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path

    search_candidates = [
        "/app/keys/jwt_public.pem",
        "/keys/jwt_public.pem",
        os.path.join(os.getcwd(), "keys", "jwt_public.pem"),
        os.path.join(os.getcwd(), "jwt_public.pem"),
    ]

    for candidate in search_candidates:
        if os.path.isfile(candidate):
            return candidate

    return None


def _print_fatal_public_key_error():
    """Outputs a prominent ANSI Red diagnostic error and halts the process."""
    configured_path = os.getenv("JWT_PUBLIC_KEY_PATH", "").strip() or "NOT SPECIFIED IN .ENV"

    error_banner = (
        "\n\033[91m\033[1m"
        "================================================================================\n"
        "[FATAL ERROR] MISSING RSA PUBLIC KEY (MICROSERVICE VERIFICATION)\n"
        "================================================================================\033[0m\033[91m\n"
        "• WHAT IS MISSING:\n"
        "  The RSA Public Key file (jwt_public.pem) could not be found.\n"
        f"  - Configured Path in JWT_PUBLIC_KEY_PATH: '{configured_path}'\n"
        "  - Checked Container Paths: /keys/jwt_public.pem, /keys/jwt_public.pem\n\n"
        "• WHY THIS ERROR OCCURRED:\n"
        "  Hardcoded fallback keys have been disabled. All microservices require\n"
        "  the authentic OpenSSL-generated RSA Public Key to verify JWT tokens.\n\n"
        "• HOW TO RESOLVE IN 3 EASY STEPS:\n"
        "  1. In your project root on your host machine, generate the keys:\n"
        "       mkdir -p keys\n"
        "       openssl genrsa -out keys/jwt_private.pem 2048\n"
        "       openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem\n\n"
        "  2. In your docker-compose file (e.g. docker-compose.all.yml), mount the keys folder:\n"
        "       volumes:\n"
        "         - ./keys:/app/keys:ro\n\n"
        "  3. In your .env file, specify:\n"
        "       JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem\n"
        "================================================================================\033[0m\n"
    )
    print(error_banner, file=sys.stderr, flush=True)
    os._exit(1)


def get_jwt_public_key() -> str:
    """Loads RSA Public Key. Strictly aborts in all environments if missing."""
    raw_key = os.getenv("JWT_PUBLIC_KEY", "").strip()
    if raw_key and "-----BEGIN" in raw_key:
        return raw_key.replace("\\n", "\n").strip()

    resolved_path = _find_public_key_path()
    if resolved_path:
        with open(resolved_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if "-----BEGIN" in content:
                return content

    _print_fatal_public_key_error()
    return ""


def parse_bearer_header(auth_header: Optional[str]) -> Optional[str]:
    """
    Extracts the raw JWT string from an 'Authorization: Bearer <token>' header value or cookie string.
    Safely strips literal surrounding quotes if the browser/client injected them.
    """
    if not auth_header:
        return None

    # Remove whitespace and literal quotes
    auth_header = auth_header.strip().strip('"').strip("'")

    if auth_header.startswith("Bearer "):
        auth_header = auth_header.replace("Bearer ", "", 1).strip().strip('"').strip("'")

    return auth_header


def extract_token_from_request(request: Request, cookie_name: str = COOKIE_NAME) -> Optional[str]:
    """
    Extracts a raw JWT token from either the 'Authorization' HTTP header or an HTTP-Only Cookie.
    """
    # 1. Try Authorization Header
    auth_header = request.headers.get("Authorization")
    if auth_header:
        token = parse_bearer_header(auth_header)
        if token:
            return token

    # 2. Fallback to HTTP-Only Cookie
    cookie_token = request.cookies.get(cookie_name)
    if cookie_token:
        return parse_bearer_header(cookie_token)

    return None


def verify_jwt_token(
    token: str, 
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Statelessly verifies a JWT token signature using the RS256 Public Key."""
    verification_key = public_key or secret_key or get_jwt_public_key()

    raw_token = parse_bearer_header(token)
    if not raw_token:
        return None

    # Extra safety for literal quotes
    if raw_token.startswith('"') and raw_token.endswith('"'):
        raw_token = raw_token[1:-1]

    try:
        # RS256 signature verification with 60s leeway for clock skew
        payload = jwt.decode(raw_token, verification_key, algorithms=[ALGORITHM], leeway=60)
        
        # Ensure sub (user_id UUID) is preserved as a string
        if "sub" in payload:
            payload["sub"] = str(payload["sub"])

        # Ensure parent_id (seller_id UUID) is preserved as a string if present
        if "parent_id" in payload and payload["parent_id"] is not None:
            payload["parent_id"] = str(payload["parent_id"])

        # Ensure billing_currency claim is preserved across all microservices
        if "billing_currency" in payload and payload["billing_currency"] is not None:
            payload["billing_currency"] = str(payload["billing_currency"]).upper().strip()
        elif "currency" in payload and payload["currency"] is not None:
            payload["billing_currency"] = str(payload["currency"]).upper().strip()
        else:
            payload["billing_currency"] = "IRT"

        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("JWT Verification Failed: Token signature has expired.")
        return None
    except Exception as e:
        logger.warning(f"JWT Verification Failed: {str(e)}")
        return None


def get_current_user_payload(
    request: Request, 
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    FastAPI Dependency Helper.
    Usage in any microservice endpoint:
        @router.get("/my-endpoint")
        def my_route(user: dict = Depends(get_current_user_payload)):
            user_id = user["sub"]
            role = user["role"]
            parent_id = user.get("parent_id")
    """
    token = extract_token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token is missing.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    verification_key = public_key or secret_key or get_jwt_public_key()
    payload = verify_jwt_token(token, public_key=verification_key)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload