# backend/config-service/app/api/config.py
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from vpn_shared import EventKeys
from app.database import get_db
from app.models import PlatformSystemConfig
from app.schemas import PublicPlatformConfigResponse, PlatformConfigUpdate
from app.events.publisher import broker, publish_system_broadcast_event, publish_sync_interval_changed_event
from vpn_shared.jwt_auth import get_current_user_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config", tags=["Platform Configuration & Maintenance"])


class BroadcastRequest(BaseModel):
    message: str = Field(..., min_length=1, description="System announcement or maintenance notice")


def get_or_create_platform_config(db: Session) -> PlatformSystemConfig:
    config = db.query(PlatformSystemConfig).first()
    if not config:
        config = PlatformSystemConfig(
            brand_name="PAYG VPN",
            default_currency="IRT",
            default_language="en",
            default_theme="dark",
            timezone="Asia/Tehran",
            billing_sync_interval=5,
            xui_request_timeout=15.0,
            is_maintenance_mode=False,
            maintenance_message="System is undergoing scheduled maintenance. Please check back shortly."
        )
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


@router.get("/public", response_model=PublicPlatformConfigResponse)
def get_public_platform_config(db: Session = Depends(get_db)):
    """
    Public Endpoint (No JWT authentication required).
    Fetched by API Gateway and Frontend on startup to render branding, locale defaults,
    and check System Maintenance Mode.
    """
    return get_or_create_platform_config(db)


@router.put("/admin", response_model=PublicPlatformConfigResponse)
async def update_admin_platform_config(
    config_in: PlatformConfigUpdate,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required.")

    c = get_or_create_platform_config(db)

    if config_in.brand_name is not None:
        c.brand_name = config_in.brand_name.strip()
    if config_in.default_currency is not None:
        c.default_currency = config_in.default_currency
    if config_in.default_language is not None:
        c.default_language = config_in.default_language
    if config_in.default_theme is not None:
        c.default_theme = config_in.default_theme
    if config_in.timezone is not None:
        c.timezone = config_in.timezone
    if config_in.billing_sync_interval is not None:
        new_interval = max(1, config_in.billing_sync_interval)
        if new_interval != c.billing_sync_interval:
            c.billing_sync_interval = new_interval
            await publish_sync_interval_changed_event(new_interval)

    # Update 3X-UI Request Timeout (in seconds)
    if config_in.xui_request_timeout is not None:
        c.xui_request_timeout = max(1.0, float(config_in.xui_request_timeout))
            
    # Update Client Status Cooldown (in minutes)
    if config_in.client_status_cooldown_minutes is not None:
        c.client_status_cooldown_minutes = max(0, config_in.client_status_cooldown_minutes)

    # Update Route Replacement Policy
    if config_in.route_replace_cooldown_days is not None:
        c.route_replace_cooldown_days = max(0, config_in.route_replace_cooldown_days)
    if config_in.disable_client_route_replacement is not None:
        c.disable_client_route_replacement = config_in.disable_client_route_replacement

    # Broadcast updated settings to all microservices via Fanout Exchange and Queue
    try:
        if not getattr(broker, "_connection", None) or not getattr(broker._connection, "is_connected", False):
            await broker.connect()

        payload = {
            "client_status_cooldown_minutes": c.client_status_cooldown_minutes,
            "route_replace_cooldown_days": c.route_replace_cooldown_days,
            "disable_client_route_replacement": c.disable_client_route_replacement,
            "xui_request_timeout": getattr(c, "xui_request_timeout", 15.0) or 15.0
        }

        # 1. Publish to Fanout Exchange so all microservices receive the update
        if hasattr(EventKeys, "CONFIG_SETTINGS_EXCHANGE"):
            await broker.publish(payload, exchange=EventKeys.CONFIG_SETTINGS_EXCHANGE)
        # 2. Publish to direct queue for backward compatibility
        await broker.publish(payload, queue=EventKeys.CONFIG_SETTINGS_CHANGED)
        logger.info(f"Broadcasted config settings changed event: {payload}")
    except Exception as e:
        logger.warning(f"Could not broadcast config settings via RabbitMQ: {e}")



    if config_in.is_maintenance_mode is not None:
        c.is_maintenance_mode = config_in.is_maintenance_mode
    if config_in.maintenance_message is not None:
        c.maintenance_message = config_in.maintenance_message.strip()

    db.commit()
    db.refresh(c)
    return c


@router.post("/admin/broadcast")
async def broadcast_system_announcement(
    req: BroadcastRequest,
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin-only endpoint to broadcast system announcements or maintenance alerts.
    Emits a SYSTEM_BROADCAST event to RabbitMQ for notification-service.
    """
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Admin privileges required."
        )

    await publish_system_broadcast_event(
        title="System Announcement / Maintenance",
        message=req.message.strip()
    )

    return {"message": "System announcement broadcasted successfully via RabbitMQ."}