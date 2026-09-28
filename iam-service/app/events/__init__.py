# backend/iam-service/app/events/__init__.py
from app.events.publisher import (
    broker,
    publish_user_created_event,
    publish_connection_processed_event
)
from app.events.consumer import iam_router

__all__ = [
    "broker",
    "iam_router",
    "publish_user_created_event",
    "publish_connection_processed_event"
]