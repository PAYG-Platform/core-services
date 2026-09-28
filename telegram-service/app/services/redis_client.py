# telegram-service/app/services/redis_client.py
import json
import logging
from typing import Optional, Any, Dict
import redis.asyncio as redis
from app.config import REDIS_URL

logger = logging.getLogger(__name__)

redis_pool = redis.ConnectionPool.from_url(
    REDIS_URL,
    decode_responses=True,
    health_check_interval=30,
    socket_connect_timeout=10.0,
    socket_timeout=10.0,
    retry_on_timeout=True,
)
redis_client = redis.Redis(connection_pool=redis_pool)


class RedisFSM:
    """
    Distributed Finite State Machine and Identity Cache stored in Redis.
    """

    @staticmethod
    async def set_user_mapping(
        telegram_id: str, 
        user_id: str, 
        username: Optional[str] = None,
        language: Optional[str] = None,
        currency: Optional[str] = None
    ):
        """Maps telegram_id <-> user_id bidirectionally and caches username, language, and billing currency."""
        await redis_client.set(f"telegram:user:{telegram_id}", user_id)
        await redis_client.set(f"user:telegram:{user_id}", telegram_id)
        if username:
            await redis_client.set(f"telegram:username:{telegram_id}", username)
        if language:
            await redis_client.set(f"telegram:language:{telegram_id}", language.strip().lower())
        if currency:
            await redis_client.set(f"telegram:currency:{telegram_id}", currency.strip().upper())

    @staticmethod
    async def set_currency(telegram_id: str, currency: str):
        await redis_client.set(f"telegram:currency:{telegram_id}", currency.strip().upper())

    @staticmethod
    async def get_currency(telegram_id: str) -> str:
        curr = await redis_client.get(f"telegram:currency:{telegram_id}")
        return curr.strip().upper() if curr else "IRT"

    @staticmethod
    async def get_user_id(telegram_id: str) -> Optional[str]:
        return await redis_client.get(f"telegram:user:{telegram_id}")

    @staticmethod
    async def get_telegram_id(user_id: str) -> Optional[str]:
        return await redis_client.get(f"user:telegram:{user_id}")

    @staticmethod
    async def get_username(telegram_id: str) -> Optional[str]:
        return await redis_client.get(f"telegram:username:{telegram_id}")

    @staticmethod
    async def set_language(telegram_id: str, language: str):
        await redis_client.set(f"telegram:language:{telegram_id}", language.strip().lower())

    @staticmethod
    async def get_language(telegram_id: str) -> str:
        lang = await redis_client.get(f"telegram:language:{telegram_id}")
        return lang.strip().lower() if lang else "en"

    @staticmethod
    async def clear_user_mapping(telegram_id: str):
        user_id = await redis_client.get(f"telegram:user:{telegram_id}")
        if user_id:
            await redis_client.delete(f"user:telegram:{user_id}")
        await redis_client.delete(f"telegram:user:{telegram_id}")
        await redis_client.delete(f"telegram:username:{telegram_id}")
        await redis_client.delete(f"telegram:language:{telegram_id}")

    @staticmethod
    async def set_state(telegram_id: str, state: str, data: Optional[Dict[str, Any]] = None, ttl: int = 900):
        """Sets FSM state with a 15-minute default timeout."""
        state_key = f"fsm:state:{telegram_id}"
        data_key = f"fsm:data:{telegram_id}"
        await redis_client.set(state_key, state, ex=ttl)
        if data is not None:
            await redis_client.set(data_key, json.dumps(data), ex=ttl)

    @staticmethod
    async def get_state(telegram_id: str) -> Optional[str]:
        return await redis_client.get(f"fsm:state:{telegram_id}")

    @staticmethod
    async def get_data(telegram_id: str) -> Dict[str, Any]:
        raw = await redis_client.get(f"fsm:data:{telegram_id}")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    @staticmethod
    async def update_data(telegram_id: str, updates: Dict[str, Any], ttl: int = 900):
        data = await RedisFSM.get_data(telegram_id)
        data.update(updates)
        await redis_client.set(f"fsm:data:{telegram_id}", json.dumps(data), ex=ttl)

    @staticmethod
    async def clear_state(telegram_id: str):
        await redis_client.delete(f"fsm:state:{telegram_id}")
        await redis_client.delete(f"fsm:data:{telegram_id}")