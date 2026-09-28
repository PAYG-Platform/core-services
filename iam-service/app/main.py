# backend/iam-service/app/main.py
import os
import sys
import logging
import traceback
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.events.publisher import broker
from app.events.consumer import iam_router, load_local_cooldown
from app.database import engine, Base, SessionLocal
from vpn_shared.logger import setup_logging

# --------------------------------------------------------------------------
# File & Console Logging Configuration
# --------------------------------------------------------------------------
logger = setup_logging("iam-service")


# --------------------------------------------------------------------------
# Lifespan Context Manager
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Connecting IAM Service to PostgreSQL Database...")
    for attempt in range(1, 11):
        try:
            Base.metadata.create_all(bind=engine)

            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(100);"))
                conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR(10) NOT NULL DEFAULT 'en';"))
                conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_currency VARCHAR(10) NOT NULL DEFAULT 'IRT';"))
                conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_self_suspended BOOLEAN NOT NULL DEFAULT FALSE;"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS iam_settings (
                        key VARCHAR(50) PRIMARY KEY,
                        value VARCHAR(255) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    );
                """))
                conn.commit()

            logger.info("Database connection and tables verified successfully.")
            break
        except Exception as e:
            logger.warning(f"Database connection attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)

    # Connect to RabbitMQ and start consumers
    from app.core.security import get_jwt_private_key, get_jwt_public_key
    get_jwt_private_key()
    get_jwt_public_key()
    logger.info("RSA Private and Public keys loaded and verified successfully.")

    # Connect to RabbitMQ and start consumers
    broker.include_router(iam_router)
    for attempt in range(1, 11):
        try:
            await broker.start()
            logger.info("Successfully started RabbitMQ broker and consumers.")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ connection attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)

    # Load local cooldown setting on startup
    db_init = SessionLocal()
    try:
        load_local_cooldown(db_init)
    finally:
        db_init.close()

    yield

    logger.info("Closing RabbitMQ connection...")
    try:
        await broker.close()
    except Exception as e:
        logger.error(f"Error closing RabbitMQ connection: {e}")


app = FastAPI(
    title="IAM Service",
    description="Identity and Authentication Management Service",
    version="1.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------
# Global Unhandled Exception Handler
# --------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_trace = traceback.format_exc()
    logger.error(f"Unhandled Exception on {request.method} {request.url}:\n{error_trace}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": f"Internal Server Error: {str(exc)}",
            "traceback": error_trace.splitlines()[-6:]
        }
    )


# Register Routers
app.include_router(auth_router)
app.include_router(users_router)


@app.get("/health")
def health_check():
    return {"service": "iam-service", "status": "healthy"}