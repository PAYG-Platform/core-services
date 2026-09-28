# backend/node-service/app/services/provisioning.py
import os
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List
from sqlalchemy.orm import Session
import httpx

from app.models import (
    Location, 
    Server, 
    UserConfig, 
    UserSubscription, 
    NodeSettings, 
    UserBlockedRouteHistory,
    ConfigReservation
)
from app.services.reservations import find_available_slot_for_region
from app.xui_client import XUIClient, persist_server_log
from app.events.publisher import (
    publish_location_capacity_exceeded,
    publish_location_capacity_event,
    publish_server_offline_event,
    publish_server_recovered_event,
    publish_traffic_consumed_event,
)

logger = logging.getLogger(__name__)


# =========================================================================
# DYNAMIC CRON POLLING INTERVAL MANAGEMENT (Event-Driven / Local DB)
# =========================================================================
_current_sync_interval_minutes: int = 5
_interval_wake_event: asyncio.Event = asyncio.Event()


def get_sync_interval_minutes() -> int:
    """Returns the currently active sync interval in minutes."""
    return _current_sync_interval_minutes


def set_sync_interval_minutes(minutes: int):
    """Dynamically updates the sync interval and immediately interrupts active sleep."""
    global _current_sync_interval_minutes
    _current_sync_interval_minutes = max(1, int(minutes))
    _interval_wake_event.set()


def get_interval_wake_event() -> asyncio.Event:
    """Returns the cancellation/wake event used by the background scheduler loop."""
    return _interval_wake_event


def load_local_sync_interval(db: Session) -> int:
    """
    Loads billing_sync_interval from the local node_db (populated via messaging)
    or falls back to the local environment variable.
    Zero external HTTP calls required.
    """
    global _current_sync_interval_minutes
    try:
        setting = db.query(NodeSettings).filter(NodeSettings.key == "billing_sync_interval").first()
        if setting and setting.value:
            _current_sync_interval_minutes = max(1, int(setting.value))
            logger.info(f"Loaded billing_sync_interval={_current_sync_interval_minutes}m from local node_db.")
            return _current_sync_interval_minutes
    except Exception as e:
        logger.warning(f"Could not load local sync interval from DB: {e}")

    env_val = int(os.getenv("SYNC_INTERVAL_MINUTES", "5"))
    _current_sync_interval_minutes = max(1, env_val)
    return _current_sync_interval_minutes


def load_local_xui_timeout(db: Session) -> float:
    """
    Loads xui_request_timeout from local node_db (populated via messaging/settings)
    and applies it to XUIClient. Defaults to 15.0s.
    """
    try:
        setting = db.query(NodeSettings).filter(NodeSettings.key == "xui_request_timeout").first()
        if setting and setting.value:
            timeout_val = max(1.0, float(setting.value))
            XUIClient.set_timeout(timeout_val)
            logger.info(f"Loaded xui_request_timeout={timeout_val}s from local node_db.")
            return timeout_val
    except Exception as e:
        logger.warning(f"Could not load xui timeout from DB: {e}")

    env_val = float(os.getenv("XUI_REQUEST_TIMEOUT", "15.0"))
    XUIClient.set_timeout(env_val)
    return env_val


def format_bytes_human(byte_count: int) -> str:
    """Formats byte integer into clean human-readable units (B, KB, MB, GB)."""
    if not byte_count or byte_count <= 0:
        return "0 B"
    b = float(byte_count)
    if b >= 1024 ** 3:
        return f"{b / (1024 ** 3):.2f} GB"
    if b >= 1024 ** 2:
        return f"{b / (1024 ** 2):.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{int(b)} B"


# =========================================================================
# HELPER FUNCTIONS
# =========================================================================
_user_provisioning_locks: Dict[str, asyncio.Lock] = {}


def get_user_provisioning_lock(user_id: uuid.UUID) -> asyncio.Lock:
    """Provides a dedicated in-memory async lock per user to serialize provisioning."""
    uid_str = str(user_id)
    if uid_str not in _user_provisioning_locks:
        _user_provisioning_locks[uid_str] = asyncio.Lock()
    return _user_provisioning_locks[uid_str]


