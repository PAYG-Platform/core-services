# backend/traffic-service/app/sync_job.py
import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from faststream.rabbit import RabbitBroker
from vpn_shared import EventKeys

from app.database import SessionLocal
from app.models import Server, UserConfig, DailyTrafficUsage, UserIspTrafficUsage
from app.xui_client import XUIClient
from app.services.isp_matcher import ISPMatcher

logger = logging.getLogger(__name__)


async def run_traffic_sync(broker: RabbitBroker):
    """
    Core Background Cron Task:
    1. Queries active user configs across active 3X-UI servers.
    2. Polls all active connected client IPs (supporting multiple devices simultaneously).
    3. Fetches real-time byte count from 3X-UI REST APIs.
    4. Calculates consumption delta (handling panel reboots).
    5. Attributes and upserts usage into DailyTrafficUsage and UserIspTrafficUsage (by user_id and ip_address).
    6. Emits TRAFFIC_CONSUMED event to RabbitMQ for financial deduction in Billing Service.
    """
    logger.info("Starting scheduled VPN traffic sync and ISP telemetry job...")
    db: Session = SessionLocal()

    try:
        # 1. Query active configs on active server nodes
        active_configs = (
            db.query(UserConfig)
            .join(Server)
            .filter(
                UserConfig.is_active == True,
                Server.is_active == True
            )
            .all()
        )

        if not active_configs:
            logger.info("No active user configurations found to sync.")
            return

        synced_count = 0
        today = datetime.now(timezone.utc).date()

        for config in active_configs:
            server = config.server

            # 2. Fetch all connected client device IPs from 3X-UI panel
            connected_ips = await XUIClient.get_client_ips(
                api_url=server.api_url,
                api_token=server.api_token,
                email=config.client_email,
                version=getattr(server, "xui_version", "v3.5.0")
            )

            # Update last known IP and ISP if fresh connection is detected
            if connected_ips:
                primary_ip = connected_ips[0]
                config.last_ip = primary_ip
                primary_meta = ISPMatcher.lookup(primary_ip)
                config.last_isp = primary_meta.get("name", "Other / Direct")

            # 3. Fetch current cumulative byte count from 3X-UI API
            current_traffic = await XUIClient.get_client_traffic(
                api_url=server.api_url,
                api_token=server.api_token,
                email=config.client_email,
                version=getattr(server, "xui_version", "v3.5.0")
            )

            if current_traffic is None:
                continue

            # 4. Delta Calculation Algorithm
            last_traffic = config.last_traffic or 0
            delta = 0

            if current_traffic >= last_traffic:
                # Normal state: traffic increased
                delta = current_traffic - last_traffic
            else:
                # Reboot state: Xray core restarted and reset stats to 0
                delta = current_traffic

            # 5. Report usage if data was consumed
            if delta > 0:
                config.last_traffic = current_traffic

                # --- A. Record / Upsert Daily Traffic in traffic_db ---
                daily_record = (
                    db.query(DailyTrafficUsage)
                    .filter(
                        DailyTrafficUsage.user_id == config.user_id,
                        DailyTrafficUsage.date == today
                    )
                    .first()
                )

                if daily_record:
                    daily_record.total_bytes += delta
                    daily_record.updated_at = datetime.now(timezone.utc)
                else:
                    db.add(DailyTrafficUsage(
                        id=uuid.uuid4(),
                        user_id=config.user_id,
                        date=today,
                        total_bytes=delta
                    ))

                # --- B. Record / Upsert Usage per Connected IP (Supports Multiple Devices) ---
                active_ips = connected_ips if connected_ips else (
                    [config.last_ip] if config.last_ip and not config.last_ip.startswith("{") else ["Direct / Unknown"]
                )
                ip_count = len(active_ips)
                bytes_per_ip = delta // ip_count
                remainder = delta % ip_count

                for idx, ip in enumerate(active_ips):
                    allocated_bytes = bytes_per_ip + (remainder if idx == 0 else 0)
                    if allocated_bytes <= 0:
                        continue

                    isp_meta = ISPMatcher.lookup(ip if ip != "Direct / Unknown" else None)
                    isp_name = isp_meta.get("name", "Other / Direct")
                    country = isp_meta.get("country", "Unknown")

                    # Look up by (user_id, ip_address) -> If IP is new, INSERTS A NEW ROW!
                    ip_record = (
                        db.query(UserIspTrafficUsage)
                        .filter(
                            UserIspTrafficUsage.user_id == config.user_id,
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
                            user_id=config.user_id,
                            ip_address=ip,
                            last_ip=ip,
                            isp_name=isp_name,
                            country=country,
                            total_bytes=allocated_bytes
                        ))
                        logger.info(f"[Sync-Job] Added NEW IP row for User {config.user_id}: {ip} ({isp_name})")

                # --- C. Publish Event for Billing Deductions ---
                event_payload = {
                    "user_id": str(config.user_id),
                    "server_id": str(server.id),
                    "used_bytes": delta,
                    "price_per_gb": float(server.price_per_gb),
                    "price_per_gb_usd": float(getattr(server, "price_per_gb_usd", 0.05)),
                    "client_email": config.client_email,
                    "client_ips": active_ips,
                    "client_ip": active_ips[0] if active_ips else ""
                }

                # Publish event to RabbitMQ for Billing Service
                await broker.publish(
                    event_payload,
                    queue=EventKeys.TRAFFIC_CONSUMED
                )

                synced_count += 1
                logger.info(
                    f"Reported {delta} bytes for User {config.user_id} across {len(active_ips)} device IP(s) "
                    f"on {server.name} ({server.price_per_gb:.0f} T/GB)."
                )

        db.commit()
        logger.info(f"Traffic & ISP sync completed. Processed {synced_count} active usage event(s).")

    except Exception as e:
        db.rollback()
        logger.error(f"Critical error executing traffic sync job: {e}")
    finally:
        db.close()