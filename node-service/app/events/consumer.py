# backend/node-service/app/events/consumer.py
import uuid
import logging
from datetime import datetime, timezone, timedelta
from faststream.rabbit import RabbitRouter, RabbitExchange, ExchangeType
from sqlalchemy import func

from app.database import SessionLocal
from app.models import (
    Server,
    UserConfig,
    UserSubscription,
    ConfigReservation,
    Location,
    NodeSettings,
)
from app.services.reservations import (
    reserve_config_slot,
    expire_old_reservations,
    find_available_slot_for_region,
)
from app.services.provisioning import (
    provision_user_configs_for_all_locations,
    provision_missing_users_for_location,
    sync_location_capacity,
    build_config_name,
    set_sync_interval_minutes,
)
from app.events.publisher import broker, publish_location_capacity_event
from app.xui_client import XUIClient
from mng_shared.events import EventKeys

logger = logging.getLogger(__name__)
node_router = RabbitRouter()


@node_router.subscriber("node_user_created_queue", exchange=EventKeys.USER_CREATED_EXCHANGE)
async def handle_user_created(msg: dict):
    """
    Triggered when IAM service registers a new user.
    1. Replicates username and creates UserSubscription locally.
    2. Initializes has_credit based on initial gift credit.
    3. Provisions configurations named '{username}-{region}-PAYG'.
    4. If initial gift credit is 0, configurations are turned OFF (is_active=False, enable=False in 3x-ui).
    """
    raw_user_id = msg.get("user_id")
    username = msg.get("username")
    gift_balance = float(msg.get("gift_balance", 0.0))
    if not raw_user_id:
        return

    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError):
        return

    has_credit = gift_balance > 0
    db = SessionLocal()
    try:
        existing_subscription = (
            db.query(UserSubscription)
            .filter(UserSubscription.user_id == user_id)
            .first()
        )

        if not existing_subscription:
            sub_token = uuid.uuid4().hex + uuid.uuid4().hex
            subscription = UserSubscription(
                user_id=user_id,
                sub_token=sub_token,
                username=username,
                has_credit=has_credit
            )
            db.add(subscription)
            db.commit()
            logger.info(f"Initialized UserSubscription token for user {user_id} (has_credit={has_credit}).")
        else:
            if username and not existing_subscription.username:
                existing_subscription.username = username
            existing_subscription.has_credit = has_credit
            db.commit()

        # Configs are turned OFF (is_active=False) if initial credit == 0
        await provision_user_configs_for_all_locations(
            db, 
            user_id, 
            username=username, 
            is_active=has_credit
        )

    except Exception:
        db.rollback()
        logger.exception(f"Error handling USER_CREATED event for user {user_id}.")
    finally:
        db.close()


@node_router.subscriber(EventKeys.CONFIG_RESERVATION_REQUESTED)
async def handle_config_reservation_requested(msg: dict):
    """
    Atomically reserves one concrete Traffic Routing slot for a client's regional purchase.
    Enforces idempotency and the single-config-per-region rule.
    """
    raw_user_id = msg.get("user_id")
    raw_location_id = msg.get("location_id")
    raw_topup_id = msg.get("topup_transaction_id")

    if not raw_user_id or not raw_location_id or not raw_topup_id:
        logger.warning(f"CONFIG_RESERVATION_REQUESTED missing required fields: {msg}")
        return

    try:
        user_id = uuid.UUID(str(raw_user_id))
        location_id = uuid.UUID(str(raw_location_id))
        topup_transaction_id = uuid.UUID(str(raw_topup_id))
    except (ValueError, TypeError):
        logger.error(f"Invalid UUID in CONFIG_RESERVATION_REQUESTED: {msg}")
        return

    ttl_minutes = max(1, int(msg.get("ttl_minutes", 30)))

    db = SessionLocal()
    try:
        result = reserve_config_slot(
            db=db,
            user_id=user_id,
            location_id=location_id,
            topup_transaction_id=topup_transaction_id,
            ttl_minutes=ttl_minutes,
        )

        if result is None:
            db.rollback()
            await broker.publish(
                {
                    "user_id": str(user_id),
                    "location_id": str(location_id),
                    "topup_transaction_id": str(topup_transaction_id),
                    "reason": "No configuration capacity available in the requested region.",
                },
                queue=EventKeys.CONFIG_RESERVATION_REJECTED,
            )
            return

        if result["existing_config"]:
            db.commit()
            await broker.publish(
                {
                    "user_id": str(user_id),
                    "location_id": str(location_id),
                    "topup_transaction_id": str(topup_transaction_id),
                },
                queue=EventKeys.CONFIG_RESERVATION_NOT_REQUIRED,
            )
            return

        reservation = result["reservation"]
        db.commit()
        db.refresh(reservation)

        await broker.publish(
            {
                "reservation_id": str(reservation.id),
                "user_id": str(user_id),
                "location_id": str(location_id),
                "topup_transaction_id": str(topup_transaction_id),
                "server_id": str(reservation.server_id),
                "inbound_id": int(reservation.inbound_id),
                "protocol": reservation.protocol,
                "expires_at": reservation.expires_at.isoformat(),
            },
            queue=EventKeys.CONFIG_RESERVATION_RESERVED,
        )

        await sync_location_capacity(db, location_id)

    except Exception:
        db.rollback()
        logger.exception(f"Error handling CONFIG_RESERVATION_REQUESTED for user {user_id}.")
    finally:
        db.close()


