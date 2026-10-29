# backend/node-service/app/api/servers.py
import uuid
import time
from urllib.parse import urlparse
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
import httpx

from app.database import get_db
from app.models import (
    Server,
    UserConfig,
    ConfigReservation,
    Location,
    ServerLog,
    NodeSettings,
    UserSubscription,
    UserBlockedRouteHistory
)
from app.schemas import (
    LocationCreate,
    LocationUpdate,
    LocationResponse,
    ServerCreate,
    ServerUpdate,
    ServerResponse,
    TestConnectionRequest,
    PublicLocationSummary,
    ManualReassignRequest,
    AdminAddUserConfigRequest,
    RoutePolicySyncRequest
)
from app.services.provisioning import (
    get_user_provisioning_lock,
    build_config_name
)
from app.xui_client import XUIClient
from app.events.publisher import (
    publish_server_changed_event,
    publish_location_capacity_event,
    publish_server_offline_event,
    broker
)
import asyncio
from mng_shared import EventKeys
from mng_shared.jwt_auth import get_current_user_payload
from app.services.provisioning import (
    provision_missing_users_for_location, 
    run_server_health_and_provisioning_sweep
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/servers", tags=["Server & Location Management"])


# =========================================================================
# HELPER FUNCTIONS
# =========================================================================
async def run_background_provision(location_id: uuid.UUID):
    """Executes retroactive user provisioning in an isolated DB session."""
    from app.database import SessionLocal
    from app.services.provisioning import provision_missing_users_for_location
    bg_db = SessionLocal()
    try:
        await provision_missing_users_for_location(bg_db, location_id)
    except Exception as e:
        logger.error(f"Error in background provisioning for location {location_id}: {e}")
    finally:
        bg_db.close()


def verify_admin_role(user: dict):
    role_val = str(user.get("role", "")).capitalize()
    if role_val != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to manage server nodes and locations.",
        )


