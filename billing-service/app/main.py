# backend/billing-service/app/main.py
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
import asyncio
from datetime import datetime, timezone, timedelta

from app.database import engine, Base
from app.api.topups import router as topups_router
from app.api.refunds import router as refunds_router
from app.api.settings import router as settings_router
from app.api.analytics import router as analytics_router
from app.events.consumer import billing_router
from app.events.publisher import broker
from app.models import WalletBucket, SystemFinancialSettings
from vpn_shared.rate_limiter import RedisRateLimiter
from vpn_shared import EventKeys
from vpn_shared.logger import setup_logging

logger = setup_logging("billing-service")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_DIR, "receipts"), exist_ok=True)
except Exception:
    UPLOAD_DIR = "./uploads"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_DIR, "receipts"), exist_ok=True)

RATE_LIMITER = RedisRateLimiter(
    config_path="/app/config/rate_limits.yml",
    service_name="billing-service",
)

# Attach router at module level so FastStream registers queue bindings before start
broker.include_router(billing_router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Verifying Billing Service database schema and directories...")
    try:
        Base.metadata.create_all(bind=engine)
        
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS username VARCHAR(50);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_accounts_username ON accounts(username);"))
            conn.execute(text("ALTER TABLE transactions ALTER COLUMN receipt_url TYPE TEXT;"))
            conn.execute(text(
                "ALTER TABLE wallet_buckets "
                "ADD COLUMN IF NOT EXISTS empty_since TIMESTAMP WITH TIME ZONE,"
                "ADD COLUMN IF NOT EXISTS last_grace_day_notified INTEGER NOT NULL DEFAULT 0;"
            ))
            conn.execute(text(
                "ALTER TABLE transactions "
                "ADD COLUMN IF NOT EXISTS location_id UUID;"
            ))
            conn.execute(text(
                "ALTER TABLE transactions "
                "ADD COLUMN IF NOT EXISTS reservation_id UUID;"
            ))
            conn.execute(text(
                "ALTER TABLE transactions "
                "ADD COLUMN IF NOT EXISTS reservation_status VARCHAR(30) DEFAULT 'NotRequired';"
            ))
            conn.execute(text(
                "ALTER TABLE system_financial_settings "
                "ADD COLUMN IF NOT EXISTS new_region_min_topup_amount "
                "DOUBLE PRECISION NOT NULL DEFAULT 50000.0;"
            ))
            conn.execute(text(
                "ALTER TABLE system_financial_settings "
                "ADD COLUMN IF NOT EXISTS existing_region_min_topup_amount "
                "DOUBLE PRECISION NOT NULL DEFAULT 10000.0;"
            ))
            conn.execute(text(
                "ALTER TABLE system_financial_settings "
                "ADD COLUMN IF NOT EXISTS region_grace_period_days "
                "INTEGER NOT NULL DEFAULT 3;"
            ))
            conn.execute(text(
                "ALTER TABLE system_financial_settings "
                "ADD COLUMN IF NOT EXISTS refund_cooldown_days "
                "INTEGER NOT NULL DEFAULT 30;"
            ))
            conn.execute(text(
                "ALTER TABLE system_financial_settings "
                "ADD COLUMN IF NOT EXISTS config_reservation_ttl_minutes "
                "INTEGER NOT NULL DEFAULT 30;"
            ))
            conn.execute(text(
                "ALTER TABLE system_financial_settings "
                "ADD COLUMN IF NOT EXISTS default_signup_gift_credit "
                "DOUBLE PRECISION NOT NULL DEFAULT 20000.0;"
            ))
            conn.execute(text(
                "ALTER TABLE system_financial_settings "
                "ADD COLUMN IF NOT EXISTS safety_cutoff_threshold "
                "DOUBLE PRECISION NOT NULL DEFAULT 50.0;"
            ))
            conn.execute(text(
                "ALTER TABLE system_financial_settings "
                "ADD COLUMN IF NOT EXISTS max_negative_balance "
                "DOUBLE PRECISION;"
            ))
            conn.execute(text(
                "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS billing_currency VARCHAR(10) NOT NULL DEFAULT 'IRT';"
            ))
            conn.execute(text(
                "ALTER TABLE wallet_buckets ADD COLUMN IF NOT EXISTS currency VARCHAR(10) NOT NULL DEFAULT 'IRT';"
            ))
            conn.execute(text(
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS currency VARCHAR(10) NOT NULL DEFAULT 'IRT';"
            ))
            conn.execute(text("""
                ALTER TABLE system_financial_settings 
                ADD COLUMN IF NOT EXISTS default_signup_gift_credit_usd DOUBLE PRECISION NOT NULL DEFAULT 0.50,
                ADD COLUMN IF NOT EXISTS new_region_min_topup_amount_usd DOUBLE PRECISION NOT NULL DEFAULT 2.00,
                ADD COLUMN IF NOT EXISTS existing_region_min_topup_amount_usd DOUBLE PRECISION NOT NULL DEFAULT 1.00,
                ADD COLUMN IF NOT EXISTS safety_cutoff_threshold_usd DOUBLE PRECISION NOT NULL DEFAULT 0.01,
                ADD COLUMN IF NOT EXISTS max_negative_balance_usd DOUBLE PRECISION,
                ADD COLUMN IF NOT EXISTS usd_to_irt_exchange_rate DOUBLE PRECISION NOT NULL DEFAULT 90000.0;
            """))

        logger.info("Billing Service database tables verified successfully.")
    except Exception as e:
        logger.error(f"Error during schema verification: {e}")

    logger.info("Connecting Billing Service to RabbitMQ and starting consumers...")
    for attempt in range(1, 11):
        try:
            await broker.start()
            logger.info("Successfully started RabbitMQ Broker and consumers in Billing Service.")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ startup attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)
    
    # FAST GRACE PERIOD EXPIRATION & CONFIG RELEASE ENGINE (EVALUATES EVERY 60 SECONDS)
    async def periodic_check_expired_buckets():
        from app.database import SessionLocal
        while True:
            try:
                await asyncio.sleep(60)  # EVALUATES PROMPTLY EVERY 60 SECONDS INSTEAD OF 30 MINUTES
                db = SessionLocal()
                try:
                    settings = db.query(SystemFinancialSettings).first()
                    grace_days = int(settings.region_grace_period_days) if (settings and settings.region_grace_period_days is not None) else 3
                    now = datetime.now(timezone.utc)
                    cutoff = now - timedelta(days=grace_days)

                    from app.events.publisher import publish_grace_period_warning
                    from app.models import Account

                    # 1. AUTO-TIMESTAMP ANY EMPTY BUCKET LACKING AN EMPTY_SINCE RECORD
                    db.query(WalletBucket).filter(
                        WalletBucket.is_active == True,
                        WalletBucket.balance <= 0,
                        WalletBucket.empty_since == None
                    ).update({WalletBucket.empty_since: now}, synchronize_session=False)

                    # Reset empty_since and grace tracking if user has refilled
                    db.query(WalletBucket).filter(
                        WalletBucket.is_active == True,
                        WalletBucket.balance > 0,
                        WalletBucket.empty_since != None
                    ).update({
                        WalletBucket.empty_since: None,
                        WalletBucket.last_grace_day_notified: 0
                    }, synchronize_session=False)
                    db.commit()

                    # 2. EVALUATE USERS WHOSE BUCKETS ARE ALL EMPTY
                    active_buckets = db.query(WalletBucket).filter(
                        WalletBucket.is_active == True
                    ).all()

                    users_checked = set()
                    for b in active_buckets:
                        uid = b.user_id
                        if uid in users_checked:
                            continue
                        users_checked.add(uid)

                        user_buckets = [ub for ub in active_buckets if ub.user_id == uid]
                        
                        # User still has balance in a bucket -> DO NOT RELEASE OR WARN
                        if any(float(ub.balance) > 0 for ub in user_buckets):
                            continue

                        valid_empty_times = [
                            (ub.empty_since.replace(tzinfo=timezone.utc) if ub.empty_since.tzinfo is None else ub.empty_since)
                            for ub in user_buckets if ub.empty_since
                        ]
                        if not valid_empty_times:
                            continue

                        earliest_empty_since = min(valid_empty_times)
                        elapsed_seconds = (now - earliest_empty_since).total_seconds()
                        elapsed_days = int(elapsed_seconds // 86400)

                        all_expired = (now - earliest_empty_since) >= timedelta(days=grace_days)

                        if all_expired:
                            for ub in user_buckets:
                                ub.is_active = False

                            # EMIT EVENT TO PURGE CONFIGS FROM 3X-UI AND RECLAIM CAPACITY
                            await broker.publish({
                                "user_id": str(uid),
                                "location_id": None
                            }, queue=EventKeys.CONFIG_RELEASED)
                            logger.info(f"[GracePeriod] Reclaimed configs for User {uid}. Grace period ({grace_days}d) expired.")
                        else:
                            # 3. DAILY GRACE PERIOD REMINDERS (DAY 1, DAY 2, etc.)
                            max_notified_day = max((getattr(ub, "last_grace_day_notified", 0) or 0) for ub in user_buckets)
                            current_grace_day = elapsed_days

                            if current_grace_day > 0 and current_grace_day < grace_days and max_notified_day < current_grace_day:
                                for ub in user_buckets:
                                    ub.last_grace_day_notified = current_grace_day

                                days_remaining = max(1, grace_days - current_grace_day)
                                user_acc = db.query(Account).filter(Account.user_id == uid).first()
                                user_curr = str(getattr(user_acc, "billing_currency", "IRT") or "IRT").upper()

                                await publish_grace_period_warning(
                                    user_id=str(uid),
                                    grace_day=current_grace_day,
                                    grace_days_total=grace_days,
                                    days_remaining=days_remaining,
                                    currency=user_curr,
                                )

                    db.commit()
                except Exception as db_err:
                    db.rollback()
                    logger.error(f"Error during expired bucket release check: {db_err}")
                finally:
                    db.close()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in periodic bucket checker: {e}")

    cleanup_task = asyncio.create_task(periodic_check_expired_buckets())

    yield

    cleanup_task.cancel()
    
    logger.info("Disconnecting from RabbitMQ Broker...")
    try:
        if hasattr(broker, "close"):
            await broker.close()
    except Exception as e:
        logger.error(f"Error disconnecting RabbitMQ Broker: {e}")


app = FastAPI(
    title="Billing & Finance Service",
    description="Multi-bucket wallet ledger and PAYG deductions service",
    version="1.0.0",
    lifespan=lifespan
)

app.middleware("http")(RATE_LIMITER.middleware)

# Mount local uploads directory so receipts can be served statically
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Include Active Routers
app.include_router(topups_router)
app.include_router(refunds_router)
app.include_router(settings_router)
app.include_router(analytics_router)


@app.get("/health")
def health_check():
    return {"service": "billing-service", "status": "operational", "db_connected": True}