@node_router.subscriber(EventKeys.CONFIG_RESERVATION_CONSUME_REQUESTED)
async def handle_config_reservation_consume(msg: dict):
    """
    Triggered when a top-up payment is approved.
    Consumes the reservation hold, creates the client on 3x-ui as '{username}-{region}-PAYG',
    fetches the exact URI from 3x-ui, and stores UserConfig.
    """
    reservation_id = msg.get("reservation_id")
    user_id = msg.get("user_id")
    topup_transaction_id = msg.get("topup_transaction_id")

    if not reservation_id or not user_id:
        return

    try:
        reservation_uuid = uuid.UUID(str(reservation_id))
        user_uuid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return

    db = SessionLocal()
    try:
        reservation = (
            db.query(ConfigReservation)
            .filter(
                ConfigReservation.id == reservation_uuid,
                ConfigReservation.user_id == user_uuid,
            )
            .with_for_update()
            .first()
        )

        if not reservation or reservation.status not in ("Reserved", "Consumed"):
            await broker.publish(
                {
                    "reservation_id": str(reservation_uuid),
                    "user_id": str(user_uuid),
                    "topup_transaction_id": str(topup_transaction_id) if topup_transaction_id else None,
                    "reason": "Reservation not found or invalid status.",
                },
                queue=EventKeys.CONFIG_RESERVATION_CONSUME_FAILED,
            )
            return

        if reservation.status == "Consumed":
            return

        # Single Config Check: If user already owns a config for this region, reactivate it
        existing_config = (
            db.query(UserConfig)
            .filter(
                UserConfig.user_id == user_uuid,
                UserConfig.location_id == reservation.location_id,
            )
            .first()
        )

        if existing_config:
            reservation.status = "Consumed"
            existing_config.is_active = True
            db.commit()

            await broker.publish(
                {
                    "reservation_id": str(reservation.id),
                    "user_id": str(user_uuid),
                    "location_id": str(reservation.location_id),
                    "topup_transaction_id": str(topup_transaction_id) if topup_transaction_id else None,
                    "config_id": str(existing_config.id),
                },
                queue=EventKeys.CONFIG_RESERVATION_CONSUMED,
            )
            return

        server = db.query(Server).filter(
            Server.id == reservation.server_id,
            Server.is_active == True,
            Server.is_reachable == True
        ).first()

        # SMART FALLBACK: If reserved server is inactive or unreachable, find another server in region
        if not server:
            fallback_slot = find_available_slot_for_region(db, reservation.location_id)
            if fallback_slot:
                server = fallback_slot["server"]
                reservation.server_id = server.id
                reservation.inbound_id = fallback_slot["inbound_id"]
                reservation.protocol = fallback_slot["protocol"]
                logger.info(
                    f"Smart fallback applied: user {user_uuid} switched to active server {server.name} in region."
                )
            else:
                raise RuntimeError("Reserved server is no longer active and all servers in region are full.")

        # Construct {username}-{region}-{short_id}-PAYG naming format with multi-config index support
        sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_uuid).first()
        raw_username = sub.username if (sub and sub.username) else f"usr_{str(user_uuid)[:8]}"

        loc = db.query(Location).filter(Location.id == reservation.location_id).first()
        loc_name = loc.name if loc else "Region"

        # Count existing configs for this user in this location to assign proper index if multiple
        existing_count = db.query(UserConfig).filter(
            UserConfig.user_id == user_uuid,
            UserConfig.location_id == reservation.location_id
        ).count()
        config_index = existing_count + 1 if existing_count > 0 else 1

        client_uuid = str(uuid.uuid4())
        sub_id = client_uuid.replace("-", "")[:16]

        config_name = build_config_name(raw_username, loc_name, client_uuid, index=config_index)
        client_email = config_name

        # Pre-flight database uniqueness check
        while db.query(UserConfig).filter(UserConfig.client_email == client_email).first():
            config_index += 1
            client_uuid = str(uuid.uuid4())
            sub_id = client_uuid.replace("-", "")[:16]
            config_name = build_config_name(raw_username, loc_name, client_uuid, index=config_index)
            client_email = config_name

        # Inject client into 3x-ui inbound panel with auto-healing duplicate email retry
        created_on_panel = await XUIClient.add_client(
            api_url=server.api_url,
            api_token=server.api_token,
            inbound_id=reservation.inbound_id,
            uuid=client_uuid,
            email=client_email,
            protocol=reservation.protocol,
            enable=True,
            version=server.xui_version,
            sub_id=sub_id,
            server_id=server.id,
            server_name=server.name
        )

        # RESILIENCE FALLBACK: If primary 3x-ui panel fails, try another reachable server in region
        if not created_on_panel:
            fallback_slot = find_available_slot_for_region(db, reservation.location_id)
            if fallback_slot and fallback_slot["server"].id != server.id:
                fallback_server = fallback_slot["server"]
                logger.warning(
                    f"3x-ui add_client failed on {server.name}. Attempting fallback to {fallback_server.name}."
                )
                server = fallback_server
                reservation.server_id = server.id
                reservation.inbound_id = fallback_slot["inbound_id"]
                reservation.protocol = fallback_slot["protocol"]
                created_on_panel = await XUIClient.add_client(
                    api_url=server.api_url,
                    api_token=server.api_token,
                    inbound_id=reservation.inbound_id,
                    uuid=client_uuid,
                    email=client_email,
                    protocol=reservation.protocol,
                    enable=True,
                    version=server.xui_version,
                    sub_id=sub_id,
                    server_id=server.id,
                    server_name=server.name
                )

        if not created_on_panel:
            err_detail = getattr(created_on_panel, "error_msg", None) or f"3x-ui client creation failed on {server.api_url}."
            raise RuntimeError(err_detail)

        # Use verified unique email and uuid accepted by 3X-UI
        actual_email = getattr(created_on_panel, "email", client_email)
        actual_uuid = getattr(created_on_panel, "uuid", client_uuid)

        # Retrieve direct connection URI from 3x-ui API using actual_email
        direct_uri = await XUIClient.get_client_uri(
            api_url=server.api_url,
            api_token=server.api_token,
            inbound_id=reservation.inbound_id,
            email=actual_email,
            version=server.xui_version,
            client_uuid=actual_uuid,
            sub_id=actual_uuid.replace("-", "")[:16]
        )

        # Enforce exact remark hash on the connection URI matching actual_email
        if direct_uri:
            base_link = direct_uri.split("#")[0]
            direct_uri = f"{base_link}#{actual_email}"

        new_config = UserConfig(
            user_id=user_uuid,
            location_id=reservation.location_id,
            server_id=server.id,
            inbound_id=reservation.inbound_id,
            protocol=reservation.protocol,
            client_email=actual_email,
            client_uuid=actual_uuid,
            uri=direct_uri,
            is_active=True,
        )
        db.add(new_config)
        reservation.status = "Consumed"

        db.commit()
        db.refresh(new_config)

        await broker.publish(
            {
                "reservation_id": str(reservation.id),
                "user_id": str(user_uuid),
                "location_id": str(reservation.location_id),
                "topup_transaction_id": str(topup_transaction_id) if topup_transaction_id else None,
                "config_id": str(new_config.id),
                "server_id": str(server.id),
                "inbound_id": int(reservation.inbound_id),
                "protocol": reservation.protocol,
            },
            queue=EventKeys.CONFIG_RESERVATION_CONSUMED,
        )

        await sync_location_capacity(db, reservation.location_id)
        logger.info(f"Provisioned Traffic Routing config '{config_name}' on server {server.name} ({reservation.protocol}).")

    except Exception as e:
        db.rollback()
        logger.exception(f"Failed consuming reservation {reservation_id}: {e}")
        await broker.publish(
            {
                "reservation_id": str(reservation_uuid),
                "user_id": str(user_uuid),
                "topup_transaction_id": str(topup_transaction_id) if topup_transaction_id else None,
                "reason": str(e),
            },
            queue=EventKeys.CONFIG_RESERVATION_CONSUME_FAILED,
        )
    finally:
        db.close()


