# backend/iam-service/app/events/consumer.py
import os
import logging
from faststream.rabbit import RabbitRouter
from app.database import SessionLocal
from app.models import IamSettings
from mng_shared.events import EventKeys

logger = logging.getLogger(__name__)
iam_router = RabbitRouter()

_current_cooldown_minutes: int = int(os.getenv("CLIENT_STATUS_COOLDOWN_MINUTES", "60"))


def get_current_cooldown_minutes(db=None) -> int:
    global _current_cooldown_minutes
    if db:
        try:
            row = db.query(IamSettings).filter(IamSettings.key == "client_status_cooldown_minutes").first()
            if row and row.value is not None:
                _current_cooldown_minutes = max(0, int(row.value))
                return _current_cooldown_minutes
        except Exception:
            pass
    return _current_cooldown_minutes


def set_current_cooldown_minutes(val: int):
    global _current_cooldown_minutes
    _current_cooldown_minutes = max(0, int(val))


def load_local_cooldown(db) -> int:
    global _current_cooldown_minutes
    try:
        row = db.query(IamSettings).filter(IamSettings.key == "client_status_cooldown_minutes").first()
        if row and row.value is not None:
            _current_cooldown_minutes = max(0, int(row.value))
            return _current_cooldown_minutes
    except Exception:
        pass
    _current_cooldown_minutes = max(0, int(os.getenv("CLIENT_STATUS_COOLDOWN_MINUTES", "60")))
    return _current_cooldown_minutes


@iam_router.subscriber(EventKeys.CONFIG_SETTINGS_CHANGED)
async def handle_config_settings_changed(msg: dict):
    raw_cooldown = msg.get("client_status_cooldown_minutes")
    if raw_cooldown is not None:
        try:
            val = max(0, int(raw_cooldown))
            set_current_cooldown_minutes(val)
            
            db = SessionLocal()
            try:
                row = db.query(IamSettings).filter(IamSettings.key == "client_status_cooldown_minutes").first()
                if not row:
                    db.add(IamSettings(key="client_status_cooldown_minutes", value=str(val)))
                else:
                    row.value = str(val)
                db.commit()
                logger.info(f"IAM Service updated client_status_cooldown_minutes={val}m via event.")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error persisting cooldown setting: {e}")