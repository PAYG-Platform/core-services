# backend/node-service/app/main.py
import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text
from vpn_shared.rate_limiter import RedisRateLimiter
from app.database import engine, Base, SessionLocal
import app.models
from vpn_shared.logger import setup_logging
from app.events.publisher import broker
from app.events.consumer import node_router
from app.api.servers import router as servers_router
from app.api.subscription import router as subscription_router
from app.services.provisioning import (
    run_server_health_and_provisioning_sweep,
    load_local_sync_interval,
    load_local_xui_timeout,
    set_sync_interval_minutes,
    get_sync_interval_minutes,
    get_interval_wake_event,
)

logger = setup_logging("node-service")

RATE_LIMITER = RedisRateLimiter(
    config_path="/app/config/rate_limits.yml",
    service_name="node-service",
)

# Attach FastStream consumer router to the shared RabbitMQ broker
broker.include_router(node_router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Database connection & schema verification
    logger.info("Verifying Node Service database tables and columns...")
    try:
        Base.metadata.create_all(bind=engine)

        with engine.begin() as conn:
            # Upgrade server reachability and dual-currency pricing columns
            conn.execute(text("""
                ALTER TABLE servers
                ADD COLUMN IF NOT EXISTS is_reachable BOOLEAN DEFAULT TRUE,
                ADD COLUMN IF NOT EXISTS last_unreachable_at TIMESTAMP WITH TIME ZONE,
                ADD COLUMN IF NOT EXISTS last_error TEXT,
                ADD COLUMN IF NOT EXISTS price_per_gb_usd DOUBLE PRECISION NOT NULL DEFAULT 0.05;
            """))

            # Track usernames, local credit authorization and billing currency
            conn.execute(text("""
                ALTER TABLE user_subscriptions
                ADD COLUMN IF NOT EXISTS username VARCHAR(50),
                ADD COLUMN IF NOT EXISTS has_credit BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS billing_currency VARCHAR(10) NOT NULL DEFAULT 'IRT';
            """))

            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS
                ix_user_subscriptions_username
                ON user_subscriptions(username);
            """))

            # Replicate settings locally via messaging without point-to-point HTTP coupling
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS node_settings (
                    key VARCHAR(50) PRIMARY KEY,
                    value VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                );
            """))

            # Ensure location_id exists on user_configs
            conn.execute(text("""
                ALTER TABLE user_configs
                ADD COLUMN IF NOT EXISTS location_id UUID;
            """))

            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS
                ix_user_configs_location_id
                ON user_configs(location_id);
            """))

            # Reservation tracking schema
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS config_reservations (
                    id UUID PRIMARY KEY,
                    user_id UUID NOT NULL,
                    location_id UUID NOT NULL
                        REFERENCES locations(id)
                        ON DELETE CASCADE,
                    server_id UUID NOT NULL
                        REFERENCES servers(id)
                        ON DELETE CASCADE,
                    inbound_id INTEGER NOT NULL,
                    protocol VARCHAR(30) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'Reserved',
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    topup_transaction_id UUID NULL,
                    created_at TIMESTAMP WITH TIME ZONE
                        NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE
                        NOT NULL DEFAULT NOW()
                );
            """))

            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS
                ix_config_reservations_location_id
                ON config_reservations(location_id);
            """))

            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS
                ix_config_reservations_server_inbound
                ON config_reservations(server_id, inbound_id, protocol);
            """))

            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS
                ix_config_reservations_user_location
                ON config_reservations(user_id, location_id);
            """))

            # Initial sync for existing active configurations to credit authorization
            conn.execute(text("""
                UPDATE user_subscriptions SET has_credit = TRUE 
                WHERE user_id IN (SELECT DISTINCT user_id FROM user_configs WHERE is_active = TRUE);
            """))

            conn.execute(text("""
                ALTER TABLE user_subscriptions
                ADD COLUMN IF NOT EXISTS username VARCHAR(50),
                ADD COLUMN IF NOT EXISTS has_credit BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS is_admin_suspended BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS is_self_suspended BOOLEAN NOT NULL DEFAULT FALSE;
            """))

            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS user_blocked_route_histories (
                    id UUID PRIMARY KEY,
                    user_id UUID NOT NULL,
                    location_id UUID NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                    server_id UUID NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                    inbound_id INTEGER NOT NULL,
                    protocol VARCHAR(30) NOT NULL,
                    host_or_sni VARCHAR(255) NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                );
            """))

            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_user_blocked_hist_lookup 
                ON user_blocked_route_histories(user_id, location_id);
            """))

            conn.execute(text("""
                ALTER TABLE user_blocked_route_histories
                ADD COLUMN IF NOT EXISTS client_uuid VARCHAR(255),
                ADD COLUMN IF NOT EXISTS client_email VARCHAR(255),
                ADD COLUMN IF NOT EXISTS is_deleted_from_panel BOOLEAN NOT NULL DEFAULT FALSE;
            """))

        logger.info("Node Service database tables and columns verified successfully.")
    except Exception as e:
        logger.error(f"Error during schema verification: {e}")

    # 2. Connect the shared RabbitMQ broker and start consumers
    logger.info("Starting Node Service RabbitMQ Broker and consumers...")
    for attempt in range(1, 11):
        try:
            await broker.start()
            logger.info("Successfully connected Node Service to RabbitMQ.")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ startup attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)

    # 3. Load initial sync interval and 3x-ui timeout from local node_db / ENV
    db_init = SessionLocal()
    try:
        init_interval = load_local_sync_interval(db_init)
        set_sync_interval_minutes(init_interval)
        load_local_xui_timeout(db_init)
    finally:
        db_init.close()

    # 4. Start background periodic PAYG traffic sync, credit check, and health probe
    async def periodic_server_sweep_loop():
        wake_event = get_interval_wake_event()

        while True:
            try:
                db = SessionLocal()
                try:
                    await run_server_health_and_provisioning_sweep(db)
                except Exception as sweep_err:
                    logger.error(f"Error in PAYG traffic sync & sweep: {sweep_err}")
                finally:
                    db.close()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in periodic server sweep loop: {e}")

            # Sleep dynamically based on the current interval setting
            interval_mins = get_sync_interval_minutes()
            sleep_secs = interval_mins * 60
            logger.info(f"PAYG sync finished. Next run scheduled in {interval_mins} minute(s) ({sleep_secs}s)...")

            wake_event.clear()
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=sleep_secs)
                logger.info(f"PAYG interval changed to {get_sync_interval_minutes()}m via event. Resuming sync immediately.")
            except asyncio.TimeoutError:
                pass

    sweep_task = asyncio.create_task(periodic_server_sweep_loop())

    yield

    # 5. Graceful shutdown
    sweep_task.cancel()
    logger.info("Disconnecting Node Service from RabbitMQ...")
    try:
        await broker.close()
    except Exception as e:
        logger.error(f"Error closing RabbitMQ connection: {e}")


app = FastAPI(
    title="Node Management Service",
    description="3x-ui Server Management, Regional Health Probing & V2Ray Subscriptions Service",
    version="1.0.0",
    lifespan=lifespan
)
app.middleware("http")(RATE_LIMITER.middleware)

# Include HTTP REST API Routers
app.include_router(servers_router)
app.include_router(subscription_router)


@app.get("/health")
def health_check():
    return {"service": "node-service", "status": "operational"}