def build_config_name(
    username: str, 
    location_name: str, 
    client_uuid: Optional[str] = None,
    index: Optional[int] = None
) -> str:
    """
    Constructs a unique config name and email:
    - Single config:  {username}-{region}-{short_id}-PAYG
    - Multi-config:   {username}-{region}-{index}-{short_id}-PAYG
    All spaces are replaced with underscores (_).
    """
    clean_username = (username or "user").strip().replace(" ", "_")
    clean_region = (location_name or "Region").strip().replace(" ", "_")
    
    short_id = str(client_uuid).replace("-", "")[:8] if client_uuid else uuid.uuid4().hex[:8]
    
    if index and index > 1:
        return f"{clean_username}-{clean_region}-{index}-{short_id}-PAYG"
    return f"{clean_username}-{clean_region}-{short_id}-PAYG"


async def fetch_live_balances_from_billing() -> Dict[str, bool]:
    """Reads credit authorization state synchronized via RabbitMQ WALLET_FUNDED and WALLET_EMPTY events."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        subscriptions = db.query(UserSubscription).all()
        return {
            str(s.user_id): bool(s.has_credit)
            for s in subscriptions
        }
    except Exception as e:
        logger.warning(f"Could not read synchronized credit state from local database: {e}")
        return {}
    finally:
        db.close()


async def sync_location_capacity(db: Session, location_id: uuid.UUID):
    """Recalculates and broadcasts location capacity metrics."""
    try:
        loc = db.query(Location).filter(Location.id == location_id).first()
        if not loc:
            return

        servers = db.query(Server).filter(
            Server.location_id == location_id,
            Server.is_active == True,
            Server.is_reachable == True
        ).all()

        total_capacity = 0
        used_capacity = 0
        has_free_slot = False

        for s in servers:
            limits = s.inbound_limits if isinstance(s.inbound_limits, list) else []
            for lim in limits:
                if not isinstance(lim, dict):
                    continue
                inbound_id = int(lim.get("inbound_id", s.target_inbound_id or 1))
                protocol = str(lim.get("protocol", s.protocol_type or "vless_reality")).strip().lower()
                user_limit = max(0, int(lim.get("user_limit", 0)))
                total_capacity += user_limit

                # All configs holding this inbound count toward occupied capacity (even if in grace period)
                occupied_cfgs = db.query(UserConfig).filter(
                    UserConfig.server_id == s.id,
                    UserConfig.inbound_id == inbound_id,
                    UserConfig.protocol == protocol,
                ).count()

                now = datetime.now(timezone.utc)
                active_reservations = db.query(ConfigReservation).filter(
                    ConfigReservation.server_id == s.id,
                    ConfigReservation.inbound_id == inbound_id,
                    ConfigReservation.protocol == protocol,
                    ConfigReservation.status == "Reserved",
                    ConfigReservation.expires_at > now,
                ).count()

                used_this = min(user_limit, occupied_cfgs + active_reservations)
                used_capacity += used_this

                if occupied_cfgs + active_reservations < user_limit:
                    has_free_slot = True

        is_full = (total_capacity == 0) or (not has_free_slot)

        await publish_location_capacity_event(
            location_id=str(location_id),
            location_name=loc.name,
            is_full=is_full,
            total_capacity=total_capacity,
            used_capacity=used_capacity,
        )
    except Exception as e:
        logger.error(f"Error syncing location capacity for {location_id}: {e}")


async def provision_user_configs_for_all_locations(
    db: Session,
    user_id: uuid.UUID,
    username: Optional[str] = None,
    is_active: bool = False
):
    """
    Ensures this user has exactly 1 active config from each active Location.
    Only provisions slots if user has credit (is_active=True).
    Uses a per-user lock to prevent concurrent duplicate provisioning on 3X-UI.
    """
    if not is_active:
        return

    async with get_user_provisioning_lock(user_id):
        # Refresh session to see newly committed records from concurrent tasks
        db.expire_all()

        active_locations = db.query(Location).filter(Location.is_active == True).all()

        # Resolve username from local UserSubscription if not passed directly
        if not username:
            sub_record = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
            username = sub_record.username if (sub_record and sub_record.username) else f"usr_{str(user_id)[:8]}"

        for loc in active_locations:
            existing_cfg = db.query(UserConfig).filter(
                UserConfig.user_id == user_id,
                UserConfig.location_id == loc.id
            ).first()

            # If user already has a config in this location on a reachable server:
            if existing_cfg and existing_cfg.server and getattr(existing_cfg.server, "is_reachable", True):
                if not existing_cfg.is_active:
                    # Re-enable the existing client on 3X-UI instead of creating a second client!
                    try:
                        await XUIClient.update_client_status(
                            api_url=existing_cfg.server.api_url,
                            api_token=existing_cfg.server.api_token,
                            inbound_id=existing_cfg.inbound_id,
                            uuid=existing_cfg.client_uuid,
                            email=existing_cfg.client_email,
                            enable=True,
                            version=getattr(existing_cfg.server, "xui_version", "v3.5.0"),
                            server_id=existing_cfg.server.id,
                            server_name=existing_cfg.server.name
                        )
                    except Exception:
                        pass
                    existing_cfg.is_active = True
                    db.commit()
                continue

            slot = find_available_slot_for_region(db, loc.id, user_id=user_id)
            if not slot:
                logger.warning(f"No reachable capacity in location '{loc.name}' for user {user_id}.")
                await publish_location_capacity_exceeded(
                    location_id=str(loc.id),
                    location_name=loc.name,
                    user_id=str(user_id),
                    username=username
                )
                continue

            server = slot["server"]
            client_uuid = str(uuid.uuid4())
            sub_id = client_uuid.replace("-", "")[:16]
            short_id = client_uuid.replace("-", "")[:8]

            # Generate unique config name with short_id and set client_email to match
            config_name = build_config_name(username, loc.name, client_uuid)
            client_email = config_name

            created = await XUIClient.add_client(
                api_url=server.api_url,
                api_token=server.api_token,
                inbound_id=slot["inbound_id"],
                uuid=client_uuid,
                email=client_email,
                protocol=slot["protocol"],
                enable=True,
                version=server.xui_version,
                sub_id=sub_id,
                server_id=server.id,
                server_name=server.name
            )

            if created:
                actual_email = getattr(created, "email", client_email)
                actual_uuid = getattr(created, "uuid", client_uuid)

                direct_uri = await XUIClient.get_client_uri(
                    api_url=server.api_url,
                    api_token=server.api_token,
                    inbound_id=slot["inbound_id"],
                    email=actual_email,
                    client_uuid=actual_uuid,
                    sub_id=actual_uuid.replace("-", "")[:16],
                    version=server.xui_version
                )

                if direct_uri:
                    base_link = direct_uri.split("#")[0]
                    direct_uri = f"{base_link}#{actual_email}"

                if existing_cfg:
                    if existing_cfg.server and getattr(existing_cfg.server, "is_reachable", True):
                        try:
                            ok = await XUIClient.remove_client(
                                api_url=existing_cfg.server.api_url,
                                api_token=existing_cfg.server.api_token,
                                inbound_id=existing_cfg.inbound_id,
                                uuid=existing_cfg.client_uuid,
                                email=existing_cfg.client_email,
                                version=getattr(existing_cfg.server, "xui_version", "v3.5.0"),
                                server_id=existing_cfg.server.id,
                                server_name=existing_cfg.server.name
                            )
                            if not ok:
                                await XUIClient.sweep_and_remove_all_payg_clients(
                                    existing_cfg.server.api_url,
                                    existing_cfg.server.api_token,
                                    known_client_emails=[existing_cfg.client_email],
                                    known_client_uuids=[existing_cfg.client_uuid],
                                    server_id=existing_cfg.server.id,
                                    server_name=existing_cfg.server.name
                                )
                        except Exception:
                            pass

                    existing_cfg.server_id = server.id
                    existing_cfg.inbound_id = slot["inbound_id"]
                    existing_cfg.protocol = slot["protocol"]
                    existing_cfg.client_uuid = actual_uuid
                    existing_cfg.client_email = actual_email
                    existing_cfg.uri = direct_uri
                    existing_cfg.is_active = True
                    existing_cfg.last_traffic = 0
                else:
                    existing_cfg = UserConfig(
                        user_id=user_id,
                        location_id=loc.id,
                        server_id=server.id,
                        inbound_id=slot["inbound_id"],
                        protocol=slot["protocol"],
                        client_email=actual_email,
                        client_uuid=actual_uuid,
                        uri=direct_uri,
                        is_active=True
                    )
                    db.add(existing_cfg)

                db.commit()
                logger.info(f"Provisioned config '{actual_email}' (active=True) for user {user_id} on {server.name}")
                await sync_location_capacity(db, loc.id)
            else:
                err_detail = getattr(created, "error_msg", None) or "Failed to add client to panel"
                server.last_error = err_detail
                if not any(k in str(err_detail).lower() for k in ["duplicate", "exist", "client"]):
                    server.is_reachable = False
                    server.last_unreachable_at = datetime.now(timezone.utc)
                    await publish_server_offline_event(
                        server_id=str(server.id),
                        server_name=server.name,
                        api_url=server.api_url,
                        error_message=server.last_error
                    )
                db.commit()


async def provision_missing_users_for_location(db: Session, location_id: uuid.UUID):
    """
    Called when a new server or location is added, or when a server recovers.
    Provisions configurations using the local has_credit status on UserSubscription (zero HTTP calls).
    Auto-heals missing, inactive, or blocked configs for funded users.
    """
    loc = db.query(Location).filter(Location.id == location_id, Location.is_active == True).first()
    if not loc:
        return

    subscriptions = db.query(UserSubscription).all()
    if not subscriptions:
        return

    unassigned_count = 0
    assigned_count = 0

    for sub in subscriptions:
        user_id = sub.user_id
        user_is_funded = bool(
            sub.has_credit 
            and not getattr(sub, "is_admin_suspended", False) 
            and not getattr(sub, "is_self_suspended", False)
        )

        # Do not provision server slots for unfunded users who never had credit
        if not user_is_funded:
            continue

        async with get_user_provisioning_lock(user_id):
            db.expire_all()

            existing_cfg = db.query(UserConfig).filter(
                UserConfig.user_id == user_id,
                UserConfig.location_id == loc.id
            ).first()

            needs_provisioning = False
            if not existing_cfg:
                needs_provisioning = True
            else:
                server_dead = (
                    not existing_cfg.server 
                    or not existing_cfg.server.is_active 
                    or not getattr(existing_cfg.server, "is_reachable", True)
                )
                is_blocked = (
                    db.query(UserBlockedRouteHistory)
                    .filter(
                        UserBlockedRouteHistory.user_id == user_id,
                        UserBlockedRouteHistory.location_id == loc.id,
                        UserBlockedRouteHistory.server_id == existing_cfg.server_id,
                        UserBlockedRouteHistory.inbound_id == existing_cfg.inbound_id
                    )
                    .first() is not None
                )

                if server_dead or is_blocked:
                    needs_provisioning = True
                elif not existing_cfg.is_active:
                    # If config already exists and server is reachable, just re-enable it on 3X-UI
                    try:
                        await XUIClient.update_client_status(
                            api_url=existing_cfg.server.api_url,
                            api_token=existing_cfg.server.api_token,
                            inbound_id=existing_cfg.inbound_id,
                            uuid=existing_cfg.client_uuid,
                            email=existing_cfg.client_email,
                            enable=True,
                            version=getattr(existing_cfg.server, "xui_version", "v3.5.0"),
                            server_id=existing_cfg.server.id,
                            server_name=existing_cfg.server.name
                        )
                    except Exception:
                        pass
                    existing_cfg.is_active = True
                    db.commit()
                    continue

            if not needs_provisioning:
                continue

            slot = find_available_slot_for_region(db, loc.id, user_id=user_id)
        if not slot:
            unassigned_count += 1
            continue

        server = slot["server"]
        client_uuid = str(uuid.uuid4())
        sub_id = client_uuid.replace("-", "")[:16]
        short_id = client_uuid.replace("-", "")[:8]

        raw_username = sub.username or f"usr_{str(user_id)[:8]}"
        config_name = build_config_name(raw_username, loc.name, client_uuid)
        client_email = config_name

        created = await XUIClient.add_client(
            api_url=server.api_url,
            api_token=server.api_token,
            inbound_id=slot["inbound_id"],
            uuid=client_uuid,
            email=client_email,
            protocol=slot["protocol"],
            enable=True,
            version=server.xui_version,
            sub_id=sub_id,
            server_id=server.id,
            server_name=server.name
        )

        if created:
            actual_email = getattr(created, "email", client_email)
            actual_uuid = getattr(created, "uuid", client_uuid)

            direct_uri = await XUIClient.get_client_uri(
                api_url=server.api_url,
                api_token=server.api_token,
                inbound_id=slot["inbound_id"],
                email=actual_email,
                client_uuid=actual_uuid,
                sub_id=actual_uuid.replace("-", "")[:16],
                version=server.xui_version
            )

            if direct_uri:
                base_link = direct_uri.split("#")[0]
                direct_uri = f"{base_link}#{actual_email}"

            if existing_cfg:
                # Remove client from previous server if reachable
                if existing_cfg.server and getattr(existing_cfg.server, "is_reachable", True):
                    try:
                        ok = await XUIClient.remove_client(
                            api_url=existing_cfg.server.api_url,
                            api_token=existing_cfg.server.api_token,
                            inbound_id=existing_cfg.inbound_id,
                            uuid=existing_cfg.client_uuid,
                            email=existing_cfg.client_email,
                            version=getattr(existing_cfg.server, "xui_version", "v3.5.0"),
                            server_id=existing_cfg.server.id,
                            server_name=existing_cfg.server.name
                        )
                        if not ok:
                            await XUIClient.sweep_and_remove_all_payg_clients(
                                existing_cfg.server.api_url,
                                existing_cfg.server.api_token,
                                known_client_emails=[existing_cfg.client_email],
                                known_client_uuids=[existing_cfg.client_uuid],
                                server_id=existing_cfg.server.id,
                                server_name=existing_cfg.server.name
                            )
                    except Exception:
                        pass

                existing_cfg.server_id = server.id
                existing_cfg.inbound_id = slot["inbound_id"]
                existing_cfg.protocol = slot["protocol"]
                existing_cfg.client_uuid = actual_uuid
                existing_cfg.client_email = actual_email
                existing_cfg.uri = direct_uri
                existing_cfg.is_active = True
                existing_cfg.last_traffic = 0
            else:
                existing_cfg = UserConfig(
                    user_id=user_id,
                    location_id=loc.id,
                    server_id=server.id,
                    inbound_id=slot["inbound_id"],
                    protocol=slot["protocol"],
                    client_email=actual_email,
                    client_uuid=actual_uuid,
                    uri=direct_uri,
                    is_active=True
                )
                db.add(existing_cfg)

            db.commit()
            assigned_count += 1
            logger.info(f"Auto-healed and provisioned config '{actual_email}' on {server.name}")
        else:
            err_detail = getattr(created, "error_msg", None) or "Connection failed during user assignment"
            server.last_error = err_detail
            if not any(k in str(err_detail).lower() for k in ["duplicate", "exist", "client"]):
                server.is_reachable = False
                server.last_unreachable_at = datetime.now(timezone.utc)
                await publish_server_offline_event(
                    server_id=str(server.id),
                    server_name=server.name,
                    api_url=server.api_url,
                    error_message=server.last_error
                )
            db.commit()
            unassigned_count += 1

    await sync_location_capacity(db, loc.id)

    if unassigned_count > 0:
        await publish_location_capacity_exceeded(
            location_id=str(loc.id),
            location_name=loc.name,
            user_id="multiple",
            username=f"{unassigned_count} waiting user(s)"
        )


# =========================================================================
# CORE SWEEP: HEALTH CHECK + TRAFFIC POLLING + RECONCILIATION
# =========================================================================
async def run_server_health_and_provisioning_sweep(db: Session) -> dict:
    """
    Bidirectional State Reconciliation Engine:
    1. Synchronizes live user balances from billing-service.
    2. Probes 3X-UI server reachability & logs connectivity errors.
    3. Enforces desired state on 3X-UI:
       - If user is admin-suspended: removes configs from 3X-UI and deletes from DB.
       - If a config must exist and is active, but was manually deleted from 3X-UI, RE-CREATES it on 3X-UI with enable=True.
       - If a config is disabled on 3X-UI but user has credit, forces enable=True.
       - If user has no credit, ensures enable=False.
    4. Calculates consumption delta bytes and emits TRAFFIC_CONSUMED events to RabbitMQ.
    5. Retroactively provisions missing configs for active locations for funded users.
    6. Records audit log in ServerLog with event_type="sync_traffic" to update telemetry.
    """
    logger.info("Executing scheduled PAYG reconciliation sweep...")
    servers = db.query(Server).filter(Server.is_active == True).all()

    if not servers:
        logger.info("No active server nodes found to sync.")
        return {"total_bytes_synced": 0, "processed_users": 0, "failed_servers": []}

    # 1. Update live credit state from billing-service
    live_credit_map = await fetch_live_balances_from_billing()
    subscriptions = db.query(UserSubscription).all()

    for s in subscriptions:
        uid_str = str(s.user_id)
        if uid_str in live_credit_map:
            s.has_credit = live_credit_map[uid_str]
    db.commit()

    user_credit_map: Dict[str, bool] = {
        str(s.user_id): bool(
            s.has_credit 
            and not getattr(s, "is_admin_suspended", False) 
            and not getattr(s, "is_self_suspended", False)
        ) 
        for s in subscriptions
    }

    sub_obj_map: Dict[str, UserSubscription] = {str(s.user_id): s for s in subscriptions}

    total_synced_bytes = 0
    processed_users = 0
    failed_servers = []
    now = datetime.now(timezone.utc)

    # 2. Iterate through all active servers
    for srv in servers:
        is_reachable, err, traffic_map, xui_enabled_map, elapsed_ms, status_code = await XUIClient.get_all_clients_traffic(
            srv.api_url, srv.api_token, server_id=srv.id, server_name=srv.name
        )

        loc_name = srv.location.name if srv.location else "VPN Region"

        if not is_reachable:
            if srv.is_reachable:
                srv.is_reachable = False
                srv.last_unreachable_at = now
                srv.last_error = err or "Connection timed out"
                db.commit()

                # Find all users with active configurations hosted on this server
                affected_users = [
                    str(r[0]) for r in db.query(UserConfig.user_id)
                    .filter(UserConfig.server_id == srv.id, UserConfig.is_active == True)
                    .distinct()
                    .all()
                ]

                await publish_server_offline_event(
                    server_id=str(srv.id),
                    server_name=srv.name,
                    api_url=srv.api_url,
                    error_message=srv.last_error,
                    location_name=loc_name,
                    affected_user_ids=affected_users
                )
            failed_servers.append(srv.name)
            continue
        else:
            if not srv.is_reachable:
                srv.is_reachable = True
                srv.last_error = None
                db.commit()
                logger.info(f"Server node '{srv.name}' has recovered and is reachable.")

                # Find all users with configurations hosted on this server
                affected_users = [
                    str(r[0]) for r in db.query(UserConfig.user_id)
                    .filter(UserConfig.server_id == srv.id, UserConfig.is_active == True)
                    .distinct()
                    .all()
                ]

                await publish_server_recovered_event(
                    server_id=str(srv.id),
                    server_name=srv.name,
                    api_url=srv.api_url,
                    location_name=loc_name,
                    affected_user_ids=affected_users
                )

        # Process user configs using the batch traffic map
        cfgs = db.query(UserConfig).filter(UserConfig.server_id == srv.id).all()
        server_client_deltas = {}
        server_delta_bytes = 0

        for cfg in cfgs:
            uid_str = str(cfg.user_id)
            user_has_credit = user_credit_map.get(uid_str, False)
            sub = sub_obj_map.get(uid_str)
            is_admin_suspended = bool(sub and getattr(sub, "is_admin_suspended", False))

            # --- CASE 1: USER IS ADMIN-SUSPENDED -> REMOVE FROM 3X-UI AND DB ---
            if is_admin_suspended:
                if cfg.client_email in xui_enabled_map:
                    try:
                        await XUIClient.remove_client(
                            srv.api_url,
                            srv.api_token,
                            cfg.inbound_id,
                            cfg.client_uuid,
                            email=cfg.client_email,
                            version=getattr(srv, "xui_version", "v3.5.0"),
                            server_id=srv.id,
                            server_name=srv.name
                        )
                    except Exception:
                        pass
                db.delete(cfg)
                continue

            # --- CASE 2: USER MUST EXIST & MUST BE ACTIVE ---
            # Only restore on 3X-UI if the user has credit AND the config is currently marked active
            if user_has_credit and cfg.is_active:
                # 2A: Check if client was manually deleted from 3X-UI panel directly
                if cfg.client_email not in xui_enabled_map:
                    logger.warning(
                        f"[Reconcile] Client {cfg.client_email} was deleted directly from 3X-UI on {srv.name}! "
                        "Restoring client on 3X-UI immediately..."
                    )
                    sub_id = cfg.client_uuid.replace("-", "")[:16]
                    created = await XUIClient.add_client(
                        api_url=srv.api_url,
                        api_token=srv.api_token,
                        inbound_id=cfg.inbound_id,
                        uuid=cfg.client_uuid,
                        email=cfg.client_email,
                        protocol=cfg.protocol,
                        enable=True,
                        version=getattr(srv, "xui_version", "v3.5.0"),
                        sub_id=sub_id,
                        server_id=srv.id,
                        server_name=srv.name
                    )
                    if created:
                        actual_email = getattr(created, "email", cfg.client_email)
                        actual_uuid = getattr(created, "uuid", cfg.client_uuid)

                        direct_uri = await XUIClient.get_client_uri(
                            api_url=srv.api_url,
                            api_token=srv.api_token,
                            inbound_id=cfg.inbound_id,
                            email=actual_email,
                            client_uuid=actual_uuid,
                            sub_id=actual_uuid.replace("-", "")[:16],
                            version=getattr(srv, "xui_version", "v3.5.0")
                        )
                        if direct_uri:
                            base_link = direct_uri.split("#")[0]
                            cfg.uri = f"{base_link}#{actual_email}"
                        cfg.client_email = actual_email
                        cfg.client_uuid = actual_uuid
                        cfg.is_active = True
                        xui_enabled_map[actual_email] = True
                        logger.info(f"[Reconcile] Successfully restored client {actual_email} on 3X-UI.")

                # 2B: Check if client is present on 3X-UI but disabled
                elif xui_enabled_map.get(cfg.client_email) is False:
                    logger.warning(f"[Reconcile] Client {cfg.client_email} is disabled on 3X-UI but has credit. Re-enabling...")
                    await XUIClient.update_client_status(
                        api_url=srv.api_url,
                        api_token=srv.api_token,
                        inbound_id=cfg.inbound_id,
                        uuid=cfg.client_uuid,
                        email=cfg.client_email,
                        enable=True,
                        version=getattr(srv, "xui_version", "v3.5.0"),
                        server_id=srv.id,
                        server_name=srv.name
                    )
                    cfg.is_active = True
                    xui_enabled_map[cfg.client_email] = True

            # --- CASE 3: USER HAS NO CREDIT OR CONFIG IS DEACTIVATED ---
            else:
                if xui_enabled_map.get(cfg.client_email) is True:
                    logger.info(f"[Reconcile] Config {cfg.client_email} is inactive or user has no credit. Deactivating on 3X-UI...")
                    await XUIClient.update_client_status(
                        api_url=srv.api_url,
                        api_token=srv.api_token,
                        inbound_id=cfg.inbound_id,
                        uuid=cfg.client_uuid,
                        email=cfg.client_email,
                        enable=False,
                        version=getattr(srv, "xui_version", "v3.5.0"),
                        server_id=srv.id,
                        server_name=srv.name
                    )
                    cfg.is_active = False
                    xui_enabled_map[cfg.client_email] = False

            # --- TRAFFIC METERING ---
            current_bytes = traffic_map.get(cfg.client_email)
            if current_bytes is not None:
                last_b = cfg.last_traffic or 0
                delta = current_bytes - last_b if current_bytes >= last_b else current_bytes
                cfg.last_traffic = current_bytes

                connected_ips = await XUIClient.get_client_ips(
                    srv.api_url, srv.api_token, cfg.client_email, version=getattr(srv, "xui_version", "v3.5.0")
                )

                if delta > 0:
                    total_synced_bytes += delta
                    server_delta_bytes += delta

                    event_payload = {
                        "user_id": uid_str,
                        "server_id": str(srv.id),
                        "used_bytes": delta,
                        "price_per_gb": float(srv.price_per_gb),
                        "price_per_gb_usd": float(getattr(srv, "price_per_gb_usd", 0.05)),
                        "client_email": cfg.client_email,
                        "client_ips": connected_ips,
                        "client_ip": connected_ips[0] if connected_ips else ""
                    }

                    await publish_traffic_consumed_event(event_payload)

                server_client_deltas[cfg.client_email] = {
                    "delta_used": f"+{format_bytes_human(delta)}" if delta > 0 else "0 B",
                    "delta_bytes": delta,
                    "current_traffic": format_bytes_human(current_bytes),
                    "current_traffic_bytes": current_bytes,
                    "previous_traffic": format_bytes_human(last_b),
                    "previous_traffic_bytes": last_b,
                    "connected_ips": connected_ips,
                    "is_active": cfg.is_active
                }

                processed_users += 1

        persist_server_log(
            endpoint=f"{srv.api_url.rstrip('/')}/panel/api/inbounds/list",
            method="GET",
            status="Success",
            status_code=status_code or 200,
            response_time_ms=elapsed_ms,
            server_id=srv.id,
            server_name=srv.name,
            event_type="sync_traffic",
            description=f"Batch polled & reconciled {len(cfgs)} client(s) on {srv.name}",
            details={
                "server_name": srv.name,
                "clients_count": len(cfgs),
                "total_new_traffic": format_bytes_human(server_delta_bytes),
                "total_new_traffic_bytes": server_delta_bytes,
                "client_traffic_deltas": server_client_deltas
            }
        )

        db.commit()

    # Persist cumulative total bytes polled across all users and time
    if total_synced_bytes > 0:
        cum_setting = db.query(NodeSettings).filter(NodeSettings.key == "cumulative_total_bytes_polled").first()
        if not cum_setting:
            db.add(NodeSettings(key="cumulative_total_bytes_polled", value=str(total_synced_bytes)))
        else:
            prev_val = int(cum_setting.value or 0)
            cum_setting.value = str(prev_val + total_synced_bytes)
        db.commit()

    # Provision missing user configs for funded accounts across active locations
    active_locations = db.query(Location).filter(Location.is_active == True).all()
    for loc in active_locations:
        await provision_missing_users_for_location(db, loc.id)

    persist_server_log(
        endpoint="/panel/api/clients/traffic",
        method="GET",
        status="Success" if not failed_servers else "Warning",
        description=(
            f"PAYG reconciliation completed. Polled {processed_users} configs "
            f"({total_synced_bytes / (1024**2):.2f} MB new traffic)."
        ),
        event_type="sync_traffic",
        details={
            "total_bytes_synced": total_synced_bytes,
            "processed_users": processed_users,
            "failed_servers": failed_servers
        }
    )

    return {
        "total_bytes_synced": total_synced_bytes,
        "processed_users": processed_users,
        "failed_servers": failed_servers
    }