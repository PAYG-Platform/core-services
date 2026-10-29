# telegram-service/app/services/api_client.py
import os
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
import jwt
import httpx

from app.config import (
    API_GATEWAY_URL,
    JWT_PRIVATE_KEY_PATH,
    JWT_PUBLIC_KEY_PATH,
    ENVIRONMENT
)

logger = logging.getLogger(__name__)


def _load_private_key() -> str:
    """
    Attempts to locate and read the RSA Private Key (PEM) from
    configured paths or standard volume mount points.
    """
    candidates = [
        JWT_PRIVATE_KEY_PATH,
        "/keys/jwt_private.pem",
        "/app/keys/jwt_private.pem",
        os.path.join(os.getcwd(), "keys", "jwt_private.pem"),
        os.path.join(os.getcwd(), "jwt_private.pem")
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if "-----BEGIN" in content:
                        return content
            except Exception as e:
                logger.warning(f"Failed reading private key at {path}: {e}")
    return ""


def create_delegated_token(user_id: str, role: str = "Client", username: str = "") -> str:
    """
    Mints a short-lived (60s) JWT token with sub=user_id.
    Uses RS256 with the private RSA key if available, otherwise falls back
    to HS256 for local development environments.
    """
    private_key = _load_private_key()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username or f"tg_{str(user_id)[:8]}",
        "role": role,
        "iat": now,
        "exp": now + timedelta(seconds=60)
    }

    if private_key:
        try:
            return jwt.encode(payload, private_key, algorithm="RS256")
        except Exception as err:
            logger.error(f"Error encoding RS256 JWT: {err}. Falling back to HS256.")

    # Fallback to HS256 for local testing/development environments
    return jwt.encode(payload, "super_secret_jwt_key_change_in_production", algorithm="HS256")