@node_router.subscriber(EventKeys.CONFIG_RESERVATION_RELEASE_REQUESTED)
async def handle_config_reservation_release(msg: dict):
    """
    Triggered when a top-up is rejected or cancelled.
    Releases the reservation slot so it becomes available for other clients.
    """
    raw_reservation_id = msg.get("reservation_id")
    if not raw_reservation_id:
        return

    try:
        reservation_id = uuid.UUID(str(raw_reservation_id))
    except (ValueError, TypeError):
        return

    db = SessionLocal()
    try:
        reservation = (
            db.query(ConfigReservation)
            .filter(
                ConfigReservation.id == reservation_id,
                ConfigReservation.status == "Reserved",
            )
            .with_for_update()
            .first()
        )

        if not reservation or reservation.status != "Reserved":
            return

        reservation.status = "Released"
        location_id = reservation.location_id
        db.commit()

        await broker.publish(
            {
                "reservation_id": str(reservation.id),
                "user_id": str(reservation.user_id),
                "location_id": str(location_id),
                "topup_transaction_id": str(reservation.topup_transaction_id) if reservation.topup_transaction_id else None,
            },
            queue=EventKeys.CONFIG_RESERVATION_RELEASED,
        )

        await sync_location_capacity(db, location_id)
        logger.info(f"Successfully released reservation {reservation_id}.")

    except Exception:
        db.rollback()
        logger.exception(f"Failed releasing reservation {reservation_id}.")
    finally:
        db.close()


