# backend/node-service/app/events/publisher.py
import os
import logging
from typing import Optional, List
from faststream.rabbit import RabbitBroker, RabbitExchange, ExchangeType
from mng_shared import EventKeys

logger = logging.getLogger(__name__)

# Fallback configured for docker-compose development network
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://admin:supersecret@rabbitmq-dev:5672/")
broker = RabbitBroker(RABBITMQ_URL)

# Safe exchange fallback for multi-consumer broadcast (Billing + Traffic)
TRAFFIC_EXCHANGE = getattr(
    EventKeys,
    "TRAFFIC_CONSUMED_EXCHANGE",
    RabbitExchange("traffic_consumed_fanout", type=ExchangeType.FANOUT)
)


async def ensure_broker_connected():
    """Ensures broker connection is active before publishing."""
    try:
        if not getattr(broker, "_connection", None) or not getattr(broker._connection, "is_connected", False):
            logger.info("Broker not connected. Attempting on-demand connection...")
            await broker.connect()
    except Exception as e:
        logger.warning(f"Could not establish on-demand connection to RabbitMQ: {e}")


async def publish_traffic_consumed_event(payload: dict):
    """
    Publishes TRAFFIC_CONSUMED event exclusively to the Fanout Exchange.
    RabbitMQ automatically routes a single copy to both billing-service and traffic-service.
    """
    try:
        await ensure_broker_connected()
        # Publish to the Fanout Exchange (for billing-service & traffic-service queues)
        await broker.publish(payload, exchange=TRAFFIC_EXCHANGE)
        logger.info(
            f"Broadcasted TRAFFIC_CONSUMED event for User {payload.get('user_id')} "
            f"({payload.get('used_bytes')} bytes, IP: {payload.get('client_ip') or 'unknown'})"
        )
    except Exception as e:
        logger.error(f"Failed to publish TRAFFIC_CONSUMED event to RabbitMQ: {e}")


async def publish_server_changed_event(
    user_id: Optional[str] = None,
    server_name: str = "Traffic Routing Node",
    action: str = "updated",
    location_name: Optional[str] = None,
    message: Optional[str] = None,
    user_ids: Optional[List[str]] = None,
):
    """
    Publishes a SERVER_CHANGED event when a Traffic Routing server node or user configuration changes.
    """
    target_uids = [str(u) for u in user_ids] if user_ids else ([str(user_id)] if user_id else [])
    payload = {
        "user_id": str(user_id) if user_id else None,
        "user_ids": target_uids,
        "server_name": server_name,
        "action": action,
        "location_name": location_name or "Traffic Routing Region",
        "message": message
    }
    try:
        await ensure_broker_connected()
        await broker.publish(payload, queue=EventKeys.SERVER_CHANGED)
        logger.info(f"Published SERVER_CHANGED event for '{server_name}' (Action: {action}, Target Users: {len(target_uids)})")
    except Exception as e:
        logger.error(f"Failed to publish SERVER_CHANGED event: {e}")


async def publish_server_offline_event(
    server_id: str,
    server_name: str,
    api_url: str,
    error_message: Optional[str] = None,
    location_name: Optional[str] = None,
    affected_user_ids: Optional[List[str]] = None,
):
    """Publishes SERVER_OFFLINE event for admins (technical) and affected users (sanitized)."""
    payload = {
        "server_id": str(server_id),
        "server_name": server_name,
        "api_url": api_url,
        "error_message": error_message or "Connection failed or host unreachable.",
        "location_name": location_name or "Traffic Routing Region",
        "affected_user_ids": affected_user_ids or []
    }
    try:
        await ensure_broker_connected()
        await broker.publish(payload, queue=EventKeys.SERVER_OFFLINE)
        logger.warning(f"Published SERVER_OFFLINE for '{server_name}' ({len(payload['affected_user_ids'])} affected users)")
    except Exception as e:
        logger.error(f"Failed to publish SERVER_OFFLINE event: {e}")


async def publish_server_recovered_event(
    server_id: str,
    server_name: str,
    api_url: str,
    location_name: Optional[str] = None,
    affected_user_ids: Optional[List[str]] = None,
):
    """Publishes SERVER_RECOVERED event for admins (technical) and affected users (sanitized)."""
    payload = {
        "server_id": str(server_id),
        "server_name": server_name,
        "api_url": api_url,
        "location_name": location_name or "Traffic Routing Region",
        "affected_user_ids": affected_user_ids or []
    }
    try:
        await ensure_broker_connected()
        await broker.publish(payload, queue=EventKeys.SERVER_RECOVERED)
        logger.info(f"Published SERVER_RECOVERED for '{server_name}' ({len(payload['affected_user_ids'])} affected users)")
    except Exception as e:
        logger.error(f"Failed to publish SERVER_RECOVERED event: {e}")


async def publish_location_capacity_event(
    location_id: str, 
    location_name: str, 
    is_full: bool, 
    total_capacity: int, 
    used_capacity: int
):
    """
    Publishes LOCATION_CAPACITY_CHANGED event so billing-service replicates capacity locally.
    """
    payload = {
        "location_id": str(location_id),
        "location_name": location_name,
        "is_full": bool(is_full),
        "total_capacity": int(total_capacity),
        "used_capacity": int(used_capacity)
    }
    try:
        await ensure_broker_connected()
        await broker.publish(payload, queue=EventKeys.LOCATION_CAPACITY_CHANGED)
        logger.info(f"Published LOCATION_CAPACITY_CHANGED for location '{location_name}' (is_full: {is_full})")
    except Exception as e:
        logger.error(f"Failed to publish LOCATION_CAPACITY_CHANGED: {e}")


