# backend/billing-service/app/events/publisher.py
import os
import logging
from typing import Optional
from faststream.rabbit import RabbitBroker
from mng_shared import EventKeys

logger = logging.getLogger(__name__)

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://admin:supersecret@rabbitmq:5672/")
broker = RabbitBroker(RABBITMQ_URL)


async def publish_wallet_empty(
    user_id: str,
    location_id: str | None = None,
    balance: float = 0.0,
    currency: str = "IRT",
    grace_days: int = 3,
):
    """
    Notifies Node Service, Notification Service, and Telegram that a user's balance is empty/negative.
    """
    payload = {
        "user_id": str(user_id),
        "location_id": str(location_id) if location_id else None,
        "balance": float(balance),
        "currency": str(currency),
        "grace_days": int(grace_days),
    }

    await broker.publish(
        payload,
        queue=EventKeys.WALLET_EMPTY,
    )

    logger.info(
        "Published WALLET_EMPTY for User %s (Balance: %s %s, Grace: %sd)",
        user_id,
        balance,
        currency,
        grace_days,
    )


async def publish_grace_period_warning(
    user_id: str,
    grace_day: int,
    grace_days_total: int,
    days_remaining: int,
    currency: str = "IRT",
):
    """Publishes daily reminder during the grace period before configurations are permanently deleted."""
    payload = {
        "user_id": str(user_id),
        "grace_day": int(grace_day),
        "grace_days_total": int(grace_days_total),
        "days_remaining": int(days_remaining),
        "currency": str(currency),
    }
    await broker.publish(
        payload,
        queue=EventKeys.GRACE_PERIOD_WARNING,
    )
    logger.info(
        "Published GRACE_PERIOD_WARNING for User %s (Day %s/%s, %sd left)",
        user_id,
        grace_day,
        grace_days_total,
        days_remaining,
    )


async def publish_wallet_funded(
    user_id: str,
    location_id: str | None = None,
):
    """
    Re-enables a user's configuration for a region when credit is added.
    """
    payload = {
        "user_id": str(user_id),
        "location_id": str(location_id) if location_id else None,
    }

    await broker.publish(
        payload,
        queue=EventKeys.WALLET_FUNDED,
    )

    logger.info(
        "Published WALLET_FUNDED for User %s location=%s",
        user_id,
        location_id,
    )


async def publish_wallet_low(user_id: str, balance: float, threshold: float = 0.0, currency: str = "IRT"):
    """Notifies Notification Service when user balance drops below the low balance warning threshold."""
    payload = {
        "user_id": str(user_id),
        "balance": balance,
        "threshold": threshold,
        "currency": currency
    }
    await broker.publish(payload, queue=EventKeys.WALLET_LOW)
    logger.info(f"Published WALLET_LOW for User {user_id} (Balance: {balance:.2f} {currency}, Threshold: {threshold:.2f})")


async def publish_payment_submitted(
    user_id: str,
    tx_type: str,
    amount: float,
    reference_id: str,
    location_name: str | None = None
):
    """
    Notifies Admins via Notification Service when a client submits a new top-up 
    verification slip or submits a refund claim (status="Pending").
    """
    payload = {
        "user_id": str(user_id),
        "tx_type": tx_type,
        "status": "Pending",
        "amount": amount,
        "reference_id": reference_id,
        "location_name": location_name
    }

    await broker.publish(
        payload,
        queue=EventKeys.PAYMENT_PROCESSED,
    )

    logger.info(
        f"Published PAYMENT_PROCESSED (Pending {tx_type}) for User {user_id} "
        f"({amount:.2f} Toman, Ref: {reference_id})"
    )


async def publish_payment_processed(
    user_id: str,
    tx_type: str,
    status: str,
    amount: float,
    reference_id: Optional[str] = None,
    admin_notes: Optional[str] = None,
    currency: str = "IRT",
    action: Optional[str] = None,
    tracking_ref: Optional[str] = None,
):
    """Notifies Client when a top-up, refund, gift credit, or balance adjustment is processed by Admin."""
    payload = {
        "user_id": str(user_id),
        "tx_type": tx_type,
        "status": status,
        "amount": amount,
        "reference_id": reference_id or "",
        "admin_notes": admin_notes or "",
        "currency": currency,
        "action": action or ("Credit" if amount >= 0 else "Debit"),
        "tracking_ref": tracking_ref or "",
    }

    await broker.publish(
        payload,
        queue=EventKeys.PAYMENT_PROCESSED,
    )

    logger.info(
        f"Published PAYMENT_PROCESSED for User {user_id} "
        f"({tx_type} {status}: {amount:.2f} {currency}, Ref: {reference_id})"
    )


async def publish_config_reservation_requested(
    user_id: str,
    location_id: str,
    topup_transaction_id: str,
    ttl_minutes: int,
):
    """Requests Node Service to reserve an inbound capacity slot during top-up verification."""
    payload = {
        "user_id": str(user_id),
        "location_id": str(location_id),
        "topup_transaction_id": str(topup_transaction_id),
        "ttl_minutes": int(ttl_minutes),
    }

    await broker.publish(
        payload,
        queue=EventKeys.CONFIG_RESERVATION_REQUESTED,
    )


async def publish_config_reservation_release_requested(
    reservation_id: str,
    user_id: str,
    location_id: str,
    topup_transaction_id: str,
):
    """Requests Node Service to release a capacity reservation hold upon top-up rejection."""
    payload = {
        "reservation_id": str(reservation_id),
        "user_id": str(user_id),
        "location_id": str(location_id),
        "topup_transaction_id": str(topup_transaction_id),
    }

    await broker.publish(
        payload,
        queue=EventKeys.CONFIG_RESERVATION_RELEASE_REQUESTED,
    )


async def publish_config_reservation_consume_requested(
    reservation_id: str,
    user_id: str,
    location_id: str,
    topup_transaction_id: str,
):
    """Requests Node Service to consume reservation and provision the client on 3x-ui upon approval."""
    payload = {
        "reservation_id": str(reservation_id),
        "user_id": str(user_id),
        "location_id": str(location_id),
        "topup_transaction_id": str(topup_transaction_id),
    }

    await broker.publish(
        payload,
        queue=EventKeys.CONFIG_RESERVATION_CONSUME_REQUESTED,
    )