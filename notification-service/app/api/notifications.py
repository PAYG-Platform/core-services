# backend/notification-service/app/api/notifications.py
import uuid
import math
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Notification
from app.schemas import (
    NotificationResponse,
    PaginatedNotificationsResponse,
    MarkSeenRequest
)
import json
import asyncio
from fastapi.responses import StreamingResponse

from vpn_shared.jwt_auth import verify_jwt_token, get_current_user_payload
from app.core.event_bus import manager

router = APIRouter(prefix="/notifications", tags=["User Notifications"])

@router.get("/stream")
async def sse_stream(request: Request, user: dict = Depends(get_current_user_payload)):
    """
    Maintains a persistent Server-Sent Events (SSE) stream for real-time pushing.
    """
    user_id = str(user["sub"])
    user_role = str(user.get("role", "")).capitalize()
    
    async def event_generator():
        queue = manager.connect(user_id, role=user_role)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # 15s timeout to send a keep-alive ping (keeps Nginx happy)
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {message['event']}\ndata: {json.dumps(message['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            manager.disconnect(user_id, queue, role=user_role)
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/unread-count")
def get_unread_count(
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    """
    Returns unseen notification count for green indicator dot.
    Enforces rate limit of 3 requests per user per minute.
    """
    user_id_str = str(user["sub"])
    
    user_uuid = uuid.UUID(user_id_str)
    unseen_count = db.query(Notification).filter(
        Notification.user_id == user_uuid,
        Notification.is_seen == False
    ).count()
    
    return {"unseen_count": unseen_count}


@router.get("", response_model=PaginatedNotificationsResponse)
@router.get("/", response_model=PaginatedNotificationsResponse)
def get_notifications(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Items per page (default 50)"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    """
    Paginated notifications endpoint (50 per page).
    Enforces rate limit of 3 requests per user per minute.
    """
    user_id_str = str(user["sub"])
    
    user_uuid = uuid.UUID(user_id_str)

    # AUTO-SEEN FOR OLDER THAN 100
    unseen_all = db.query(Notification).filter(
        Notification.user_id == user_uuid,
        Notification.is_seen == False
    ).order_by(Notification.created_at.desc()).all()
    
    if len(unseen_all) > 100:
        old_ids = [n.id for n in unseen_all[100:]]
        db.query(Notification).filter(Notification.id.in_(old_ids)).update(
            {Notification.is_seen: True, Notification.seen_at: datetime.now(timezone.utc)},
            synchronize_session=False
        )
        db.commit()

    query = db.query(Notification).filter(Notification.user_id == user_uuid)
    
    total = query.count()
    total_pages = math.ceil(total / limit) if total > 0 else 1
    offset = (page - 1) * limit

    items = query.order_by(Notification.created_at.desc()).offset(offset).limit(limit).all()
    
    unseen_count = db.query(Notification).filter(
        Notification.user_id == user_uuid,
        Notification.is_seen == False
    ).count()

    return PaginatedNotificationsResponse(
        items=items,
        total=total,
        page=page,
        limit=limit,
        total_pages=total_pages,
        unseen_count=unseen_count
    )


@router.get("/unseen-billing")
def get_unseen_billing_status(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    """Returns whether the client has any unseen billing notifications (topup/refund/adjustment)."""
    user_uuid = uuid.UUID(str(user["sub"]))
    count = db.query(Notification).filter(
        Notification.user_id == user_uuid,
        Notification.category == "billing",
        Notification.is_seen == False
    ).count()
    return {"has_unseen_billing": count > 0, "unseen_count": count}


@router.post("/mark-billing-seen")
def mark_billing_notifications_seen(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    """Marks all unseen billing notifications as seen when client opens Payment Histories page."""
    user_uuid = uuid.UUID(str(user["sub"]))
    now = datetime.now(timezone.utc)
    updated_count = db.query(Notification).filter(
        Notification.user_id == user_uuid,
        Notification.category == "billing",
        Notification.is_seen == False
    ).update({
        Notification.is_seen: True,
        Notification.seen_at: now
    }, synchronize_session=False)
    db.commit()
    return {"message": "Billing notifications marked as seen.", "updated_count": updated_count}


@router.post("/mark-all-seen")
def mark_all_notifications_seen(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    """Marks all unseen notifications as seen for the current user."""
    user_uuid = uuid.UUID(str(user["sub"]))
    now = datetime.now(timezone.utc)
    updated_count = db.query(Notification).filter(
        Notification.user_id == user_uuid,
        Notification.is_seen == False
    ).update({
        Notification.is_seen: True,
        Notification.seen_at: now
    }, synchronize_session=False)
    db.commit()
    return {"message": "All notifications marked as seen.", "updated_count": updated_count}


@router.post("/mark-seen")
def mark_notifications_seen(
    payload: MarkSeenRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    """
    Marks selected notifications as seen (`is_seen = True`).
    Once marked as seen, items cannot revert back to unseen.
    """
    user_uuid = uuid.UUID(str(user["sub"]))
    now = datetime.now(timezone.utc)

    if not payload.notification_ids:
        # Fallback: mark all unseen for this user
        updated_count = db.query(Notification).filter(
            Notification.user_id == user_uuid,
            Notification.is_seen == False
        ).update({
            Notification.is_seen: True,
            Notification.seen_at: now
        }, synchronize_session=False)
        db.commit()
        return {"message": f"Marked {updated_count} notifications as seen.", "updated_count": updated_count}

    updated_count = db.query(Notification).filter(
        Notification.id.in_(payload.notification_ids),
        Notification.user_id == user_uuid,
        Notification.is_seen == False
    ).update({
        Notification.is_seen: True,
        Notification.seen_at: now
    }, synchronize_session=False)

    db.commit()
    return {"message": f"Marked {updated_count} notifications as seen.", "updated_count": updated_count}


@router.get("/unacknowledged", response_model=List[NotificationResponse])
def get_unacknowledged_notices(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    """
    Returns unacknowledged system pop-up notices (e.g., server replacements, unallocated regional credit).
    The frontend will continue rendering the modal until the client marks it acknowledged.
    """
    user_uuid = uuid.UUID(str(user["sub"]))
    return db.query(Notification).filter(
        Notification.user_id == user_uuid,
        Notification.requires_acknowledgement == True,
        Notification.is_acknowledged == False
    ).order_by(Notification.created_at.desc()).all()


@router.post("/{notification_id}/acknowledge")
def acknowledge_notice(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_payload)
):
    """
    Called when the client checks 'I have read and understood' and clicks 'Accept'.
    """
    user_uuid = uuid.UUID(str(user["sub"]))
    notice = db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.user_id == user_uuid
    ).first()

    if not notice:
        raise HTTPException(status_code=404, detail="Notification not found.")

    notice.is_acknowledged = True
    notice.is_seen = True
    notice.seen_at = datetime.now(timezone.utc)
    notice.acknowledged_at = datetime.now(timezone.utc)
    db.commit()

    return {"message": "Notice acknowledged successfully."}