@node_router.subscriber(EventKeys.WALLET_EMPTY)
async def handle_wallet_empty(msg: dict):
    """
    Triggered when Billing service detects a user's balance reaches 0 or drops <= safety cutoff.
    1. Sets has_credit = False in local node_db.
    2. Turns OFF (enable=False) the client across all 3x-ui panels and marks is_active=False.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError):
        return

    db = SessionLocal()
    try:
        subscription = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
        if subscription:
            subscription.has_credit = False

        configs = db.query(UserConfig).filter(
            UserConfig.user_id == user_id,
            UserConfig.is_active == True,
        ).all()

        for config in configs:
            server = config.server
            if not server or not getattr(server, "is_reachable", True):
                continue

            # Turn OFF client credentials in 3X-UI
            success = await XUIClient.update_client_status(
                api_url=server.api_url,
                api_token=server.api_token,
                inbound_id=config.inbound_id,
                uuid=config.client_uuid,
                email=config.client_email,
                enable=False,  # Disconnects client immediately
                version=server.xui_version,
                server_id=server.id,
                server_name=server.name
            )

            if success:
                config.is_active = False
                logger.info(f"Turned OFF user {user_id} on {server.name} (Credit <= safety cutoff).")

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error processing WALLET_EMPTY for user {user_id}: {e}")
    finally:
        db.close()


@node_router.subscriber(EventKeys.WALLET_FUNDED)
async def handle_wallet_funded(msg: dict):
    """
    Triggered when a user receives credit (gift, top-up approval, or balance adjustment).
    1. Sets has_credit = True in local node_db.
    2. Turns ON (enable=True) all client configs in 3X-UI immediately.
    3. Provisions any missing regions as active.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError):
        return

    db = SessionLocal()
    try:
        # Mark local authorization flag as True
        subscription = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
        if subscription:
            subscription.has_credit = True
        else:
            sub_token = uuid.uuid4().hex + uuid.uuid4().hex
            subscription = UserSubscription(user_id=user_id, sub_token=sub_token, has_credit=True)
            db.add(subscription)

        # IMMEDIATELY TURN ON ON 3X-UI PANELS
        configs = db.query(UserConfig).filter(UserConfig.user_id == user_id).all()
        for config in configs:
            server = config.server
            if not server:
                continue

            try:
                success = await XUIClient.update_client_status(
                    api_url=server.api_url,
                    api_token=server.api_token,
                    inbound_id=config.inbound_id,
                    uuid=config.client_uuid,
                    email=config.client_email,
                    enable=True,  # TURN ON IMMEDIATELY
                    version=server.xui_version,
                    server_id=server.id,
                    server_name=server.name
                )
                if success:
                    config.is_active = True
                    server.is_reachable = True
                    server.last_error = None
            except Exception as conn_err:
                logger.warning(f"Failed to reach {server.name} during WALLET_FUNDED: {conn_err}")

        db.commit()

        # Provision any missing locations
        username = subscription.username if subscription else None
        await provision_user_configs_for_all_locations(db, user_id, username=username, is_active=True)
        logger.info(f"Enabled Traffic Routing configurations for User {user_id} via WALLET_FUNDED.")

    except Exception as e:
        db.rollback()
        logger.error(f"Error processing WALLET_FUNDED for user {user_id}: {e}")
    finally:
        db.close()


