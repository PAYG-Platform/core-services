# traffic-service/app/events/consumer.py
import uuid
import logging
from datetime import datetime, timezone
from faststream.rabbit import RabbitRouter, RabbitExchange, ExchangeType

from app.database import SessionLocal
from app.models import DailyTrafficUsage, UserIspTrafficUsage, TrafficSettings
from app.services.isp_matcher import ISPMatcher
from app.xui_client import XUIClient
from mng_shared.events import EventKeys

logger = logging.getLogger(__name__)
traffic_consumer_router = RabbitRouter()

CONFIG_SETTINGS_EXCHANGE = getattr(
    EventKeys,
    "CONFIG_SETTINGS_EXCHANGE",
    RabbitExchange("config_settings_fanout", type=ExchangeType.FANOUT)
)


@traffic_consumer_router.subscriber("traffic_config_settings_queue", exchange=CONFIG_SETTINGS_EXCHANGE)
async def handle_config_settings_changed_exchange(msg: dict):
    """Handles settings changes broadcasted via Fanout Exchange."""
    await process_config_settings_change(msg)


@traffic_consumer_router.subscriber(EventKeys.CONFIG_SETTINGS_CHANGED)
async def handle_config_settings_changed_queue(msg: dict):
    """Handles settings changes received via direct queue fallback."""
    await process_config_settings_change(msg)


async def process_config_settings_change(msg: dict):
    if "xui_request_timeout" in msg:
        try:
            val = float(msg["xui_request_timeout"])
            XUIClient.set_timeout(val)
            db = SessionLocal()
            try:
                row = db.query(TrafficSettings).filter(TrafficSettings.key == "xui_request_timeout").first()
                if not row:
                    db.add(TrafficSettings(key="xui_request_timeout", value=str(val)))
                else:
                    row.value = str(val)
                db.commit()
                logger.info(f"Traffic Service updated 3x-ui timeout to {val}s from event.")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error updating xui_request_timeout in Traffic Service: {e}")

# Safe fallback definition for the Fanout Exchange
TRAFFIC_EXCHANGE = getattr(
    EventKeys,
    "TRAFFIC_CONSUMED_EXCHANGE",
    RabbitExchange("traffic_consumed_fanout", type=ExchangeType.FANOUT)
)


# SEPARATE SUBSCRIBERS TO PREVENT FASTSTREAM DECORATOR WRAPPER CONFLICTS
@traffic_consumer_router.subscriber("traffic_metering_consumed_queue", exchange=TRAFFIC_EXCHANGE)
async def handle_traffic_consumed_event_exchange(msg: dict):
    """Listens for traffic telemetry events broadcasted via the Fanout Exchange."""
    await process_traffic_consumed_telemetry(msg)


@traffic_consumer_router.subscriber(EventKeys.TRAFFIC_CONSUMED)
async def handle_traffic_consumed_event_queue(msg: dict):
    """Listens for traffic telemetry events sent directly to the queue."""
    await process_traffic_consumed_telemetry(msg)


async def process_traffic_consumed_telemetry(msg: dict):
    """
    Consumes bandwidth usage events from 3X-UI:
    1. Upserts DailyTrafficUsage in traffic_db.
    2. Gathers all simultaneously connected device IPs for the config.
    3. Distributes usage across all active IPs/ISPs and creates/updates a row per (user_id, ip_address).
    """
    raw_user_id = msg.get("user_id")
    used_bytes = int(msg.get("used_bytes") or 0)

    # Extract all active device IPs (supports list or single string fallback)
    raw_ips = msg.get("client_ips") or []
    if isinstance(raw_ips, str) and raw_ips.strip():
        raw_ips = [raw_ips.strip()]
    single_ip = str(msg.get("client_ip") or "").strip()
    if single_ip and single_ip not in raw_ips:
        raw_ips.append(single_ip)

    if not raw_user_id or used_bytes <= 0:
        return

    try:
        user_uuid = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError):
        return

    today = datetime.now(timezone.utc).date()
    db = SessionLocal()

    try:
        # 1. Upsert Daily Traffic in traffic_db
        daily_record = (
            db.query(DailyTrafficUsage)
            .filter(
                DailyTrafficUsage.user_id == user_uuid,
                DailyTrafficUsage.date == today
            )
            .first()
        )

        if daily_record:
            daily_record.total_bytes += used_bytes
            daily_record.updated_at = datetime.now(timezone.utc)
        else:
            db.add(DailyTrafficUsage(
                id=uuid.uuid4(),
                user_id=user_uuid,
                date=today,
                total_bytes=used_bytes
            ))

        # 2. Distribute usage evenly across all active device IPs
        active_ips = [ip.strip() for ip in raw_ips if ip.strip()] or ["Direct / Unknown"]
        ip_count = len(active_ips)
        bytes_per_ip = used_bytes // ip_count
        remainder = used_bytes % ip_count

        for idx, ip in enumerate(active_ips):
            allocated_bytes = bytes_per_ip + (remainder if idx == 0 else 0)
            if allocated_bytes <= 0:
                continue

            isp_meta = ISPMatcher.lookup(ip if ip != "Direct / Unknown" else None)
            isp_name = isp_meta.get("name", "Other / Direct")
            country = isp_meta.get("country", "Unknown")

            # 3. Look up by (user_id, ip_address) -> If IP is new, INSERTS A NEW ROW
            ip_record = (
                db.query(UserIspTrafficUsage)
                .filter(
                    UserIspTrafficUsage.user_id == user_uuid,
                    UserIspTrafficUsage.ip_address == ip
                )
                .first()
            )

            if ip_record:
                ip_record.total_bytes += allocated_bytes
                ip_record.isp_name = isp_name
                ip_record.country = country
                ip_record.last_ip = ip
                ip_record.updated_at = datetime.now(timezone.utc)
            else:
                db.add(UserIspTrafficUsage(
                    id=uuid.uuid4(),
                    user_id=user_uuid,
                    ip_address=ip,
                    last_ip=ip,
                    isp_name=isp_name,
                    country=country,
                    total_bytes=allocated_bytes
                ))
                logger.info(f"[Traffic-Service] Added NEW IP row for User {user_uuid}: {ip} ({isp_name})")

        db.commit()
        logger.info(f"[Traffic-Service] Recorded {used_bytes} bytes for User {user_uuid} across {len(active_ips)} active IP(s).")

    except Exception as e:
        db.rollback()
        logger.error(f"[Traffic-Service] Error recording traffic telemetry in traffic_db: {e}")
    finally:
        db.close()