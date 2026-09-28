# support-service/app/main.py
import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.events.publisher import broker
from app.database import engine, Base
import app.models
from app.events.consumer import support_consumer_router
from app.api.tickets import router as tickets_router
from vpn_shared.logger import setup_logging

logger = setup_logging("support-service")

broker.include_router(support_consumer_router)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_DIR, "tickets"), exist_ok=True)
except Exception:
    UPLOAD_DIR = os.path.join(ROOT_DIR, "uploads")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_DIR, "tickets"), exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        logger.warning(f"Database table verification error: {e}")

    logger.info("Connecting Support Service to RabbitMQ and starting consumers...")
    for attempt in range(1, 11):
        try:
            await broker.start()
            logger.info("Successfully connected to RabbitMQ and started listeners.")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ connection attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)
    yield
    logger.info("Closing RabbitMQ connection...")
    try:
        if hasattr(broker, "close"):
            await broker.close()
    except Exception as e:
        logger.error(f"Error closing RabbitMQ connection: {e}")


app = FastAPI(
    title="Support & Ticketing Service",
    description="Role-based helpdesk microservice for Clients and Admins",
    version="1.0.0",
    lifespan=lifespan
)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

app.include_router(tickets_router)


@app.get("/health")
def health_check():
    return {"service": "support-service", "status": "healthy", "db_connected": True}