class BackendAPIClient:
    """
    Asynchronous client for microservice operations through the centralized API Gateway.
    All calls are signed with an authenticated user JWT token.
    """

    def __init__(self, user_id: str, username: str = ""):
        self.user_id = str(user_id)
        self.username = username
        self.base_url = API_GATEWAY_URL.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        token = create_delegated_token(self.user_id, username=self.username)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

    # =========================================================================
    # 0. IAM & USER PROFILE
    # =========================================================================

    async def get_user_profile(self) -> Optional[Dict[str, Any]]:
        """Queries IAM service to get the user's latest profile, role, and language preference."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/iam/users/me",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch user profile for {self.user_id}: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching user profile from IAM: {e}")
            return None

    async def update_user_language(self, language_code: str) -> bool:
        """Updates user's preferred language in IAM, which syncs across both web dashboard and bot."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.patch(
                    f"{self.base_url}/api/iam/users/me/language",
                    params={"language": language_code.strip().lower()},
                    headers=self._headers()
                )
                if res.status_code == 200:
                    logger.info(f"Updated language to '{language_code}' for user {self.user_id} in IAM.")
                    return True
                logger.warning(f"Failed to update language in IAM for user {self.user_id}: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error updating language in IAM: {e}")
            return False

    # =========================================================================
    # 1. BILLING, BALANCE & DEPOSIT ENDPOINTS
    # =========================================================================

    async def get_balance(self) -> Optional[Dict[str, Any]]:
        """Queries current wallet credit, paid balance, gift balance, and minimum top-up."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/billing/topups/balance",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch balance for user {self.user_id}: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching balance from API gateway: {e}")
            return None

    async def get_admin_financial_settings(self) -> Optional[Dict[str, Any]]:
        """Queries platform payment accounts (Shetab bank cards, USDT addresses) and deposit instructions."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/billing/settings/admin-financials",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch financial settings: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching admin financial settings: {e}")
            return None

    async def submit_fiat_topup(self, amount: float, receipt_b64: str, description: Optional[str] = None) -> Dict[str, Any]:
        """Submits a card-to-card transfer receipt, amount, and optional note for administrative verification."""
        payload = {
            "amount": float(amount),
            "receipt_url": receipt_b64,
            "payment_method": "Bank Card Transfer",
            "description": description or "Wallet top-up via Telegram Bot"
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                res = await client.post(
                    f"{self.base_url}/api/billing/topups/fiat",
                    headers=self._headers(),
                    json=payload
                )
                if res.status_code in (200, 201):
                    return {"success": True, "data": res.json()}
                detail = res.json().get("detail", "Failed to submit top-up.") if res.content else f"HTTP {res.status_code}"
                return {"success": False, "detail": detail}
            except Exception as e:
                logger.error(f"Error submitting fiat top-up: {e}")
                return {"success": False, "detail": str(e)}

    async def get_refund_balance(self) -> Optional[Dict[str, Any]]:
        """Queries user's refundable deposit balance and AML 30-day cooldown status."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/billing/refunds/balances",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch refund balance for user {self.user_id}: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching refund balance: {e}")
            return None

    async def submit_refund_request(
        self,
        amount: float,
        card_number: str,
        cardholder_name: Optional[str] = None,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Submits a payout refund claim with destination Shetab card number, cardholder name, and optional memo."""
        dest_account = card_number.strip()
        if cardholder_name and cardholder_name.strip():
            dest_account = f"{dest_account} ({cardholder_name.strip()})"

        payload = {
            "amount": float(amount),
            "destination_account": dest_account,
            "reason": reason.strip() if reason and reason.strip() else "Requested via Telegram Bot"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.post(
                    f"{self.base_url}/api/billing/refunds/request",
                    headers=self._headers(),
                    json=payload
                )
                if res.status_code in (200, 201):
                    return {"success": True, "data": res.json()}
                detail = res.json().get("detail", "Refund request failed.") if res.content else f"HTTP {res.status_code}"
                return {"success": False, "detail": detail}
            except Exception as e:
                logger.error(f"Error submitting refund request: {e}")
                return {"success": False, "detail": str(e)}

    # =========================================================================
    # 2. PAYMENT & TRANSACTION HISTORIES
    # =========================================================================

    async def get_topup_history(self, limit: int = 15) -> Optional[List[Dict[str, Any]]]:
        """Retrieves user's recent top-up transactions."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/billing/topups/my",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    data = res.json()
                    return data[:limit] if isinstance(data, list) else []
                logger.warning(f"Failed to fetch top-up history: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching top-up history: {e}")
            return None

    async def get_refund_history(self, limit: int = 15) -> Optional[List[Dict[str, Any]]]:
        """Retrieves user's recent refund requests and approvals."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/billing/refunds/my",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    data = res.json()
                    return data[:limit] if isinstance(data, list) else []
                logger.warning(f"Failed to fetch refund history: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching refund history: {e}")
            return None

    # =========================================================================
    # 3. TRAFFIC & TELEMETRY ANALYTICS (DIAGRAMS)
    # =========================================================================

    async def get_client_daily_traffic(self, days: int = 30) -> Optional[List[Dict[str, Any]]]:
        """Queries day-by-day traffic timeseries for the past N days."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/traffic/traffic/client/daily?days={days}",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch daily traffic telemetry: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching daily traffic telemetry: {e}")
            return None

    async def get_client_provider_stats(self) -> Optional[Dict[str, Any]]:
        """Queries client bandwidth consumption segmented by carrier/ISP (MCI, Irancell, etc.)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/traffic/traffic/client/provider-stats",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch provider stats: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching provider stats: {e}")
            return None

    # =========================================================================
    # 4. DAILY FINANCIAL TRENDS (TOP-UPS & GIFTS)
    # =========================================================================

    async def get_client_financial_trends(self, days: int = 30) -> Optional[List[Dict[str, Any]]]:
        """Queries daily approved top-up amounts and gift credits granted over the past N days."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/billing/analytics/client/daily-financial-trends?days={days}",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch financial trends: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching client financial trends: {e}")
            return None

    # =========================================================================
    # 5. NODE & Traffic Routing CONFIGURATIONS
    # =========================================================================

    async def get_my_configs(self) -> Optional[Dict[str, Any]]:
        """Fetches all active user node configurations, bandwidth usage, and subscription link."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/node/configs/me",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch user configs: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching user configs from node-service: {e}")
            return None

    async def check_replace_status(self, config_id: str) -> Dict[str, Any]:
        """Checks if a route can be automatically replaced or requires filing an emergency ticket."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/node/configs/{config_id}/replace-status",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed checking replace status for config {config_id}: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error checking route replacement status: {e}")
            return {"can_auto_replace": False, "message": "Could not inspect route replacement status."}

    async def replace_blocked_route(self, config_id: str, client_isp: str) -> Dict[str, Any]:
        """Executes route replacement on 3X-UI or triggers an automatic support ticket with ISP metadata."""
        payload = {"client_isp": client_isp}
        async with httpx.AsyncClient(timeout=18.0) as client:
            try:
                res = await client.post(
                    f"{self.base_url}/api/node/configs/{config_id}/replace-blocked",
                    headers=self._headers(),
                    json=payload
                )
                if res.status_code == 200:
                    return res.json()
                detail = res.json().get("detail", "Route replacement request failed.") if res.content else f"HTTP {res.status_code}"
                return {"success": False, "message": detail}
            except Exception as e:
                logger.error(f"Error replacing blocked route: {e}")
                return {"success": False, "message": str(e)}

    # =========================================================================
    # 6. SUPPORT TICKETS & CHAT
    # =========================================================================

    async def list_tickets(self, page: int = 1, limit: int = 10) -> Optional[Dict[str, Any]]:
        """Lists user's support ticket threads with pagination."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/support/tickets/?page={page}&limit={limit}",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to list support tickets: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error listing tickets from support-service: {e}")
            return None

    async def get_ticket_detail(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a support ticket thread with recent conversation messages."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.get(
                    f"{self.base_url}/api/support/tickets/{ticket_id}",
                    headers=self._headers()
                )
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Failed to fetch ticket {ticket_id}: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Error fetching ticket detail: {e}")
            return None

    async def create_ticket(
        self,
        subject: str,
        category: str,
        content: str,
        file_bytes: Optional[bytes] = None,
        file_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Creates a new support ticket with optional file or screenshot attachment."""
        data = {
            "subject": subject,
            "category": category,
            "content": content
        }
        files = None
        if file_bytes and file_name:
            files = {"file": (file_name, file_bytes, "image/jpeg")}

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                res = await client.post(
                    f"{self.base_url}/api/support/tickets/",
                    headers=self._headers(),
                    data=data,
                    files=files
                )
                if res.status_code in (200, 201):
                    return {"success": True, "data": res.json()}
                detail = res.json().get("detail", "Failed creating ticket.") if res.content else f"HTTP {res.status_code}"
                return {"success": False, "detail": detail}
            except Exception as e:
                logger.error(f"Error creating ticket: {e}")
                return {"success": False, "detail": str(e)}

    async def add_ticket_reply(
        self,
        ticket_id: str,
        content: str,
        file_bytes: Optional[bytes] = None,
        file_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Appends a message reply to an existing support ticket thread."""
        data = {"content": content}
        files = None
        if file_bytes and file_name:
            files = {"file": (file_name, file_bytes, "image/jpeg")}

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                res = await client.post(
                    f"{self.base_url}/api/support/tickets/{ticket_id}/messages",
                    headers=self._headers(),
                    data=data,
                    files=files
                )
                if res.status_code in (200, 201):
                    return {"success": True, "data": res.json()}
                detail = res.json().get("detail", "Failed sending reply.") if res.content else f"HTTP {res.status_code}"
                return {"success": False, "detail": detail}
            except Exception as e:
                logger.error(f"Error replying to ticket {ticket_id}: {e}")
                return {"success": False, "detail": str(e)}