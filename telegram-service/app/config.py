# telegram-service/app/config.py
import os
import sys
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
API_ID = int(os.getenv("TELEGRAM_API_ID", 0))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://api-gateway:80").rstrip("/")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://notification-service:8000").rstrip("/")
IAM_SERVICE_URL = os.getenv("IAM_SERVICE_URL", f"{API_GATEWAY_URL}/api/iam").rstrip("/")

FALLBACK_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FALLBACK_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "PAYG_MNG_Bot").strip()

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0").strip()
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://admin:super_secret_rmq_pass@rabbitmq:5672/").strip()

JWT_PRIVATE_KEY_PATH = os.getenv("JWT_PRIVATE_KEY_PATH", "/keys/jwt_private.pem").strip()
JWT_PUBLIC_KEY_PATH = os.getenv("JWT_PUBLIC_KEY_PATH", "/keys/jwt_public.pem").strip()

# Outbound Proxy Configuration
PROXY_HOST = os.getenv("PROXY_HOST")
PROXY_PORT = os.getenv("PROXY_PORT")


async def fetch_bot_credentials() -> tuple[str, str]:
    """
    Dynamically fetches the latest Bot Token and Username configured
    by the admin in Notification Service / DB. Falls back to .env if unreachable.
    """
    url = f"{NOTIFICATION_SERVICE_URL}/notifications/settings/telegram-bot"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("bot_token") or FALLBACK_BOT_TOKEN
                username = data.get("bot_username") or FALLBACK_BOT_USERNAME
                if token:
                    logger.info("Successfully fetched Bot token from Notification Service.")
                    return token.strip(), username.strip().lstrip("@")
    except Exception as e:
        logger.warning(f"Could not reach Admin Notification Settings ({e}). Using .env fallback.")

    return FALLBACK_BOT_TOKEN, FALLBACK_BOT_USERNAME.lstrip("@")