# backend/notification-service/app/events/publisher.py
import os
import logging
from typing import Optional
from faststream.rabbit import RabbitBroker
from mng_shared import EventKeys

logger = logging.getLogger(__name__)

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://admin:supersecret@rabbitmq-dev:5672/")
broker = RabbitBroker(RABBITMQ_URL)


async def publish_notification_created(user_id: str, notification_id: str, title: str, message: Optional[str] = None):
    """Publishes NOTIFICATION_CREATED event for external push workers (Telegram / Email)."""
    payload = {
        "user_id": str(user_id),
        "notification_id": str(notification_id),
        "title": title,
        "message": message or ""
    }
    try:
        await broker.publish(payload, queue=EventKeys.NOTIFICATION_CREATED)
        logger.info(f"Published NOTIFICATION_CREATED for User {user_id}")
    except Exception as e:
        logger.error(f"Failed to publish NOTIFICATION_CREATED event: {e}")