@node_router.subscriber(EventKeys.CONFIG_RELEASED)
async def handle_config_released(msg: dict):
    """
    Triggered when a user's wallet has been empty past the grace period duration.
    1. Removes client credentials from the 3X-UI panel API.
    2. Deletes the UserConfig row from node_db.
    3. Frees up the inbound slot and syncs capacity for new reservations.
    """
    raw_user_id = msg.get("user_id")
    raw_location_id = msg.get("location_id")

    if not raw_user_id:
        return

    try:
        user_id = uuid.UUID(str(raw_user_id))
        location_id = uuid.UUID(str(raw_location_id)) if raw_location_id else None
    except (ValueError, TypeError):
        return

    db = SessionLocal()
    try:
        query = db.query(UserConfig).filter(UserConfig.user_id == user_id)
        if location_id:
            query = query.filter(UserConfig.location_id == location_id)
        configs = query.all()

        affected_locations = set()
        for config in configs:
            loc_id = config.location_id
            if loc_id:
                affected_locations.add(loc_id)
            server = config.server

            # 1. Delete client config from 3X-UI panel
            if server and getattr(server, "is_reachable", True):
                try:
                    await XUIClient.remove_client(
                        api_url=server.api_url,
                        api_token=server.api_token,
                        inbound_id=config.inbound_id,
                        uuid=config.client_uuid,
                        email=config.client_email,
                        version=getattr(server, "xui_version", "v3.5.0"),
                        server_id=server.id,
                        server_name=server.name
                    )
                except Exception as del_err:
                    logger.warning(f"Could not remove client from 3x-ui during config release: {del_err}")

            # 2. Delete UserConfig row to free the slot
            db.delete(config)
            logger.info(f"Deleted expired UserConfig for user {user_id} (location={loc_id}). Slot is now free.")

        db.commit()

        # 3. Recalculate capacity to mark the slot as available
        for loc_id in affected_locations:
            await sync_location_capacity(db, loc_id)

    except Exception as e:
        db.rollback()
        logger.error(f"Error processing CONFIG_RELEASED for user {user_id}: {e}")
    finally:
        db.close()


async def expire_config_reservations():
    """
    Background cron/scheduler task that sweeps expired reservations.
    Emits CONFIG_RESERVATION_EXPIRED and recalculates location capacities.
    """
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        expired = (
            db.query(ConfigReservation)
            .filter(
                ConfigReservation.status == "Reserved",
                ConfigReservation.expires_at <= now,
            )
            .with_for_update()
            .all()
        )

        affected_locations = set()
        for reservation in expired:
            reservation.status = "Expired"
            affected_locations.add(reservation.location_id)

            await broker.publish(
                {
                    "reservation_id": str(reservation.id),
                    "user_id": str(reservation.user_id),
                    "location_id": str(reservation.location_id),
                    "topup_transaction_id": str(reservation.topup_transaction_id) if reservation.topup_transaction_id else None,
                },
                queue=EventKeys.CONFIG_RESERVATION_EXPIRED,
            )

        db.commit()

        for loc_id in affected_locations:
            await sync_location_capacity(db, loc_id)

    except Exception:
        db.rollback()
        logger.exception("Failed expiring configuration reservations.")
    finally:
        db.close()


CONFIG_SETTINGS_EXCHANGE = getattr(
    EventKeys,
    "CONFIG_SETTINGS_EXCHANGE",
    RabbitExchange("config_settings_fanout", type=ExchangeType.FANOUT)
)


