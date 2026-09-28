# backend/iam-service/app/events/publisher.py
import os
import uuid
import logging
from typing import Optional, Union
from faststream.rabbit import RabbitBroker
from vpn_shared import EventKeys

logger = logging.getLogger(__name__)

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://admin:supersecret@rabbitmq-dev:5672/")
broker = RabbitBroker(RABBITMQ_URL)


async def publish_user_created_event(
    user_id: Union[uuid.UUID, str], 
    username: str, 
    email: str, 
    role: str,
    parent_id: Optional[Union[uuid.UUID, str]] = None,
    gift_balance: float = 0.0,
    billing_currency: str = "IRT"
):
    """
    Emits USER_CREATED event to RabbitMQ for billing-service & node-service.
    Passes immutable billing_currency so Billing Service initializes the correct currency bucket.
    """
    payload = {
        "user_id": str(user_id),
        "username": username,
        "email": email,
        "role": str(role),
        "parent_id": str(parent_id) if parent_id else None,
        "gift_balance": float(gift_balance),
        "billing_currency": str(billing_currency).upper().strip()
    }
    try:
        # Publish to the EXCHANGE, not a direct queue
        await broker.publish(payload, exchange=EventKeys.USER_CREATED_EXCHANGE)
        logger.info(f"Published USER_CREATED event to Fanout Exchange for User ID {user_id}")
    except Exception as e:
        logger.error(f"Failed to publish USER_CREATED event: {e}")


async def publish_connection_processed_event(
    client_id: Union[uuid.UUID, str],
    seller_id: Union[uuid.UUID, str],
    seller_username: str,
    status: str
):
    """
    Emits CONNECTION_PROCESSED event to RabbitMQ for notification-service
    when a reseller accepts or rejects a client's connection request.
    """
    payload = {
        "client_id": str(client_id),
        "seller_id": str(seller_id),
        "seller_username": seller_username,
        "status": status
    }
    try:
        await broker.publish(payload, queue=EventKeys.CONNECTION_PROCESSED)
        logger.info(
            f"Published CONNECTION_PROCESSED event for Client {client_id} "
            f"by Reseller @{seller_username} (Status: {status})"
        )
    except Exception as e:
        logger.error(f"Failed to publish CONNECTION_PROCESSED event: {e}")


async def publish_user_telegram_linked_event(
    user_id: Union[uuid.UUID, str],
    username: str,
    telegram_id: str
):
    """Emits USER_TELEGRAM_LINKED event when a Telegram account is bound to a user."""
    payload = {
        "user_id": str(user_id),
        "username": username,
        "telegram_id": str(telegram_id)
    }
    try:
        await broker.publish(payload, queue=EventKeys.USER_TELEGRAM_LINKED)
        logger.info(f"Published USER_TELEGRAM_LINKED for User {user_id} (TG: {telegram_id})")
    except Exception as e:
        logger.error(f"Failed to publish USER_TELEGRAM_LINKED event: {e}")


async def publish_user_telegram_unlinked_event(
    user_id: Union[uuid.UUID, str],
    telegram_id: str
):
    """Emits USER_TELEGRAM_UNLINKED event when a user disconnects their Telegram account."""
    payload = {
        "user_id": str(user_id),
        "telegram_id": str(telegram_id)
    }
    try:
        await broker.publish(payload, queue=EventKeys.USER_TELEGRAM_UNLINKED)
        logger.info(f"Published USER_TELEGRAM_UNLINKED for User {user_id} (TG: {telegram_id})")
    except Exception as e:
        logger.error(f"Failed to publish USER_TELEGRAM_UNLINKED event: {e}")


async def publish_password_changed_event(user_id: Union[uuid.UUID, str]):
    """Emits USER_PASSWORD_CHANGED security event when password is reset or updated."""
    payload = {"user_id": str(user_id)}
    try:
        await broker.publish(payload, queue=EventKeys.USER_PASSWORD_CHANGED)
        logger.info(f"Published USER_PASSWORD_CHANGED security event for User {user_id}")
    except Exception as e:
        logger.error(f"Failed to publish USER_PASSWORD_CHANGED event: {e}")


async def publish_recovery_keys_regenerated_event(user_id: Union[uuid.UUID, str]):
    """Emits USER_RECOVERY_KEYS_REGENERATED security event when recovery keys are regenerated."""
    payload = {"user_id": str(user_id)}
    try:
        await broker.publish(payload, queue=EventKeys.USER_RECOVERY_KEYS_REGENERATED)
        logger.info(f"Published USER_RECOVERY_KEYS_REGENERATED security event for User {user_id}")
    except Exception as e:
        logger.error(f"Failed to publish USER_RECOVERY_KEYS_REGENERATED event: {e}")


async def publish_user_status_changed_event(user_id: Union[uuid.UUID, str], is_active: bool):
    """
    Publishes user suspension or reactivation event to node-service (for 3X-UI configs)
    and notification-service (for in-app inbox and Telegram push delivery).
    """
    event_queue = EventKeys.USER_ACTIVATED if is_active else EventKeys.USER_SUSPENDED
    notif_queue = "notification.events.user_activated" if is_active else "notification.events.user_suspended"
    payload = {"user_id": str(user_id), "is_active": is_active}
    try:
        # Deliver to node-service to provision or purge 3X-UI configs
        await broker.publish(payload, queue=event_queue)
        # Deliver to notification-service to record alert and dispatch Telegram message
        await broker.publish(payload, queue=notif_queue)
        logger.info(f"Published user status event for User {user_id} (is_active={is_active})")
    except Exception as e:
        logger.error(f"Failed to publish user status change event: {e}")