# backend/notification-service/app/main.py
import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text

from app.database import engine, Base
import app.models
from app.api.notifications import router as notifications_router
from app.api.settings import router as settings_router
from app.events.consumer import notification_router
from app.events.publisher import broker
from mng_shared.logger import setup_logging

logger = setup_logging("notification-service")

broker.include_router(notification_router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Verify DB connection and auto-create tables
    logger.info("Connecting Notification Service to Database and verifying tables...")
    for attempt in range(1, 11):
        try:
            Base.metadata.create_all(bind=engine)

            with engine.begin() as conn:
                conn.execute(text("""
                    ALTER TABLE user_notification_preferences 
                    ALTER COLUMN notify_client_topup DROP NOT NULL,
                    ALTER COLUMN notify_client_out_of_credit DROP NOT NULL,
                    ALTER COLUMN notify_admin_debt_limit DROP NOT NULL,
                    ALTER COLUMN debt_limit_threshold DROP NOT NULL,
                    ADD COLUMN IF NOT EXISTS is_empty_warned BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS last_grace_day_notified INTEGER NOT NULL DEFAULT 0;
                """))

            logger.info("Notification Service database tables verified successfully.")
            break
        except Exception as e:
            logger.warning(f"Database connection attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)

    # 2. Start RabbitMQ Broker & Consumers
    logger.info("Starting Notification Service RabbitMQ Broker and Consumers...")
    for attempt in range(1, 11):
        try:
            await broker.start()
            logger.info("Successfully started RabbitMQ Broker and notification consumers.")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ startup attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)

    yield

    logger.info("Disconnecting Notification Service from RabbitMQ...")
    try:
        await broker.close()
    except Exception as e:
        logger.error(f"Error closing RabbitMQ connection: {e}")


app = FastAPI(
    title="Notification Service",
    description="Standalone notification microservice with rate-limited polling and event-driven architecture",
    version="1.0.0",
    lifespan=lifespan
)

# Register API Routers
app.include_router(notifications_router)
app.include_router(settings_router)


@app.get("/health")
def health_check():
    return {"service": "notification-service", "status": "operational"}