@node_router.subscriber("node_config_settings_queue", exchange=CONFIG_SETTINGS_EXCHANGE)
async def handle_config_settings_changed_exchange(msg: dict):
    """Listens for settings updates broadcasted to all microservices via Fanout Exchange."""
    await process_config_settings_changed(msg)


@node_router.subscriber(EventKeys.CONFIG_SETTINGS_CHANGED)
async def handle_config_settings_changed_queue(msg: dict):
    """Listens for settings updates sent directly to queue (fallback)."""
    await process_config_settings_changed(msg)


async def process_config_settings_changed(msg: dict):
    """Event-Carried State Transfer: Persists route replacement policy settings in node_settings."""
    db = SessionLocal()
    try:
        if "route_replace_cooldown_days" in msg:
            val = str(max(0, int(msg["route_replace_cooldown_days"])))
            row = db.query(NodeSettings).filter(NodeSettings.key == "route_replace_cooldown_days").first()
            if not row:
                db.add(NodeSettings(key="route_replace_cooldown_days", value=val))
            else:
                row.value = val

        if "disable_client_route_replacement" in msg:
            val = "true" if msg["disable_client_route_replacement"] else "false"
            row = db.query(NodeSettings).filter(NodeSettings.key == "disable_client_route_replacement").first()
            if not row:
                db.add(NodeSettings(key="disable_client_route_replacement", value=val))
            else:
                row.value = val

        if "xui_request_timeout" in msg:
            val = str(max(1.0, float(msg["xui_request_timeout"])))
            row = db.query(NodeSettings).filter(NodeSettings.key == "xui_request_timeout").first()
            if not row:
                db.add(NodeSettings(key="xui_request_timeout", value=val))
            else:
                row.value = val
            XUIClient.set_timeout(float(val))

        db.commit()
        logger.info(f"Node Service synchronized route replacement and timeout settings: {msg}")
    except Exception as e:
        db.rollback()
        logger.error(f"Error persisting route replacement settings in node_db: {e}")
    finally:
        db.close()


@node_router.subscriber(EventKeys.CONFIG_SYNC_INTERVAL_CHANGED)
async def handle_sync_interval_changed(msg: dict):
    """
    Event-Carried State Transfer:
    Saves new sync interval in local node_db and updates background scheduler immediately.
    """
    raw_interval = msg.get("billing_sync_interval")
    if not raw_interval:
        return

    try:
        interval = max(1, int(raw_interval))
        set_sync_interval_minutes(interval)

        # Persist to local database
        db = SessionLocal()
        try:
            setting = db.query(NodeSettings).filter(NodeSettings.key == "billing_sync_interval").first()
            if not setting:
                db.add(NodeSettings(key="billing_sync_interval", value=str(interval)))
            else:
                setting.value = str(interval)
            db.commit()
            logger.info(f"Node Service persisted billing_sync_interval={interval}m in node_db via event.")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"Failed processing CONFIG_SYNC_INTERVAL_CHANGED event: {e}")