def record_server_http_log(
    db: Session,
    endpoint: str,
    method: str,
    status: str,
    description: str,
    status_code: Optional[int] = None,
    response_time_ms: Optional[int] = None,
    server_id: Optional[uuid.UUID] = None,
    server_name: Optional[str] = None,
    details: Optional[dict] = None
):
    """Helper to record actual HTTP calls to 3x-ui panels into server_logs table."""
    try:
        log_entry = ServerLog(
            id=uuid.uuid4(),
            server_id=server_id,
            server_name=server_name or "3x-ui Node",
            endpoint=endpoint,
            method=method.upper(),
            status_code=status_code,
            response_time_ms=response_time_ms,
            status=status,
            description=description,
            details=details or {},
            event_type="http_call" if status == "Success" else "api_error"
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to record server log: {e}")


def get_location_capacity_metrics(db: Session, loc: Location) -> Dict[str, Any]:
    """
    Computes total capacity, used capacity, and availability for a region
    by counting all assigned user configs holding an inbound slot.
    """
    now = datetime.now(timezone.utc)
    servers = (
        db.query(Server)
        .filter(Server.location_id == loc.id, Server.is_active == True)
        .all()
    )

    total_capacity = 0
    used_capacity = 0
    has_at_least_one_free_slot = False

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

            total_capacity += user_limit

            # Count all configs holding this slot (enabled or in grace period)
            occupied_cfgs = (
                db.query(UserConfig)
                .filter(
                    UserConfig.server_id == server.id,
                    UserConfig.inbound_id == inbound_id,
                    UserConfig.protocol == protocol,
                )
                .count()
            )

            active_reservations = (
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

            used_this = min(user_limit, occupied_cfgs + active_reservations)
            used_capacity += used_this

            if occupied_cfgs + active_reservations < user_limit:
                has_at_least_one_free_slot = True

    is_full = (total_capacity == 0) or (not has_at_least_one_free_slot)
    available_capacity = max(0, total_capacity - used_capacity)

    return {
        "total_capacity": total_capacity,
        "used_capacity": used_capacity,
        "available_capacity": available_capacity,
        "is_full": is_full,
    }


async def sync_and_publish_location_capacity(db: Session, loc: Location):
    metrics = get_location_capacity_metrics(db, loc)
    await publish_location_capacity_event(
        location_id=str(loc.id),
        location_name=loc.name,
        is_full=metrics["is_full"],
        total_capacity=metrics["total_capacity"],
        used_capacity=metrics["used_capacity"],
    )


def enrich_server_with_usage(db: Session, server: Server) -> dict:
    server_data = {
        "id": server.id,
        "location_id": server.location_id,
        "name": server.name,
        "api_url": server.api_url,
        "api_token": server.api_token or "",
        "domain_or_ip": server.domain_or_ip,
        "port": server.port,
        "protocol_type": server.protocol_type,
        "target_inbound_id": server.target_inbound_id,
        "price_per_gb": server.price_per_gb,
        "price_per_gb_usd": getattr(server, "price_per_gb_usd", 0.05) if getattr(server, "price_per_gb_usd", None) is not None else 0.05,
        "is_active": server.is_active,
        "is_reachable": getattr(server, "is_reachable", True),
        "last_error": getattr(server, "last_error", None),
        "created_at": server.created_at,
        "updated_at": server.updated_at,
    }
    
    limits = server.inbound_limits if isinstance(server.inbound_limits, list) else []
    enriched_limits = []
    now = datetime.now(timezone.utc)

    for lim in limits:
        if not isinstance(lim, dict):
            continue

        inb_id = int(lim.get("inbound_id", server.target_inbound_id or 1))
        proto = str(lim.get("protocol", server.protocol_type or "vless_reality")).strip().lower()
        u_limit = max(0, int(lim.get("user_limit", 0)))
        
        # Count all existing configs holding this inbound slot
        occupied_cfgs = db.query(UserConfig).filter(
            UserConfig.server_id == server.id,
            UserConfig.inbound_id == inb_id,
            UserConfig.protocol == proto,
        ).count()

        active_res = db.query(ConfigReservation).filter(
            ConfigReservation.server_id == server.id,
            ConfigReservation.inbound_id == inb_id,
            ConfigReservation.protocol == proto,
            ConfigReservation.status == "Reserved",
            ConfigReservation.expires_at > now,
        ).count()
        
        used = occupied_cfgs + active_res
        
        enriched_limits.append({
            "inbound_id": inb_id,
            "tag": lim.get("tag") or f"{proto.upper()} Port {lim.get('port', server.port)}",
            "protocol": proto,
            "port": int(lim.get("port", server.port)),
            "domain_or_ip": lim.get("domain_or_ip") or "",
            "path": lim.get("path") or "/",
            "user_limit": u_limit,
            "used_limit": used
        })
        
    server_data["inbound_limits"] = enriched_limits
    return server_data


# =========================================================================
# 1. LOCATION / REGION ENDPOINTS (MUST BE BEFORE /{server_id})
# =========================================================================

@router.get("/locations-public", response_model=List[PublicLocationSummary])
def get_public_locations_sanitized(db: Session = Depends(get_db)):
    """
    Zero-Leakage Public Read Model:
    Returns high-level regional availability without leaking server counts,
    capacities, node IPs, or internal fleet topology.
    """
    locations = (
        db.query(Location)
        .filter(Location.is_active == True)
        .order_by(Location.name.asc())
        .all()
    )

    results = []
    for loc in locations:
        active_servers = (
            db.query(Server)
            .filter(
                Server.location_id == loc.id,
                Server.is_active == True,
                Server.is_reachable == True
            )
            .all()
        )

        has_active_nodes = len(active_servers) > 0

        min_irt = min((s.price_per_gb for s in active_servers), default=1000.0)
        min_usd = min((getattr(s, "price_per_gb_usd", 0.05) for s in active_servers), default=0.05)

        results.append(
            PublicLocationSummary(
                name=loc.name,
                country_code=loc.country_code,
                flag=loc.flag,
                status="Available" if has_active_nodes else "Maintenance",
                base_rate_display=f"From ~{int(min_irt):,} T / GB",
                base_rate_usd_display=f"From ~${min_usd:.2f} / GB"
            )
        )

    return results


@router.get("/locations", response_model=List[LocationResponse])
def list_locations(db: Session = Depends(get_db)):
    """Public availability read-model for all active locations."""
    locations = (
        db.query(Location)
        .filter(Location.is_active == True)
        .order_by(Location.name.asc())
        .all()
    )

    results = []
    for loc in locations:
        metrics = get_location_capacity_metrics(db, loc)
        results.append(
            LocationResponse(
                id=loc.id,
                name=loc.name,
                country_code=loc.country_code,
                flag=loc.flag,
                is_active=loc.is_active,
                total_capacity=metrics["total_capacity"],
                used_capacity=metrics["used_capacity"],
                is_full=metrics["is_full"],
                created_at=loc.created_at,
                updated_at=loc.updated_at,
            )
        )

    return results


@router.get("/locations/{location_id}/check-availability")
def check_location_availability(
    location_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Returns current regional capacity and availability status."""
    location = (
        db.query(Location)
        .filter(Location.id == location_id, Location.is_active == True)
        .first()
    )

    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location region not found.",
        )

    metrics = get_location_capacity_metrics(db, location)
    return {
        "location_id": str(location_id),
        "total_capacity": metrics["total_capacity"],
        "used_capacity": metrics["used_capacity"],
        "available_capacity": metrics["available_capacity"],
        "is_full": metrics["is_full"],
        "available": not metrics["is_full"],
    }


@router.get("/locations/{location_id}", response_model=LocationResponse)
def get_location_detail(
    location_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Retrieves details of a specific location region."""
    loc = db.query(Location).filter(Location.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location region not found.")

    metrics = get_location_capacity_metrics(db, loc)
    return LocationResponse(
        id=loc.id,
        name=loc.name,
        country_code=loc.country_code,
        flag=loc.flag,
        is_active=loc.is_active,
        total_capacity=metrics["total_capacity"],
        used_capacity=metrics["used_capacity"],
        is_full=metrics["is_full"],
        created_at=loc.created_at,
        updated_at=loc.updated_at,
    )


@router.post("/locations", response_model=LocationResponse, status_code=status.HTTP_201_CREATED)
async def create_location(
    loc_in: LocationCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload),
):
    """Admin creates a new Location region."""
    verify_admin_role(user)

    existing = db.query(Location).filter(
        Location.country_code == loc_in.country_code.strip().upper()
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Location with country code '{loc_in.country_code}' already exists.",
        )

    loc = Location(
        id=uuid.uuid4(),
        name=loc_in.name.strip(),
        country_code=loc_in.country_code.strip().upper(),
        flag=loc_in.flag.strip(),
        is_active=True,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)

    await sync_and_publish_location_capacity(db, loc)

    return LocationResponse(
        id=loc.id,
        name=loc.name,
        country_code=loc.country_code,
        flag=loc.flag,
        is_active=loc.is_active,
        total_capacity=0,
        used_capacity=0,
        is_full=True,
        created_at=loc.created_at,
        updated_at=loc.updated_at,
    )


@router.put("/locations/{location_id}", response_model=LocationResponse)
async def update_location(
    location_id: uuid.UUID,
    loc_update: LocationUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload),
):
    """Admin updates location details."""
    verify_admin_role(user)

    loc = db.query(Location).filter(Location.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location region not found.")

    if loc_update.name is not None:
        loc.name = loc_update.name.strip()
    if loc_update.country_code is not None:
        loc.country_code = loc_update.country_code.strip().upper()
    if loc_update.flag is not None:
        loc.flag = loc_update.flag.strip()
    if loc_update.is_active is not None:
        loc.is_active = loc_update.is_active

    db.commit()
    db.refresh(loc)

    await sync_and_publish_location_capacity(db, loc)
    metrics = get_location_capacity_metrics(db, loc)

    return LocationResponse(
        id=loc.id,
        name=loc.name,
        country_code=loc.country_code,
        flag=loc.flag,
        is_active=loc.is_active,
        total_capacity=metrics["total_capacity"],
        used_capacity=metrics["used_capacity"],
        is_full=metrics["is_full"],
        created_at=loc.created_at,
        updated_at=loc.updated_at,
    )


@router.delete("/locations/{location_id}")
async def delete_location(
    location_id: uuid.UUID,
    force: bool = Query(False, description="Force delete all servers and configs in this region"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload),
):
    """
    Admin deletes a location region.
    - Cleans up all client configs from each 3X-UI panel in that region via API.
    - Deletes servers and configurations for that location from node_db.
    - All historical traffic usage, top-up records, and ISP telemetry are preserved.
    """
    verify_admin_role(user)

    loc = db.query(Location).filter(Location.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location region not found.")

    assigned_servers = db.query(Server).filter(Server.location_id == location_id).all()
    assigned_server_count = len(assigned_servers)

    if assigned_server_count > 0 and not force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot remove '{loc.name}'. It currently has {assigned_server_count} server node(s). "
                "Pass force=true to delete the region and its servers."
            ),
        )

    # Clean up 3X-UI panels for each server in this location
    total_configs_cleaned = 0
    for srv in assigned_servers:
        if getattr(srv, "is_reachable", True):
            # A. Remove known UserConfigs
            cfgs = db.query(UserConfig).filter(UserConfig.server_id == srv.id).all()
            for cfg in cfgs:
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
                    total_configs_cleaned += 1
                except Exception:
                    pass

            # B. Sweep to remove any replaced or orphaned clients on the panel
            try:
                cleaned_extra = await XUIClient.sweep_and_remove_all_payg_clients(
                    srv.api_url,
                    srv.api_token,
                    server_id=srv.id,
                    server_name=srv.name,
                    sweep_all_payg=True
                )
                total_configs_cleaned += cleaned_extra
            except Exception:
                pass

    # Delete local node_db records for this location (does NOT touch billing_db or traffic_db)
    db.query(ConfigReservation).filter(ConfigReservation.location_id == location_id).delete(synchronize_session=False)
    db.query(UserBlockedRouteHistory).filter(UserBlockedRouteHistory.location_id == location_id).delete(synchronize_session=False)
    db.query(UserConfig).filter(UserConfig.location_id == location_id).delete(synchronize_session=False)
    db.query(Server).filter(Server.location_id == location_id).delete(synchronize_session=False)

    loc_name = loc.name
    db.delete(loc)
    db.commit()

    logger.info(f"Force-deleted location '{loc_name}' ({assigned_server_count} servers, {total_configs_cleaned} panel configs cleaned).")

    return {
        "message": f"Region '{loc_name}' and its {assigned_server_count} server(s) were removed successfully. All historical traffic and billing telemetry remain intact.",
        "cleaned_configs_count": total_configs_cleaned,
    }


# ==========================================
# 2. STATIC SERVER OPERATIONS (MUST BE DEFINED BEFORE /{server_id})
# ==========================================

@router.get("/sync-telemetry")
def get_sync_telemetry(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    verify_admin_role(user)

    last_sync_log = db.query(ServerLog).filter(
        ServerLog.event_type.in_(["sync_traffic", "health_check"])
    ).order_by(ServerLog.created_at.desc()).first()

    # Query persistent cumulative counter saved in node_settings
    cum_setting = db.query(NodeSettings).filter(NodeSettings.key == "cumulative_total_bytes_polled").first()
    cum_bytes = int(cum_setting.value) if (cum_setting and cum_setting.value) else 0

    # Also sum active user configs
    current_cfg_bytes = db.query(func.sum(UserConfig.last_traffic)).scalar() or 0
    total_bytes = max(cum_bytes, int(current_cfg_bytes))

    active_servers = db.query(Server).filter(Server.is_active == True).count()
    active_configs = db.query(UserConfig).filter(UserConfig.is_active == True).count()

    return {
        "last_synced_at": last_sync_log.created_at.isoformat() if last_sync_log else None,
        "total_bytes_processed": total_bytes,
        "active_servers_count": active_servers,
        "active_configs_count": active_configs,
        "is_active": True
    }


@router.post("/sync-traffic")
async def force_sync_traffic(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    verify_admin_role(user)
    result = await run_server_health_and_provisioning_sweep(db)
    
    total_mb = (result.get("total_bytes_synced", 0) / (1024 * 1024))
    processed = result.get("processed_users", 0)
    failed = result.get("failed_servers", [])

    return {
        "status": "success" if not failed else "warning",
        "message": f"Synchronized {processed} configs. New traffic: {total_mb:.2f} MB.",
        "total_bytes_synced": result.get("total_bytes_synced", 0),
        "processed_users": processed,
        "failed_count": len(failed)
    }


@router.get("/logs")
def get_server_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    verify_admin_role(user)

    query = db.query(ServerLog)

    if status and status.lower() != "all":
        query = query.filter(ServerLog.status.ilike(status.strip()))

    if event_type and event_type.lower() != "all":
        query = query.filter(ServerLog.event_type.ilike(event_type.strip()))

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                ServerLog.server_name.ilike(term),
                ServerLog.endpoint.ilike(term),
                ServerLog.description.ilike(term)
            )
        )

    total = query.count()
    total_pages = max(1, (total + limit - 1) // limit)
    offset = (page - 1) * limit

    items = query.order_by(ServerLog.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages
    }


@router.post("/internal/policy-sync")
def sync_route_policy_internal(
    req: RoutePolicySyncRequest,
    db: Session = Depends(get_db)
):
    """Directly synchronizes route replacement policies into node_db."""
    from app.models import NodeSettings

    if req.route_replace_cooldown_days is not None:
        cd_row = db.query(NodeSettings).filter(NodeSettings.key == "route_replace_cooldown_days").first()
        if not cd_row:
            db.add(NodeSettings(key="route_replace_cooldown_days", value=str(req.route_replace_cooldown_days)))
        else:
            cd_row.value = str(req.route_replace_cooldown_days)

    if req.disable_client_route_replacement is not None:
        dis_val = "true" if req.disable_client_route_replacement else "false"
        dis_row = db.query(NodeSettings).filter(NodeSettings.key == "disable_client_route_replacement").first()
        if not dis_row:
            db.add(NodeSettings(key="disable_client_route_replacement", value=dis_val))
        else:
            dis_row.value = dis_val

    if req.xui_request_timeout is not None:
        timeout_val = max(1.0, float(req.xui_request_timeout))
        t_row = db.query(NodeSettings).filter(NodeSettings.key == "xui_request_timeout").first()
        if not t_row:
            db.add(NodeSettings(key="xui_request_timeout", value=str(timeout_val)))
        else:
            t_row.value = str(timeout_val)
        XUIClient.set_timeout(timeout_val)

    db.commit()
    return {"status": "success", "synced": req.dict() if hasattr(req, "dict") else req.model_dump()}


@router.post("/test-connection")
async def test_server_connection(
    req: TestConnectionRequest,
    user: dict = Depends(get_current_user_payload),
):
    verify_admin_role(user)

    clean_url = req.api_url.rstrip("/").strip()
    is_reachable, err = await XUIClient.check_server_reachable(clean_url, req.api_token.strip())

    if is_reachable:
        return {
            "status": "success",
            "message": "Successfully connected to 3x-ui panel API.",
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to connect to 3x-ui panel: {err or 'Connection refused / timed out'}",
        )


@router.post("/detect-inbounds")
async def detect_server_inbounds(
    req: TestConnectionRequest,
    user: dict = Depends(get_current_user_payload),
):
    verify_admin_role(user)

    inbounds = await XUIClient.fetch_inbounds_list(
        api_url=req.api_url.strip(),
        api_token=req.api_token.strip()
    )

    if not inbounds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not detect active inbounds. Verify that the 3x-ui URL and token are correct and at least one inbound is enabled."
        )

    return {"status": "success", "inbounds": inbounds}


# ==========================================
# 3. BASE SERVER LISTING & CREATION
# ==========================================

@router.get("", response_model=List[ServerResponse])
@router.get("/", response_model=List[ServerResponse])
def list_servers(
    location_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload),
):
    verify_admin_role(user)
    query = db.query(Server)
    if location_id:
        query = query.filter(Server.location_id == location_id)
    servers = query.order_by(Server.created_at.desc()).all()
    
    return [enrich_server_with_usage(db, s) for s in servers]


@router.post("", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(
    server_in: ServerCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload),
):
    verify_admin_role(user)

    clean_url = server_in.api_url.rstrip("/").strip()
    parsed_host = urlparse(clean_url).hostname or ""
    
    inbound_limits_data = [item.model_dump() for item in server_in.inbound_limits]
    primary_inbound = inbound_limits_data[0] if inbound_limits_data else {}

    domain_or_ip = (
        server_in.domain_or_ip.strip() 
        if server_in.domain_or_ip and server_in.domain_or_ip.strip() 
        else (primary_inbound.get("domain_or_ip") or parsed_host)
    )
    port = server_in.port or primary_inbound.get("port") or 443
    target_inbound_id = server_in.target_inbound_id or primary_inbound.get("inbound_id") or 1
    protocol_type = server_in.protocol_type or primary_inbound.get("protocol") or "vless"

    new_server = Server(
        id=uuid.uuid4(),
        name=server_in.name.strip(),
        location_id=server_in.location_id,
        api_url=clean_url,
        api_token=server_in.api_token.strip(),
        domain_or_ip=domain_or_ip,
        port=port,
        protocol_type=protocol_type.lower().strip(),
        target_inbound_id=target_inbound_id,
        inbound_limits=inbound_limits_data,
        price_per_gb=server_in.price_per_gb,
        price_per_gb_usd=server_in.price_per_gb_usd if server_in.price_per_gb_usd is not None else 0.05,
        is_active=True,
    )
    db.add(new_server)
    db.commit()
    db.refresh(new_server)

    if new_server.location_id and new_server.is_active:
        asyncio.create_task(run_background_provision(new_server.location_id))

    if new_server.location_id:
        loc = db.query(Location).filter(Location.id == new_server.location_id).first()
        if loc:
            await sync_and_publish_location_capacity(db, loc)

    await publish_server_changed_event(
        user_id=None,
        server_name=new_server.name,
        action="created",
    )

    return enrich_server_with_usage(db, new_server)


# ==========================================
# 4. PARAMETERIZED SERVER ROUTES (/{server_id})
# ==========================================

@router.get("/{server_id}", response_model=ServerResponse)
def get_server_detail(
    server_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload),
):
    verify_admin_role(user)
    server = db.query(Server).filter(Server.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server node not found.")
    
    return enrich_server_with_usage(db, server)


@router.put("/{server_id}", response_model=ServerResponse)
async def update_server(
    server_id: uuid.UUID,
    server_update: ServerUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload),
):
    verify_admin_role(user)

    server = db.query(Server).filter(Server.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server node not found.")

    old_location_id = server.location_id

    if server_update.name is not None:
        server.name = server_update.name.strip()
    if server_update.location_id is not None:
        server.location_id = server_update.location_id
    if server_update.xui_version is not None:
        server.xui_version = server_update.xui_version.strip()
    if server_update.api_url is not None:
        server.api_url = server_update.api_url.rstrip("/").strip()
    if server_update.api_token is not None:
        server.api_token = server_update.api_token.strip()
    if server_update.inbound_limits is not None:
        server.inbound_limits = [item.model_dump() for item in server_update.inbound_limits]
    if server_update.price_per_gb is not None:
        server.price_per_gb = server_update.price_per_gb
    if server_update.price_per_gb_usd is not None:
        server.price_per_gb_usd = server_update.price_per_gb_usd
    if server_update.is_active is not None:
        server.is_active = server_update.is_active

    db.commit()
    db.refresh(server)
    logger.info(f"Updated server node '{server.name}'.")

    # Broadcast updated capacity & auto-provision waiting users
    affected_loc_ids = {old_location_id, server.location_id}
    for loc_id in affected_loc_ids:
        if loc_id:
            loc = db.query(Location).filter(Location.id == loc_id).first()
            if loc:
                await sync_and_publish_location_capacity(db, loc)
                asyncio.create_task(run_background_provision(loc_id))

    await publish_server_changed_event(
        user_id=None,
        server_name=server.name,
        action="updated",
    )

    return enrich_server_with_usage(db, server)


@router.delete("/{server_id}")
async def delete_server(
    server_id: uuid.UUID,
    force: bool = Query(False, description="Force delete server, migrating users or setting them to wait for new servers"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload),
):
    """
    Admin deletes a server node.
    - Cleans up client configs on the 3X-UI panel.
    - Migrates users to another active server in that region if capacity exists.
    - If no server in that region has capacity, sets users to wait for new servers.
    - All traffic usage and billing telemetry remain intact.
    """
    verify_admin_role(user)

    server = db.query(Server).filter(Server.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server node not found.")

    configs_on_server = db.query(UserConfig).filter(UserConfig.server_id == server_id).all()
    active_configs_count = len(configs_on_server)
    is_reachable = getattr(server, "is_reachable", True)

    if not force and is_reachable and active_configs_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete server '{server.name}'. It currently hosts {active_configs_count} "
                "active configuration(s). Pass ?force=true to migrate users or queue them for new servers."
            ),
        )

    location_id = server.location_id
    server_name = server.name
    migrated_count = 0
    waiting_count = 0

    # 1. Clean up client configs from the 3X-UI panel
    if is_reachable:
        known_emails = [cfg.client_email for cfg in configs_on_server if cfg.client_email]
        known_uuids = [cfg.client_uuid for cfg in configs_on_server if cfg.client_uuid]

        try:
            blocked_records = db.query(UserBlockedRouteHistory).filter(
                UserBlockedRouteHistory.server_id == server.id
            ).all()
            for b in blocked_records:
                if getattr(b, "client_email", None):
                    known_emails.append(b.client_email)
                if getattr(b, "client_uuid", None):
                    known_uuids.append(b.client_uuid)
        except Exception:
            pass

        # A. Remove all known current configs
        for cfg in configs_on_server:
            try:
                await XUIClient.remove_client(
                    server.api_url,
                    server.api_token,
                    cfg.inbound_id,
                    cfg.client_uuid,
                    email=cfg.client_email,
                    version=getattr(server, "xui_version", "v3.5.0"),
                    server_id=server.id,
                    server_name=server.name
                )
            except Exception:
                pass

        # B. Direct Panel Sweep: query 3X-UI inbounds and delete ANY matching or replaced client
        try:
            await XUIClient.sweep_and_remove_all_payg_clients(
                server.api_url,
                server.api_token,
                known_client_emails=known_emails,
                known_client_uuids=known_uuids,
                server_id=server.id,
                server_name=server.name,
                sweep_all_payg=True
            )
        except Exception as sweep_err:
            logger.warning(f"Error during panel sweep on {server.name}: {sweep_err}")

    # 2. Migrate active users to other servers in this region if available
    migrated_users_info: List[tuple] = []
    if location_id:
        from app.services.reservations import find_available_slot_for_region
        from app.services.provisioning import build_config_name

        for cfg in configs_on_server:
            sub = db.query(UserSubscription).filter(UserSubscription.user_id == cfg.user_id).first()
            user_is_funded = bool(
                sub 
                and sub.has_credit 
                and not getattr(sub, "is_admin_suspended", False) 
                and not getattr(sub, "is_self_suspended", False)
            )

            slot = find_available_slot_for_region(
                db, 
                location_id, 
                user_id=cfg.user_id,
                exclude_inbound_keys={(server.id, cfg.inbound_id)}
            )

            # Target server MUST be a distinct server with a DIFFERENT api_url
            if (
                slot 
                and slot["server"].id != server.id 
                and slot["server"].api_url.rstrip("/") != server.api_url.rstrip("/") 
                and user_is_funded
            ):
                target_server = slot["server"]
                client_uuid = str(uuid.uuid4())
                sub_id = client_uuid.replace("-", "")[:16]
                short_id = client_uuid.replace("-", "")[:8]

                raw_username = sub.username or f"usr_{str(cfg.user_id)[:8]}"
                loc = db.query(Location).filter(Location.id == location_id).first()
                loc_name = loc.name if loc else "Region"
                config_name = build_config_name(raw_username, loc_name, client_uuid)
                client_email = config_name

                created = await XUIClient.add_client(
                    api_url=target_server.api_url,
                    api_token=target_server.api_token,
                    inbound_id=slot["inbound_id"],
                    uuid=client_uuid,
                    email=client_email,
                    protocol=slot["protocol"],
                    enable=True,
                    version=getattr(target_server, "xui_version", "v3.5.0"),
                    sub_id=sub_id,
                    server_id=target_server.id,
                    server_name=target_server.name
                )

                if created:
                    actual_email = getattr(created, "email", client_email)
                    actual_uuid = getattr(created, "uuid", client_uuid)

                    direct_uri = await XUIClient.get_client_uri(
                        api_url=target_server.api_url,
                        api_token=target_server.api_token,
                        inbound_id=slot["inbound_id"],
                        email=actual_email,
                        client_uuid=actual_uuid,
                        sub_id=actual_uuid.replace("-", "")[:16],
                        version=getattr(target_server, "xui_version", "v3.5.0")
                    )
                    if direct_uri:
                        base_link = direct_uri.split("#")[0]
                        direct_uri = f"{base_link}#{actual_email}"

                    cfg.server_id = target_server.id
                    cfg.inbound_id = slot["inbound_id"]
                    cfg.protocol = slot["protocol"]
                    cfg.client_uuid = actual_uuid
                    cfg.client_email = actual_email
                    cfg.uri = direct_uri
                    cfg.is_active = True
                    cfg.last_traffic = 0
                    migrated_count += 1
                    migrated_users_info.append((str(cfg.user_id), loc_name, target_server.name))
                    continue

            waiting_count += 1

    # 3. Delete the server from the database
    db.delete(server)
    db.commit()

    # Notify each migrated user to refresh their subscription
    for m_uid, m_loc, m_srv in migrated_users_info:
        await publish_server_changed_event(
            user_id=m_uid,
            server_name=m_srv,
            action="migrated",
            location_name=m_loc,
            message=f"Server '{server_name}' was removed. Your route for '{m_loc}' was migrated to '{m_srv}'."
        )

    logger.info(
        f"Deleted server '{server_name}' from 3X-UI and DB: {migrated_count} users migrated, "
        f"{waiting_count} users waiting for new servers."
    )

    if location_id:
        loc = db.query(Location).filter(Location.id == location_id).first()
        if loc:
            await sync_and_publish_location_capacity(db, loc)

    await publish_server_changed_event(
        user_id=None,
        server_name=server_name,
        action="deleted",
    )

    msg = f"Server '{server_name}' deleted from 3X-UI and system."
    if migrated_count > 0:
        msg += f" {migrated_count} user(s) moved to other working server(s)."
    if waiting_count > 0:
        msg += f" {waiting_count} user(s) are waiting for new server nodes."

    return {
        "message": msg,
        "migrated_count": migrated_count,
        "waiting_count": waiting_count,
    }


