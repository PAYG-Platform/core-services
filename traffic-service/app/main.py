# backend/traffic-service/app/main.py
import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from faststream.rabbit import RabbitBroker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text
from app.events.consumer import traffic_consumer_router
from app.database import engine, Base
from app.models import Server, UserConfig, DailyTrafficUsage, UserIspTrafficUsage, TrafficSettings
from app.sync_job import run_traffic_sync
from app.services.isp_matcher import ISPMatcher
from app.api.traffic import router as traffic_router
from app.xui_client import XUIClient
from mng_shared.logger import setup_logging

logger = setup_logging("traffic-service")

load_dotenv()

RABBITMQ_URL = os.getenv("RABBITMQ_URL")
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", 5))

if not RABBITMQ_URL:
    logger.critical("RABBITMQ_URL environment variable is missing!")
    sys.exit(1)

broker = RabbitBroker(RABBITMQ_URL)
broker.include_router(traffic_consumer_router)
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Load ISP Subnet Database
    logger.info("Initializing ISP Matcher subnet database...")
    ISPMatcher.load()

    # 2. Verify DB connection and auto-create tables if missing
    logger.info("Verifying PostgreSQL database connection and tables for Traffic Service...")
    for attempt in range(1, 11):
        try:
            Base.metadata.create_all(bind=engine)

            with engine.begin() as conn:
                # Ensure daily_traffic_usage exists
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS daily_traffic_usage (
                        id UUID PRIMARY KEY,
                        user_id UUID NOT NULL,
                        date DATE NOT NULL,
                        total_bytes BIGINT NOT NULL DEFAULT 0,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        CONSTRAINT uq_traffic_user_daily UNIQUE (user_id, date)
                    );
                """))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_traffic_daily_user_id ON daily_traffic_usage(user_id);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_traffic_daily_date ON daily_traffic_usage(date);"))

                # Ensure user_configs has IP/ISP tracking columns
                conn.execute(text("""
                    ALTER TABLE user_configs 
                    ADD COLUMN IF NOT EXISTS last_ip VARCHAR(50),
                    ADD COLUMN IF NOT EXISTS last_isp VARCHAR(100) DEFAULT 'Other';
                """))

                # Ensure user_isp_traffic_usage exists with unique constraint on (user_id, ip_address)
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS user_isp_traffic_usage (
                        id UUID PRIMARY KEY,
                        user_id UUID NOT NULL,
                        ip_address VARCHAR(50) NOT NULL DEFAULT 'Direct / Unknown',
                        last_ip VARCHAR(50),
                        isp_name VARCHAR(100) NOT NULL,
                        country VARCHAR(10) NOT NULL DEFAULT 'Unknown',
                        total_bytes BIGINT NOT NULL DEFAULT 0,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        CONSTRAINT uq_user_ip_usage UNIQUE (user_id, ip_address)
                    );
                """))
                conn.execute(text("""
                    ALTER TABLE user_isp_traffic_usage 
                    ADD COLUMN IF NOT EXISTS ip_address VARCHAR(50) DEFAULT 'Direct / Unknown',
                    ADD COLUMN IF NOT EXISTS last_ip VARCHAR(50);
                """))

                # Safely drop old (user_id, isp_name) constraint and ensure uq_user_ip_usage is active
                conn.execute(text("""
                    DO $$ 
                    BEGIN
                        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_user_isp_usage') THEN
                            ALTER TABLE user_isp_traffic_usage DROP CONSTRAINT uq_user_isp_usage;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_user_ip_usage') THEN
                            ALTER TABLE user_isp_traffic_usage ADD CONSTRAINT uq_user_ip_usage UNIQUE (user_id, ip_address);
                        END IF;
                    END $$;
                """))

                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_isp_usage_user_id ON user_isp_traffic_usage(user_id);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_isp_usage_ip ON user_isp_traffic_usage(ip_address);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_isp_usage_isp ON user_isp_traffic_usage(isp_name);"))

                # Ensure servers table checkpoint contains price_per_gb_usd
                conn.execute(text("""
                    ALTER TABLE servers 
                    ADD COLUMN IF NOT EXISTS price_per_gb_usd DOUBLE PRECISION NOT NULL DEFAULT 0.05;
                """))

                # Ensure traffic_settings table exists
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS traffic_settings (
                        key VARCHAR(50) PRIMARY KEY,
                        value VARCHAR(255) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    );
                """))

            # Load initial 3X-UI request timeout from DB or ENV
            with engine.connect() as conn:
                res = conn.execute(text("SELECT value FROM traffic_settings WHERE key = 'xui_request_timeout' LIMIT 1;")).fetchone()
                if res and res[0]:
                    XUIClient.set_timeout(float(res[0]))
                    logger.info(f"Traffic Service loaded 3x-ui timeout={res[0]}s from traffic_db.")
                else:
                    env_timeout = float(os.getenv("XUI_REQUEST_TIMEOUT", "15.0"))
                    XUIClient.set_timeout(env_timeout)

            logger.info("Database tables and schema verified successfully in traffic_db.")
            break
        except Exception as e:
            logger.warning(f"Database connection attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)

    # 3. Connect to RabbitMQ and start listeners
    logger.info("Connecting Traffic Service to RabbitMQ and starting consumers...")
    for attempt in range(1, 11):
        try:
            await broker.start()
            logger.info("Successfully connected to RabbitMQ and started listeners.")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ connection attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)

    # 4. Schedule cron job
    logger.info(f"Starting APScheduler (Interval: {SYNC_INTERVAL_MINUTES} minutes)...")
    scheduler.add_job(
        run_traffic_sync,
        'interval',
        minutes=SYNC_INTERVAL_MINUTES,
        args=[broker],
        id='sync_traffic_job',
        replace_existing=True
    )
    scheduler.start()

    yield

    # 5. Graceful Shutdown
    logger.info("Stopping scheduler and closing RabbitMQ broker...")
    scheduler.shutdown()
    try:
        if hasattr(broker, "stop"):
            await broker.stop()
        elif hasattr(broker, "close"):
            await broker.close()
    except Exception as e:
        logger.error(f"Error closing RabbitMQ connection: {e}")


app = FastAPI(
    title="Traffic & Metering Service",
    description="Bandwidth polling, telemetry timeseries aggregation, ISP attribution, and 3x-ui sync service",
    version="2.1.0",
    lifespan=lifespan
)

# Register REST Routers
app.include_router(traffic_router)


@app.get("/health")
def health_check():
    return {"service": "traffic-service", "status": "operational", "db_connected": True}