async def publish_location_capacity_exceeded(
    location_id: str,
    location_name: str,
    user_id: str,
    username: Optional[str] = None
):
    """
    Alerts Notification Service that a location has no server capacity for a user.
    """
    payload = {
        "location_id": str(location_id),
        "location_name": str(location_name),
        "user_id": str(user_id),
        "username": username or str(user_id)[:8]
    }
    try:
        await ensure_broker_connected()
        await broker.publish(payload, queue=EventKeys.LOCATION_CAPACITY_EXCEEDED)
        logger.warning(f"Published LOCATION_CAPACITY_EXCEEDED for region '{location_name}' (User: {username})")
    except Exception as e:
        logger.error(f"Failed to publish LOCATION_CAPACITY_EXCEEDED: {e}")


async def publish_config_reservation_created(
    reservation_id: str,
    user_id: str,
    location_id: str,
    server_id: str,
    inbound_id: int,
    protocol: str,
):
    payload = {
        "reservation_id": str(reservation_id),
        "user_id": str(user_id),
        "location_id": str(location_id),
        "server_id": str(server_id),
        "inbound_id": int(inbound_id),
        "protocol": str(protocol),
        "existing_config": False,
    }

    try:
        await ensure_broker_connected()
        await broker.publish(
            {
                "reservation_id": None,
                "user_id": str(user_id),
                "location_id": str(location_id),
                "server_id": None,
                "inbound_id": None,
                "protocol": None,
                "existing_config": True,
            },
            queue=EventKeys.CONFIG_RESERVATION_CREATED,
        )

        logger.info(
            "Published CONFIG_RESERVATION_CREATED "
            "reservation=%s user=%s location=%s server=%s inbound=%s protocol=%s",
            reservation_id,
            user_id,
            location_id,
            server_id,
            inbound_id,
            protocol,
        )
    except Exception as e:
        logger.error(
            "Failed to publish CONFIG_RESERVATION_CREATED: %s",
            e,
        )


async def publish_config_reservation_rejected(
    user_id: str,
    location_id: str,
    topup_transaction_id: str,
    reason: str,
):
    payload = {
        "user_id": str(user_id),
        "location_id": str(location_id),
        "topup_transaction_id": str(topup_transaction_id),
        "reason": str(reason),
    }

    try:
        await ensure_broker_connected()
        await broker.publish(
            payload,
            queue=EventKeys.CONFIG_RESERVATION_REJECTED,
        )

        logger.info(
            "Published CONFIG_RESERVATION_REJECTED "
            "user=%s location=%s transaction=%s",
            user_id,
            location_id,
            topup_transaction_id,
        )
    except Exception as e:
        logger.error(
            "Failed to publish CONFIG_RESERVATION_REJECTED: %s",
            e,
        )


async def publish_config_reservation_consumed(
    reservation_id: str,
    user_id: str,
    location_id: str,
    server_id: str,
    inbound_id: int,
    protocol: str,
    client_uuid: str,
):
    payload = {
        "reservation_id": str(reservation_id),
        "user_id": str(user_id),
        "location_id": str(location_id),
        "server_id": str(server_id),
        "inbound_id": int(inbound_id),
        "protocol": str(protocol),
        "client_uuid": str(client_uuid),
    }

    try:
        await ensure_broker_connected()
        await broker.publish(
            payload,
            queue=EventKeys.CONFIG_RESERVATION_CONSUMED,
        )

        logger.info(
            "Published CONFIG_RESERVATION_CONSUMED "
            "reservation=%s user=%s",
            reservation_id,
            user_id,
        )
    except Exception as e:
        logger.error(
            "Failed to publish CONFIG_RESERVATION_CONSUMED: %s",
            e,
        )


async def publish_config_reservation_released(
    reservation_id: str,
    user_id: str,
    location_id: str,
    reason: str,
):
    payload = {
        "reservation_id": str(reservation_id),
        "user_id": str(user_id),
        "location_id": str(location_id),
        "reason": str(reason),
    }

    try:
        await ensure_broker_connected()
        await broker.publish(
            payload,
            queue=EventKeys.CONFIG_RESERVATION_RELEASED,
        )

        logger.info(
            "Published CONFIG_RESERVATION_RELEASED "
            "reservation=%s user=%s location=%s",
            reservation_id,
            user_id,
            location_id,
        )
    except Exception as e:
        logger.error(
            "Failed to publish CONFIG_RESERVATION_RELEASED: %s",
            e,
        )


async def publish_server_replaced_event(
    user_ids: List[str],
    old_server_name: str,
    replacement_server_names: List[str],
    reason: str,
):
    """
    Publishes a SERVER_REPLACED event when a blocked server is replaced
    and its active users are distributed across one or more replacement servers.
    """
    payload = {
        "user_ids": user_ids,
        "old_server_name": old_server_name,
        "replacement_server_names": replacement_server_names,
        "reason": reason,
    }
    try:
        await ensure_broker_connected()
        await broker.publish(payload, queue=EventKeys.SERVER_REPLACED)
        logger.info(
            f"Published SERVER_REPLACED for {len(user_ids)} users from '{old_server_name}' "
            f"to {replacement_server_names}"
        )
    except Exception as e:
        logger.error(f"Failed to publish SERVER_REPLACED event: {e}")