# =========================================================================
# USER ROUTES TELEMETRY & MANUAL REASSIGNMENT
# =========================================================================

@router.get("/users/{target_user_id}/routes")
def get_user_routes_and_history(
    target_user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Returns:
    1. active_configs: in-use configs with server, inbound, protocol, and host/SNI.
    2. blocked_history: past blocked/replaced routes.
    3. available_servers: active and reachable servers with inbounds for manual reassignment.
    """
    verify_admin_role(user_payload)

    # 1. Active in-use configs
    active_cfgs = db.query(UserConfig).filter(
        UserConfig.user_id == target_user_id
    ).all()

    active_list = []
    for cfg in active_cfgs:
        srv = cfg.server
        loc = cfg.location or (srv.location if srv else None)
        
        inb_tag = f"Inbound #{cfg.inbound_id}"
        host_sni = ""
        port = getattr(srv, "port", 443)

        if srv and isinstance(srv.inbound_limits, list):
            for lim in srv.inbound_limits:
                if isinstance(lim, dict) and int(lim.get("inbound_id", 0)) == cfg.inbound_id:
                    inb_tag = lim.get("tag") or inb_tag
                    host_sni = lim.get("domain_or_ip") or ""
                    port = lim.get("port") or port
                    break
        if not host_sni and srv:
            host_sni = srv.domain_or_ip or ""

        active_list.append({
            "id": str(cfg.id),
            "location_id": str(cfg.location_id),
            "location_name": loc.name if loc else "Unknown Region",
            "location_flag": loc.flag if loc else "🌐",
            "location_code": loc.country_code if loc else "N/A",
            "server_id": str(cfg.server_id),
            "server_name": srv.name if srv else "Unknown Server",
            "server_ip": srv.domain_or_ip if srv else "",
            "inbound_id": cfg.inbound_id,
            "inbound_tag": inb_tag,
            "protocol": cfg.protocol.upper(),
            "port": port,
            "host_or_sni": host_sni,
            "client_email": cfg.client_email,
            "uri": cfg.uri or "",
            "data_used_bytes": int(cfg.last_traffic or 0),
            "is_active": cfg.is_active,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None
        })

    # 2. Blocked & replaced routes history
    history_records = db.query(UserBlockedRouteHistory).filter(
        UserBlockedRouteHistory.user_id == target_user_id
    ).order_by(UserBlockedRouteHistory.created_at.desc()).all()

    history_list = []
    for h in history_records:
        srv = h.server
        loc = h.location or (srv.location if srv else None)
        history_list.append({
            "id": str(h.id),
            "location_id": str(h.location_id),
            "location_name": loc.name if loc else "Unknown Region",
            "location_flag": loc.flag if loc else "🌐",
            "server_id": str(h.server_id),
            "server_name": srv.name if srv else "Old Server",
            "inbound_id": h.inbound_id,
            "protocol": h.protocol.upper(),
            "host_or_sni": h.host_or_sni or "N/A",
            "client_email": h.client_email or "",
            "is_deleted_from_panel": h.is_deleted_from_panel,
            "blocked_at": h.created_at.isoformat() if h.created_at else None
        })

    # 3. Available active servers and inbounds
    servers = db.query(Server).filter(
        Server.is_active == True,
        Server.is_reachable == True
    ).order_by(Server.name.asc()).all()

    available_servers = []
    for s in servers:
        loc = s.location
        inbounds = []
        limits = s.inbound_limits if isinstance(s.inbound_limits, list) else []
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            inb_id = int(lim.get("inbound_id", 1))
            inbounds.append({
                "inbound_id": inb_id,
                "tag": lim.get("tag") or f"Inbound #{inb_id}",
                "protocol": (lim.get("protocol") or "vless_reality").lower(),
                "port": int(lim.get("port", s.port or 443)),
                "domain_or_ip": lim.get("domain_or_ip") or s.domain_or_ip or "",
                "user_limit": int(lim.get("user_limit", 10))
            })

        available_servers.append({
            "id": str(s.id),
            "name": s.name,
            "location_id": str(s.location_id) if s.location_id else None,
            "location_name": loc.name if loc else "Unassigned",
            "location_flag": loc.flag if loc else "🌐",
            "inbounds": inbounds
        })

    return {
        "active_configs": active_list,
        "blocked_history": history_list,
        "available_servers": available_servers
    }


@router.post("/users/{target_user_id}/manual-reassign")
async def admin_manual_reassign_route(
    target_user_id: uuid.UUID,
    reassign_in: ManualReassignRequest,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin manually reassigns a specific configuration (or location route) to a new server and inbound.
    """
    verify_admin_role(user_payload)

    async with get_user_provisioning_lock(target_user_id):
        target_server = db.query(Server).filter(
            Server.id == reassign_in.server_id,
            Server.is_active == True,
            Server.is_reachable == True
        ).first()

        if not target_server:
            raise HTTPException(status_code=400, detail="Target server is offline or not reachable.")

        limits = target_server.inbound_limits if isinstance(target_server.inbound_limits, list) else []
        chosen_inb = next((lim for lim in limits if int(lim.get("inbound_id", 0)) == reassign_in.inbound_id), None)

        target_protocol = reassign_in.protocol or (chosen_inb.get("protocol") if chosen_inb else target_server.protocol_type) or "vless_reality"
        target_host = chosen_inb.get("domain_or_ip") if chosen_inb else target_server.domain_or_ip

        # Find the specific config to reassign
        query = db.query(UserConfig).filter(UserConfig.user_id == target_user_id)
        if reassign_in.config_id:
            query = query.filter(UserConfig.id == reassign_in.config_id)
        else:
            query = query.filter(UserConfig.location_id == reassign_in.location_id)
        user_cfg = query.first()

        sub = db.query(UserSubscription).filter(UserSubscription.user_id == target_user_id).first()
        raw_username = sub.username if sub and sub.username else f"usr_{str(target_user_id)[:8]}"

        loc = db.query(Location).filter(Location.id == reassign_in.location_id).first()
        loc_name = loc.name if loc else "Region"

        # Record old route in history if present
        if user_cfg:
            old_srv = user_cfg.server
            db.add(UserBlockedRouteHistory(
                id=uuid.uuid4(),
                user_id=target_user_id,
                location_id=reassign_in.location_id,
                server_id=user_cfg.server_id,
                inbound_id=user_cfg.inbound_id,
                protocol=user_cfg.protocol,
                host_or_sni=user_cfg.server.domain_or_ip if user_cfg.server else None,
                client_uuid=user_cfg.client_uuid,
                client_email=user_cfg.client_email,
                is_deleted_from_panel=False
            ))

            if old_srv and getattr(old_srv, "is_reachable", True):
                try:
                    await XUIClient.remove_client(
                        old_srv.api_url,
                        old_srv.api_token,
                        user_cfg.inbound_id,
                        user_cfg.client_uuid,
                        email=user_cfg.client_email,
                        version=getattr(old_srv, "xui_version", "v3.5.0")
                    )
                except Exception:
                    pass

        # 3. Provision new client on target server
        new_uuid = str(uuid.uuid4())
        short_id = new_uuid.replace("-", "")[:8]
        sub_id = new_uuid.replace("-", "")[:16]
        config_name = build_config_name(raw_username, loc_name, new_uuid)
        new_client_email = config_name

        user_is_funded = bool(sub and sub.has_credit and not sub.is_admin_suspended and not sub.is_self_suspended)

        created = await XUIClient.add_client(
            api_url=target_server.api_url,
            api_token=target_server.api_token,
            inbound_id=reassign_in.inbound_id,
            uuid=new_uuid,
            email=new_client_email,
            protocol=target_protocol,
            enable=user_is_funded,
            version=getattr(target_server, "xui_version", "v3.5.0"),
            sub_id=sub_id,
            server_id=target_server.id,
            server_name=target_server.name
        )

        if not created:
            err_detail = getattr(created, "error_msg", None) or "Failed to add client to target 3X-UI server panel."
            raise HTTPException(status_code=502, detail=f"Failed to add client to target 3X-UI: {err_detail}")

        actual_email = getattr(created, "email", new_client_email)
        actual_uuid = getattr(created, "uuid", new_uuid)

        direct_uri = await XUIClient.get_client_uri(
            api_url=target_server.api_url,
            api_token=target_server.api_token,
            inbound_id=reassign_in.inbound_id,
            email=actual_email,
            client_uuid=actual_uuid,
            sub_id=actual_uuid.replace("-", "")[:16],
            version=getattr(target_server, "xui_version", "v3.5.0")
        )

        if direct_uri:
            base_link = direct_uri.split("#")[0]
            direct_uri = f"{base_link}#{actual_email}"

        # 4. Save UserConfig
        if user_cfg:
            user_cfg.server_id = target_server.id
            user_cfg.inbound_id = reassign_in.inbound_id
            user_cfg.protocol = target_protocol
            user_cfg.client_uuid = actual_uuid
            user_cfg.client_email = actual_email
            user_cfg.uri = direct_uri
            user_cfg.last_traffic = 0
            user_cfg.is_active = user_is_funded
        else:
            user_cfg = UserConfig(
                user_id=target_user_id,
                location_id=reassign_in.location_id,
                server_id=target_server.id,
                inbound_id=reassign_in.inbound_id,
                protocol=target_protocol,
                client_email=new_client_email,
                client_uuid=new_uuid,
                uri=direct_uri,
                is_active=user_is_funded
            )
            db.add(user_cfg)

        db.commit()

        # 5. Purge old configs on all servers in this region
        prefix = f"{raw_username}-{loc_name}-"
        try:
            await XUIClient.purge_user_old_configs(
                api_url=target_server.api_url,
                api_token=target_server.api_token,
                email_prefix=prefix,
                active_client_uuid=new_uuid,
                active_client_email=new_client_email,
                server_id=target_server.id,
                server_name=target_server.name
            )
        except Exception:
            pass

        # Notify user on web dashboard and Telegram about manual reassignment
        await publish_server_changed_event(
            user_id=str(target_user_id),
            server_name=target_server.name,
            action="reassigned",
            location_name=loc_name,
            message=f"Your route for '{loc_name}' was reassigned by an administrator to server '{target_server.name}' (Inbound #{reassign_in.inbound_id})."
        )

        return {
            "status": "success",
            "message": f"Successfully reassigned route to {target_server.name} (Inbound #{reassign_in.inbound_id}) for {loc_name}.",
            "config_id": str(user_cfg.id),
            "uri": direct_uri
        }


@router.post("/users/{target_user_id}/configs")
async def admin_add_user_config(
    target_user_id: uuid.UUID,
    cfg_in: AdminAddUserConfigRequest,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin manually creates a new configuration for a user in any region.
    Allows multiple configurations in the same region.
    """
    verify_admin_role(user_payload)

    async with get_user_provisioning_lock(target_user_id):
        target_server = db.query(Server).filter(
            Server.id == cfg_in.server_id,
            Server.is_active == True,
            Server.is_reachable == True
        ).first()

        if not target_server:
            raise HTTPException(status_code=400, detail="Selected server is offline or not reachable.")

        limits = target_server.inbound_limits if isinstance(target_server.inbound_limits, list) else []
        chosen_inb = next((lim for lim in limits if int(lim.get("inbound_id", 0)) == cfg_in.inbound_id), None)

        target_protocol = cfg_in.protocol or (chosen_inb.get("protocol") if chosen_inb else target_server.protocol_type) or "vless_reality"

        sub = db.query(UserSubscription).filter(UserSubscription.user_id == target_user_id).first()
        raw_username = sub.username if sub and sub.username else f"usr_{str(target_user_id)[:8]}"

        loc = db.query(Location).filter(Location.id == cfg_in.location_id).first()
        loc_name = loc.name if loc else "Region"

        # Count existing configs for this user on this exact server/inbound
        existing_on_inbound = db.query(UserConfig).filter(
            UserConfig.user_id == target_user_id,
            UserConfig.server_id == cfg_in.server_id,
            UserConfig.inbound_id == cfg_in.inbound_id
        ).count()

        config_index = existing_on_inbound + 1 if existing_on_inbound > 0 else 1
        new_uuid = str(uuid.uuid4())
        sub_id = new_uuid.replace("-", "")[:16]

        config_name = build_config_name(raw_username, loc_name, new_uuid, index=config_index)
        new_client_email = config_name

        # Ensure email uniqueness in database
        while db.query(UserConfig).filter(UserConfig.client_email == new_client_email).first():
            config_index += 1
            new_uuid = str(uuid.uuid4())
            sub_id = new_uuid.replace("-", "")[:16]
            config_name = build_config_name(raw_username, loc_name, new_uuid, index=config_index)
            new_client_email = config_name

        user_is_funded = bool(sub and sub.has_credit and not sub.is_admin_suspended and not sub.is_self_suspended)

        created = await XUIClient.add_client(
            api_url=target_server.api_url,
            api_token=target_server.api_token,
            inbound_id=cfg_in.inbound_id,
            uuid=new_uuid,
            email=new_client_email,
            protocol=target_protocol,
            enable=user_is_funded,
            version=getattr(target_server, "xui_version", "v3.5.0"),
            sub_id=sub_id,
            server_id=target_server.id,
            server_name=target_server.name
        )

        if not created:
            err_detail = getattr(created, "error_msg", None) or "Failed to create client on target 3X-UI server panel."
            raise HTTPException(status_code=502, detail=f"Failed to create client on 3X-UI: {err_detail}")

        # Use verified email and uuid accepted by 3X-UI (accounting for auto-healing collision retries)
        actual_email = getattr(created, "email", new_client_email)
        actual_uuid = getattr(created, "uuid", new_uuid)

        direct_uri = await XUIClient.get_client_uri(
            api_url=target_server.api_url,
            api_token=target_server.api_token,
            inbound_id=cfg_in.inbound_id,
            email=actual_email,
            client_uuid=actual_uuid,
            sub_id=actual_uuid.replace("-", "")[:16],
            version=getattr(target_server, "xui_version", "v3.5.0")
        )

        if direct_uri:
            base_link = direct_uri.split("#")[0]
            direct_uri = f"{base_link}#{actual_email}"

        new_cfg = UserConfig(
            id=uuid.uuid4(),
            user_id=target_user_id,
            location_id=cfg_in.location_id,
            server_id=target_server.id,
            inbound_id=cfg_in.inbound_id,
            protocol=target_protocol,
            client_email=new_client_email,
            client_uuid=new_uuid,
            uri=direct_uri,
            is_active=user_is_funded,
            last_traffic=0
        )
        db.add(new_cfg)
        db.commit()

        # Notify user on web dashboard and Telegram about added config with specific instructions
        await publish_server_changed_event(
            user_id=str(target_user_id),
            server_name=target_server.name,
            action="added",
            location_name=loc_name,
            message=(
                f"A new Traffic Routing configuration for '{loc_name}' has been manually added to your account by an administrator.\n\n"
                f"• What you should do: Please update/refresh your subscription link in your Traffic Routing application, "
                f"or log in to the website / Telegram bot to view and copy your new configuration directly."
            )
        )

        return {
            "status": "success",
            "message": f"Successfully created config on {target_server.name} ({loc_name}).",
            "config_id": str(new_cfg.id),
            "uri": direct_uri
        }


@router.delete("/users/{target_user_id}/configs/{config_id}")
async def admin_delete_user_config(
    target_user_id: uuid.UUID,
    config_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin explicitly removes a specific configuration from 3X-UI and the database.
    """
    verify_admin_role(user_payload)

    async with get_user_provisioning_lock(target_user_id):
        cfg = db.query(UserConfig).filter(
            UserConfig.id == config_id,
            UserConfig.user_id == target_user_id
        ).first()

        if not cfg:
            raise HTTPException(status_code=404, detail="Configuration not found.")

        server = cfg.server
        if server and getattr(server, "is_reachable", True):
            try:
                await XUIClient.remove_client(
                    api_url=server.api_url,
                    api_token=server.api_token,
                    inbound_id=cfg.inbound_id,
                    uuid=cfg.client_uuid,
                    email=cfg.client_email,
                    version=getattr(server, "xui_version", "v3.5.0"),
                    server_id=server.id,
                    server_name=server.name
                )
            except Exception as e:
                logger.warning(f"Failed removing client from 3X-UI during admin delete: {e}")

        loc_name = cfg.location.name if cfg.location else "Traffic Routing Region"
        srv_name = server.name if server else "Traffic Routing Node"

        db.delete(cfg)
        db.commit()

        # Notify user on web dashboard and Telegram about deleted config with instructions to update sub
        await publish_server_changed_event(
            user_id=str(target_user_id),
            server_name=srv_name,
            action="removed",
            location_name=loc_name,
            message=(
                f"Your Traffic Routing configuration for '{loc_name}' was manually removed by an administrator.\n\n"
                f"• What you should do: Please update/refresh your subscription link in your Traffic Routing application to sync your active server list."
            )
        )

        return {"status": "success", "message": "Configuration removed successfully."}


@router.patch("/users/{target_user_id}/configs/{config_id}/toggle")
async def admin_toggle_single_config(
    target_user_id: uuid.UUID,
    config_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin toggles active/disabled status for an individual configuration on 3X-UI.
    """
    verify_admin_role(user_payload)

    async with get_user_provisioning_lock(target_user_id):
        cfg = db.query(UserConfig).filter(
            UserConfig.id == config_id,
            UserConfig.user_id == target_user_id
        ).first()

        if not cfg:
            raise HTTPException(status_code=404, detail="Configuration not found.")

        new_status = not cfg.is_active
        server = cfg.server

        if server and getattr(server, "is_reachable", True):
            try:
                await XUIClient.update_client_status(
                    api_url=server.api_url,
                    api_token=server.api_token,
                    inbound_id=cfg.inbound_id,
                    uuid=cfg.client_uuid,
                    email=cfg.client_email,
                    enable=new_status,
                    version=getattr(server, "xui_version", "v3.5.0"),
                    server_id=server.id,
                    server_name=server.name
                )
            except Exception as e:
                logger.warning(f"Error updating client status on 3X-UI: {e}")

        cfg.is_active = new_status
        db.commit()

        return {
            "status": "success",
            "is_active": new_status,
            "message": f"Configuration {'enabled' if new_status else 'disabled'} successfully."
        }

# ==========================================
# 5. IMMEDIATE USER PROVISIONING & STATUS RECONCILIATION
# ==========================================

async def check_user_credit_from_billing(user_id: uuid.UUID) -> bool:
    """Reads credit authorization state synchronized via RabbitMQ WALLET_FUNDED and WALLET_EMPTY events."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
        return bool(sub and sub.has_credit)
    except Exception as e:
        logger.warning(f"Could not read user credit state: {e}")
        return False
    finally:
        db.close()


@router.patch("/users/{target_user_id}/status")
async def execute_immediate_user_status_change(
    target_user_id: uuid.UUID,
    is_active: bool = Query(..., description="True to activate, False to suspend"),
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin Immediate Action:
    - If Suspended (is_active=False): Deletes all client configs from 3X-UI panels AND removes from node_db.
    - If Activated (is_active=True): Checks user credit/gift and provisions configs in 3X-UI immediately.
    """
    verify_admin_role(user_payload)

    sub = db.query(UserSubscription).filter(UserSubscription.user_id == target_user_id).first()
    if not sub:
        sub = UserSubscription(
            user_id=target_user_id,
            sub_token=uuid.uuid4().hex + uuid.uuid4().hex,
            username=f"usr_{str(target_user_id)[:8]}"
        )
        db.add(sub)
        db.flush()

    if not is_active:
        # 1. ADMIN SUSPEND: DELETE ALL CONFIGS FROM 3X-UI PANELS AND DATABASE
        sub.is_admin_suspended = True
        configs = db.query(UserConfig).filter(UserConfig.user_id == target_user_id).all()
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
                    logger.warning(f"Failed removing client from 3X-UI: {del_err}")

            db.delete(config)

        db.commit()

        # Recalculate and free location capacities
        for loc_id in affected_locations:
            await sync_location_capacity(db, loc_id)

        logger.info(f"[Immediate] Admin suspended User {target_user_id}: Removed all configs from 3X-UI and database.")
        return {
            "status": "success",
            "message": "User suspended: All configs removed from 3X-UI panels and database.",
            "action": "suspended"
        }

    else:
        # 2. ADMIN ACTIVATE: VERIFY CREDIT & PROVISION CONFIGS IMMEDIATELY
        sub.is_admin_suspended = False
        has_funds = await check_user_credit_from_billing(target_user_id)
        sub.has_credit = has_funds
        db.commit()

        if has_funds and not sub.is_self_suspended:
            from app.services.provisioning import provision_user_configs_for_all_locations
            username = sub.username or f"usr_{str(target_user_id)[:8]}"
            await provision_user_configs_for_all_locations(db, target_user_id, username=username, is_active=True)
            logger.info(f"[Immediate] Admin activated User {target_user_id}: Provisioned configs on 3X-UI.")
            return {
                "status": "success",
                "message": "User activated: Configurations provisioned on 3X-UI panels.",
                "action": "activated"
            }
        else:
            logger.info(f"[Immediate] Admin activated User {target_user_id}: Account active, waiting for wallet credit.")
            return {
                "status": "success",
                "message": "User activated: Account is active, waiting for wallet credit to provision.",
                "action": "waiting_credit"
            }


@router.post("/users/me/toggle-vpn")
async def execute_client_immediate_vpn_toggle(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Client Self-Action:
    Only enables or disables the client in 3X-UI (enable=False / True).
    Does NOT delete or remove configurations.
    """
    user_id = uuid.UUID(str(user_payload["sub"]))
    sub = db.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")

    sub.is_self_suspended = not sub.is_self_suspended
    db.commit()

    should_enable = bool(sub.has_credit and not sub.is_admin_suspended and not sub.is_self_suspended)
    configs = db.query(UserConfig).filter(UserConfig.user_id == user_id).all()

    for config in configs:
        server = config.server
        if server and getattr(server, "is_reachable", True):
            try:
                await XUIClient.update_client_status(
                    api_url=server.api_url,
                    api_token=server.api_token,
                    inbound_id=config.inbound_id,
                    uuid=config.client_uuid,
                    email=config.client_email,
                    enable=should_enable,
                    version=getattr(server, "xui_version", "v3.5.0"),
                    server_id=server.id,
                    server_name=server.name
                )
            except Exception:
                pass
        config.is_active = should_enable

    db.commit()
    return {
        "status": "success",
        "is_self_suspended": sub.is_self_suspended,
        "is_active": should_enable
    }