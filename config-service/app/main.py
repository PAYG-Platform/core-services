# backend/config-service/app/main.py
import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.database import engine, Base
from app.api.config import router as config_router
from app.events.publisher import broker
from app.events.consumer import config_router as event_router
from vpn_shared.logger import setup_logging

logger = setup_logging("config-service")

broker.include_router(event_router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify tables and ensure migration column exists
    try:
        Base.metadata.create_all(bind=engine)
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("""
                ALTER TABLE platform_system_configs
                ADD COLUMN IF NOT EXISTS xui_request_timeout DOUBLE PRECISION NOT NULL DEFAULT 15.0;
            """))
    except Exception as e:
        logger.warning(f"Database schema verification error: {e}")

    logger.info("Starting Config Service RabbitMQ Broker...")
    for attempt in range(1, 11):
        try:
            await broker.start()
            logger.info("Successfully started RabbitMQ Broker and consumers.")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ startup attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)
    yield
    logger.info("Disconnecting Config Service from RabbitMQ...")
    try:
        await broker.close()
    except Exception as e:
        logger.error(f"Error closing RabbitMQ connection: {e}")


app = FastAPI(
    title="Config Service",
    description="Standalone platform branding, locale defaults, and maintenance mode microservice",
    version="1.0.0",
    lifespan=lifespan
)

app.include_router(config_router)


@app.get("/health")
def health_check():
    return {"service": "config-service", "status": "operational"}