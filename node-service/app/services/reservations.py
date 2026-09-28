# node-service/app/services/reservations.py

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.models import Server, UserConfig, ConfigReservation, UserBlockedRouteHistory


def expire_old_reservations(db: Session) -> int:
    """
    Marks expired capacity reservations as 'Expired'.

    This is executed within the active database transaction before scanning
    for capacity so that stale reservation holds do not block other clients.
    """
    now = datetime.now(timezone.utc)

    expired_count = (
        db.query(ConfigReservation)
        .filter(
            ConfigReservation.status == "Reserved",
            ConfigReservation.expires_at <= now,
        )
        .update(
            {
                ConfigReservation.status: "Expired",
            },
            synchronize_session=False,
        )
    )

    return expired_count


def find_available_slot_for_region(
    db: Session,
    location_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    exclude_inbound_keys: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """
    Finds an available server/inbound/protocol slot in the requested region.

    Capacity & Slot Occupancy Rules:
    1. Every existing UserConfig holding an inbound slot (whether currently active
       or temporarily disabled during its grace period) is counted as an occupied slot.
       The slot is only freed when the config is permanently deleted from the database
       and 3X-UI after the grace period expires.
    2. Active, unexpired reservations are added to the existing config count.
    3. If total_existing_configs + active_reservations >= user_limit, that inbound
       is considered FULL and unusable.
    4. If user_id is provided, inbounds recently reported as blocked (within 7 days)
       by this user are excluded from consideration.
    """
    expire_old_reservations(db)

    # 1. Compile inbound keys recently blocked by this user in this region
    blocked_keys = set()
    blocked_hosts_by_server: Dict[uuid.UUID, set] = {}
    if user_id:
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        blocked_records = (
            db.query(UserBlockedRouteHistory)
            .filter(
                UserBlockedRouteHistory.user_id == user_id,
                UserBlockedRouteHistory.location_id == location_id,
                UserBlockedRouteHistory.created_at >= seven_days_ago,
            )
            .all()
        )
        blocked_keys = {(h.server_id, h.inbound_id) for h in blocked_records}
        for h in blocked_records:
            if h.host_or_sni:
                h_clean = h.host_or_sni.strip().lower()
                if h.server_id not in blocked_hosts_by_server:
                    blocked_hosts_by_server[h.server_id] = set()
                blocked_hosts_by_server[h.server_id].add(h_clean)

    if exclude_inbound_keys:
        blocked_keys.update(exclude_inbound_keys)

    # 2. Query all active and reachable server nodes in this region
    servers = (
        db.query(Server)
        .filter(
            Server.location_id == location_id,
            Server.is_active == True,
            Server.is_reachable == True,
        )
        .order_by(Server.created_at.asc())
        .all()
    )

    now = datetime.now(timezone.utc)

    for server in servers:
        inbound_limits = (
            server.inbound_limits
            if isinstance(server.inbound_limits, list)
            else []
        )

        if not inbound_limits:
            inbound_limits = [
                {
                    "inbound_id": server.target_inbound_id or 1,
                    "protocol": server.protocol_type or "vless_reality",
                    "user_limit": 100,
                }
            ]

        for limit_item in inbound_limits:
            if not isinstance(limit_item, dict):
                continue

            inbound_id = int(limit_item.get("inbound_id", server.target_inbound_id or 1))
            protocol = str(limit_item.get("protocol", server.protocol_type or "vless_reality")).strip().lower()
            user_limit = max(0, int(limit_item.get("user_limit", 0)))

            if user_limit <= 0:
                continue

            # Skip if this specific inbound on this server was blocked by this user
            if (server.id, inbound_id) in blocked_keys:
                continue

            # 2. Skip if on the same server and uses the same blocked SNI/Host
            cand_host = (limit_item.get("domain_or_ip") or server.domain_or_ip or "").strip().lower()
            server_blocked_hosts = blocked_hosts_by_server.get(server.id, set())
            if cand_host and cand_host in server_blocked_hosts:
                continue

            occupied_configs_count = (
                db.query(UserConfig)
                .filter(
                    UserConfig.server_id == server.id,
                    UserConfig.inbound_id == inbound_id,
                    UserConfig.protocol == protocol,
                )
                .count()
            )

            active_reservations_count = (
                db.query(ConfigReservation)
                .filter(
                    ConfigReservation.server_id == server.id,
                    ConfigReservation.inbound_id == inbound_id,
                    ConfigReservation.protocol == protocol,
                    ConfigReservation.status == "Reserved",
                    ConfigReservation.expires_at > now,
                )
                .count()
            )

            # Slot is available ONLY if occupied slots + reservations are strictly under the inbound limit
            if occupied_configs_count + active_reservations_count < user_limit:
                return {
                    "server": server,
                    "inbound_id": inbound_id,
                    "protocol": protocol,
                    "port": int(limit_item.get("port", server.port or 443)),
                    "domain_or_ip": str(limit_item.get("domain_or_ip") or server.domain_or_ip or ""),
                    "path": limit_item.get("path") or "/",
                    "user_limit": user_limit,
                }

    return None


def reserve_config_slot(
    db: Session,
    user_id: uuid.UUID,
    location_id: uuid.UUID,
    topup_transaction_id: Optional[uuid.UUID] = None,
    ttl_minutes: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Creates an atomic capacity reservation for a client's regional top-up.

    Rules enforced:
    1. Idempotency: If this topup_transaction_id already holds an active reservation, reuse it.
    2. Single Config per Region: If the user already owns a UserConfig for this region,
       returns existing_config=True (no new slot reservation required).
    3. Active Hold Check: If the user already has a pending unexpired reservation for this region,
       returns existing_config=False with the existing reservation.
    4. Capacity Check: Searches and reserves an available slot; returns None if region is full.
    """
    now = datetime.now(timezone.utc)

    # 1. Idempotency check for existing transaction
    if topup_transaction_id:
        existing_tx_reservation = (
            db.query(ConfigReservation)
            .filter(
                ConfigReservation.topup_transaction_id == topup_transaction_id,
                ConfigReservation.status == "Reserved",
                ConfigReservation.expires_at > now,
            )
            .first()
        )
        if existing_tx_reservation:
            return {
                "existing_config": False,
                "reservation": existing_tx_reservation,
            }

    # 2. Single regional config check: If user already holds a config in this region
    existing_config = (
        db.query(UserConfig)
        .filter(
            UserConfig.user_id == user_id,
            UserConfig.location_id == location_id,
        )
        .first()
    )

    if existing_config:
        return {
            "existing_config": True,
            "reservation": None,
        }

    # 3. Prevent duplicate simultaneous reservations by the same user for the same region
    existing_user_reservation = (
        db.query(ConfigReservation)
        .filter(
            ConfigReservation.user_id == user_id,
            ConfigReservation.location_id == location_id,
            ConfigReservation.status == "Reserved",
            ConfigReservation.expires_at > now,
        )
        .first()
    )

    if existing_user_reservation:
        return {
            "existing_config": False,
            "reservation": existing_user_reservation,
        }

    # 4. Find and reserve an available slot
    slot = find_available_slot_for_region(
        db=db,
        location_id=location_id,
        user_id=user_id,
    )

    if not slot:
        return None

    reservation = ConfigReservation(
        user_id=user_id,
        location_id=location_id,
        server_id=slot["server"].id,
        inbound_id=slot["inbound_id"],
        protocol=slot["protocol"],
        status="Reserved",
        expires_at=now + timedelta(minutes=max(1, ttl_minutes)),
        topup_transaction_id=topup_transaction_id,
    )

    db.add(reservation)
    db.flush()

    return {
        "existing_config": False,
        "reservation": reservation,
    }


def release_reservation(
    db: Session,
    reservation_id: uuid.UUID,
) -> bool:
    """
    Releases an active reservation hold when a top-up is rejected or cancelled.
    Returns True if a reservation was found and released, False otherwise.
    """
    reservation = (
        db.query(ConfigReservation)
        .filter(
            ConfigReservation.id == reservation_id,
            ConfigReservation.status == "Reserved",
        )
        .first()
    )

    if not reservation:
        return False

    reservation.status = "Released"
    db.flush()
    return True


def release_reservation_by_transaction(
    db: Session,
    topup_transaction_id: uuid.UUID,
) -> bool:
    """
    Releases any active reservation hold associated with a topup_transaction_id.
    """
    reservation = (
        db.query(ConfigReservation)
        .filter(
            ConfigReservation.topup_transaction_id == topup_transaction_id,
            ConfigReservation.status == "Reserved",
        )
        .first()
    )

    if not reservation:
        return False

    reservation.status = "Released"
    db.flush()
    return True