@node_router.subscriber(EventKeys.USER_SUSPENDED)
async def handle_user_suspended_by_admin(msg: dict):
    """
    Triggered when Admin suspends a user account.
    Completely removes all client configs from 3X-UI panels and deletes them from the service.
    Frees up inbound slots immediately for other users.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return
    user_id = uuid.UUID(str(raw_user_id))
    db = SessionLocal()
    try:
        sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
        if sub:
            sub.is_admin_suspended = True

        configs = db.query(UserConfig).filter(UserConfig.user_id == user_id).all()
        affected_locations = set()

        for config in configs:
            if config.location_id:
                affected_locations.add(config.location_id)
            server = config.server
            if server and getattr(server, "is_reachable", True):
                try:
                    await XUIClient.remove_client(
                        api_url=server.api_url,
                        api_token=server.api_token,
                        inbound_id=config.inbound_id,
                        uuid=config.client_uuid,
                        email=config.client_email,
                        version=getattr(server, "xui_version", "v3.5.0"),
                        server_id=server.id,
                        server_name=server.name
                    )
                except Exception as del_err:
                    logger.warning(f"Failed removing client from 3X-UI during admin suspension: {del_err}")

            db.delete(config)

        db.commit()

        # Recalculate capacity to free up inbound slots immediately
        for loc_id in affected_locations:
            await sync_location_capacity(db, loc_id)

        logger.info(f"Admin suspended User {user_id}: Deleted {len(configs)} configs from 3X-UI and service.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling USER_SUSPENDED for user {user_id}: {e}")
    finally:
        db.close()


@node_router.subscriber(EventKeys.USER_ACTIVATED)
async def handle_user_activated_by_admin(msg: dict):
    """
    Triggered when Admin activates a user account.
    Creates active configs on 3X-UI and service if the user has gift or credit.
    If all servers in a region are full, user enters waiting status until new servers/inbounds are added.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return
    user_id = uuid.UUID(str(raw_user_id))
    db = SessionLocal()
    try:
        sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
        if sub:
            sub.is_admin_suspended = False
        else:
            sub_token = uuid.uuid4().hex + uuid.uuid4().hex
            sub = UserSubscription(user_id=user_id, sub_token=sub_token, is_admin_suspended=False)
            db.add(sub)
        db.commit()
        db.refresh(sub)

        # Only provision configs if user has credit and hasn't paused their own Traffic Routing
        should_provision = bool(sub.has_credit and not sub.is_self_suspended)
        if should_provision:
            username = sub.username or f"usr_{str(user_id)[:8]}"
            await provision_user_configs_for_all_locations(db, user_id, username=username, is_active=True)
            logger.info(f"Admin activated User {user_id}: Provisioned configs on 3X-UI for all available regions.")
        else:
            logger.info(f"Admin activated User {user_id}: Account active, waiting for wallet credit to provision configs.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling USER_ACTIVATED for user {user_id}: {e}")
    finally:
        db.close()


@node_router.subscriber(EventKeys.USER_VPN_PAUSED)
async def handle_user_vpn_paused_by_client(msg: dict):
    """
    Triggered when Client self-suspends/pauses their Traffic Routing in Account Settings.
    Disables 3X-UI configs. (Client remains logged in).
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return
    user_id = uuid.UUID(str(raw_user_id))
    db = SessionLocal()
    try:
        sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
        if sub:
            sub.is_self_suspended = True

        configs = db.query(UserConfig).filter(UserConfig.user_id == user_id).all()
        for config in configs:
            server = config.server
            if server and getattr(server, "is_reachable", True):
                await XUIClient.update_client_status(
                    api_url=server.api_url,
                    api_token=server.api_token,
                    inbound_id=config.inbound_id,
                    uuid=config.client_uuid,
                    email=config.client_email,
                    enable=False,
                    version=server.xui_version,
                    server_id=server.id,
                    server_name=server.name
                )
            config.is_active = False

        db.commit()
        logger.info(f"Client self-paused Traffic Routing: Turned OFF 3X-UI configs for User {user_id}.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling USER_VPN_PAUSED for user {user_id}: {e}")
    finally:
        db.close()


@node_router.subscriber(EventKeys.USER_VPN_RESUMED)
async def handle_user_vpn_resumed_by_client(msg: dict):
    """
    Triggered when Client reactivates their Traffic Routing in Account Settings.
    Enables configs ONLY if user has credit and is not admin-suspended!
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return
    user_id = uuid.UUID(str(raw_user_id))
    db = SessionLocal()
    try:
        sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
        if sub:
            sub.is_self_suspended = False

        should_enable = bool(sub and sub.has_credit and not sub.is_admin_suspended)

        configs = db.query(UserConfig).filter(UserConfig.user_id == user_id).all()
        for config in configs:
            server = config.server
            if server and getattr(server, "is_reachable", True):
                await XUIClient.update_client_status(
                    api_url=server.api_url,
                    api_token=server.api_token,
                    inbound_id=config.inbound_id,
                    uuid=config.client_uuid,
                    email=config.client_email,
                    enable=should_enable,
                    version=server.xui_version,
                    server_id=server.id,
                    server_name=server.name
                )
            config.is_active = should_enable

        db.commit()

        if should_enable:
            username = sub.username if sub else None
            await provision_user_configs_for_all_locations(db, user_id, username=username, is_active=True)

        logger.info(f"Client self-resumed Traffic Routing: User {user_id} enabled={should_enable} (has_credit={sub.has_credit if sub else False}).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling USER_VPN_RESUMED for user {user_id}: {e}")
    finally:
        db.close()