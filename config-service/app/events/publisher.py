# backend/config-service/app/events/publisher.py
import os
import logging
from typing import Optional, List
from faststream.rabbit import RabbitBroker
from vpn_shared import EventKeys

logger = logging.getLogger(__name__)

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://admin:supersecret@rabbitmq-dev:5672/")
broker = RabbitBroker(RABBITMQ_URL)


async def publish_system_broadcast_event(
    title: str,
    message: str,
    user_ids: Optional[List[str]] = None
):
    """
    Publishes a SYSTEM_BROADCAST event to RabbitMQ.
    Consumed by notification-service to record in-app notifications and trigger Telegram alerts.
    """
    payload = {
        "title": title,
        "message": message,
        "user_ids": user_ids or []
    }
    try:
        await broker.publish(payload, queue=EventKeys.SYSTEM_BROADCAST)
        logger.info(f"Published SYSTEM_BROADCAST event via RabbitMQ: '{title}'")
    except Exception as e:
        logger.error(f"Failed to publish SYSTEM_BROADCAST event: {e}")


async def publish_sync_interval_changed_event(interval_minutes: int):
    """Publishes CONFIG_SYNC_INTERVAL_CHANGED event to RabbitMQ for node-service."""
    payload = {"billing_sync_interval": int(interval_minutes)}
    try:
        await broker.publish(payload, queue=EventKeys.CONFIG_SYNC_INTERVAL_CHANGED)
        logger.info(f"Published CONFIG_SYNC_INTERVAL_CHANGED event: {interval_minutes}m")
    except Exception as e:
        logger.error(f"Failed to publish CONFIG_SYNC_INTERVAL_CHANGED event: {e}")