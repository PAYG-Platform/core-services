# backend/support-service/app/api/tickets.py
import os
import uuid
import math
import logging
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, Request, UploadFile, File, Form, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.events.publisher import publish_ticket_message_event
from app.database import get_db
from app.models import Ticket, TicketMessage
from app.schemas import (
    PaginatedTicketsResponse, 
    GlobalTicketPollResponse, 
    TicketDetailResponse, 
    TicketMessageResponse, 
    TicketResponse
)
from mng_shared.jwt_auth import verify_jwt_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["Support Tickets"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(BASE_DIR)
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
try:
    TICKETS_DIR = os.path.join(UPLOAD_DIR, "tickets")
    os.makedirs(TICKETS_DIR, exist_ok=True)
except Exception:
    UPLOAD_DIR = os.path.join(ROOT_DIR, "uploads")
    TICKETS_DIR = os.path.join(UPLOAD_DIR, "tickets")
    os.makedirs(TICKETS_DIR, exist_ok=True)
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ALLOWED_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
ALLOWED_EXTENSIONS = ALLOWED_IMAGE_EXTS | ALLOWED_VIDEO_EXTS


def get_current_user(request: Request) -> dict:
    cookie_token = request.cookies.get("access_token")
    auth_header = request.headers.get("Authorization")

    token = cookie_token or auth_header
    
    if not token or token in ["null", "undefined", '""', "''"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Authentication token required."
        )

    token_str = token.replace("Bearer ", "").strip().strip('"').strip("'")
    
    payload = verify_jwt_token(token_str)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid or expired token."
        )

    return payload


async def save_uploaded_file(file: Optional[UploadFile]) -> Optional[str]:
    if not file or not file.filename:
        return None

    file_extension = os.path.splitext(file.filename)[1].lower()
    if file_extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image and video files are supported."
        )

    unique_filename = f"{uuid.uuid4().hex}{file_extension}"
    file_path = os.path.join(TICKETS_DIR, unique_filename)

    try:
        await file.seek(0)
        contents = await file.read()
        if not contents or len(contents) == 0:
            logger.warning(f"Uploaded file {file.filename} is 0 bytes.")
            return None

        with open(file_path, "wb") as buffer:
            buffer.write(contents)

        logger.info(f"Saved uploaded media ({len(contents)} bytes) to {file_path}")
        return f"/uploads/tickets/{unique_filename}"
    except Exception as e:
        logger.error(f"Failed to save uploaded file {file.filename}: {e}")
        return None


def normalize_role(role_val: str) -> str:
    if not role_val:
        return "Client"
    return role_val.strip().capitalize()


def check_ticket_unseen(t: Ticket, effective_role: str, user_id: uuid.UUID, db: Session) -> bool:
    if effective_role == "Client":
        return db.query(TicketMessage).filter(
            TicketMessage.ticket_id == t.id,
            func.lower(TicketMessage.sender_role) != "client",
            TicketMessage.is_seen_by_client == False
        ).first() is not None

    elif effective_role == "Admin":
        return db.query(TicketMessage).filter(
            TicketMessage.ticket_id == t.id,
            func.lower(TicketMessage.sender_role) != "admin",
            TicketMessage.is_seen_by_admin == False
        ).first() is not None

    return False


# ==========================================
# ENDPOINTS
# ==========================================

@router.post("/", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    subject: str = Form(..., min_length=3, max_length=255),
    category: str = Form(...),
    content: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    portal_role: Optional[str] = Form(None, description="Portal view perspective"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    if not content and not file:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Ticket must contain either text content or an attachment."
        )

    user_role = normalize_role(portal_role) if portal_role else normalize_role(user.get("role"))
    user_id = uuid.UUID(str(user.get("sub")))

    new_ticket = Ticket(
        creator_id=user_id,
        creator_role=user_role,
        category=category,
        subject=subject,
        status="Open"
    )
    db.add(new_ticket)
    db.flush()

    attachment_path = await save_uploaded_file(file)

    initial_message = TicketMessage(
        ticket_id=new_ticket.id,
        sender_id=user_id,
        sender_role=user_role,
        content=content,
        attachment_url=attachment_path,
        is_seen_by_client=(user_role == "Client"),
        is_seen_by_admin=(user_role == "Admin")
    )
    db.add(initial_message)
    db.commit()
    db.refresh(new_ticket)

    preview = (content[:50] + "...") if content else "Sent a new attachment."
    recipient_role = "Admin" if user_role == "Client" else "Client"
    recipient_id = None if user_role == "Client" else str(new_ticket.creator_id)

    await publish_ticket_message_event(
        recipient_id=recipient_id,
        ticket_id=str(new_ticket.id),
        preview_text=preview,
        recipient_role=recipient_role
    )

    setattr(new_ticket, "has_unseen_messages", False)
    return new_ticket


@router.get("/", response_model=PaginatedTicketsResponse)
def list_tickets(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(10, ge=1, le=100, description="Items per page"),
    portal_role: Optional[str] = Query(None, description="Portal view perspective"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    user_role = normalize_role(user.get("role"))
    user_id = uuid.UUID(str(user.get("sub")))
    effective_role = normalize_role(portal_role) if portal_role else user_role

    query = db.query(Ticket)

    if effective_role == "Client":
        query = query.filter(Ticket.creator_id == user_id, Ticket.creator_role == "Client")

    total = query.count()
    total_pages = math.ceil(total / limit) if total > 0 else 1
    offset = (page - 1) * limit

    tickets = query.order_by(Ticket.updated_at.desc()).offset(offset).limit(limit).all()

    for t in tickets:
        unseen = check_ticket_unseen(t, effective_role, user_id, db)
        setattr(t, "has_unseen_messages", unseen)

    return PaginatedTicketsResponse(
        items=tickets,
        total=total,
        page=page,
        limit=limit,
        total_pages=total_pages
    )


@router.get("/{ticket_id}", response_model=TicketDetailResponse)
def get_ticket_detail(
    ticket_id: uuid.UUID,
    portal_role: Optional[str] = Query(None, description="Portal view perspective"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    user_role = normalize_role(portal_role) if portal_role else normalize_role(user.get("role"))
    user_id = uuid.UUID(str(user.get("sub")))

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    if user_role == "Client" and ticket.creator_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied to this ticket.")

    messages_query = (
        db.query(TicketMessage)
        .filter(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.created_at.desc())
        .limit(20)
        .all()
    )
    messages_query.reverse()

    if user_role == "Client":
        db.query(TicketMessage).filter(
            TicketMessage.ticket_id == ticket_id, 
            TicketMessage.is_seen_by_client == False
        ).update({TicketMessage.is_seen_by_client: True}, synchronize_session=False)

    elif user_role == "Admin":
        db.query(TicketMessage).filter(
            TicketMessage.ticket_id == ticket_id,
            TicketMessage.is_seen_by_admin == False
        ).update({TicketMessage.is_seen_by_admin: True}, synchronize_session=False)

    db.commit()

    return {
        "id": ticket.id,
        "creator_id": ticket.creator_id,
        "creator_role": ticket.creator_role,
        "category": ticket.category,
        "subject": ticket.subject,
        "status": ticket.status,
        "has_unseen_messages": False,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "messages": messages_query
    }


@router.post("/{ticket_id}/messages", response_model=TicketMessageResponse)
async def add_message_reply(
    ticket_id: uuid.UUID,
    content: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    portal_role: Optional[str] = Form(None, description="Portal view perspective"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    if not content and not file:
        raise HTTPException(
            status_code=400, 
            detail="Message must contain text content or an attachment."
        )

    user_role = normalize_role(portal_role) if portal_role else normalize_role(user.get("role"))
    user_id = uuid.UUID(str(user.get("sub")))

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    if user_role == "Client" and ticket.creator_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    if ticket.status.lower() in ["done", "closed"] and user_id == ticket.creator_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This ticket is marked as Done/Closed. As the creator, you cannot send new messages."
        )

    attachment_path = await save_uploaded_file(file)

    new_msg = TicketMessage(
        ticket_id=ticket.id,
        sender_id=user_id,
        sender_role=user_role,
        content=content,
        attachment_url=attachment_path,
        is_seen_by_client=(user_role == "Client"),
        is_seen_by_admin=(user_role == "Admin")
    )

    ticket.updated_at = datetime.now(timezone.utc)
    db.add(new_msg)
    db.commit()
    db.refresh(new_msg)

    if user_role == "Client":
        recipient_id = None
        recipient_role = "Admin"
    else:
        recipient_id = str(ticket.creator_id)
        recipient_role = "Client"

    preview = (content[:50] + "...") if content else "Sent a new attachment."

    msg_data = {
        "id": str(new_msg.id),
        "ticket_id": str(new_msg.ticket_id),
        "sender_id": str(new_msg.sender_id),
        "sender_role": new_msg.sender_role,
        "content": new_msg.content or "",
        "attachment_url": new_msg.attachment_url,
        "created_at": new_msg.created_at.isoformat() if new_msg.created_at else None
    }

    await publish_ticket_message_event(
        recipient_id=recipient_id,
        ticket_id=str(ticket.id),
        preview_text=preview,
        recipient_role=recipient_role,
        message_data=msg_data
    )

    return new_msg


@router.get("/{ticket_id}/messages", response_model=List[TicketMessageResponse])
def poll_ticket_messages(
    ticket_id: uuid.UUID,
    since: Optional[str] = Query(None, description="ISO timestamp to fetch new messages after"),
    offset: int = Query(0, description="Pagination offset"),
    limit: int = Query(20, description="Pagination limit"),
    portal_role: Optional[str] = Query(None, description="Portal view perspective"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    user_role = normalize_role(portal_role) if portal_role else normalize_role(user.get("role"))
    user_id = uuid.UUID(str(user.get("sub")))

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    if user_role == "Client" and ticket.creator_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    query = db.query(TicketMessage).filter(TicketMessage.ticket_id == ticket_id)

    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query = query.filter(TicketMessage.created_at > since_dt)
            messages = query.order_by(TicketMessage.created_at.asc()).all()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid ISO timestamp format for 'since'.")
    else:
        messages = query.order_by(TicketMessage.created_at.desc()).offset(offset).limit(limit).all()
        messages.reverse()

    for msg in messages:
        if user_role == "Client":
            msg.is_seen_by_client = True
        elif user_role == "Admin":
            msg.is_seen_by_admin = True

    db.commit()
    return messages


@router.patch("/{ticket_id}/status", response_model=TicketResponse)
async def update_ticket_status(
    ticket_id: uuid.UUID,
    status_str: str = Form(..., alias="status"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    user_role = normalize_role(user.get("role"))

    if user_role == "Client":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Clients are not authorized to modify ticket status."
        )

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    valid_statuses = ["Open", "In Progress", "Done", "Closed"]
    formatted_status = status_str.strip().title()
    if formatted_status not in valid_statuses:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid status. Choose from: {', '.join(valid_statuses)}"
        )

    ticket.status = formatted_status
    ticket.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ticket)

    preview = f"Ticket status changed to '{formatted_status}'."
    await publish_ticket_message_event(
        recipient_id=str(ticket.creator_id),
        ticket_id=str(ticket.id),
        preview_text=preview,
        recipient_role=ticket.creator_role
    )

    setattr(ticket, "has_unseen_messages", False)
    return ticket


@router.get("/updates/poll", response_model=List[GlobalTicketPollResponse])
def poll_all_tickets_and_messages(
    since: Optional[str] = Query(None, description="ISO timestamp to fetch updates after"),
    portal_role: Optional[str] = Query(None, description="Portal view perspective"),
    admin_scope: Optional[str] = Query("direct", description="'direct' or 'all'"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    user_id_str = str(user.get("sub"))
    user_id = uuid.UUID(user_id_str)
    effective_role = normalize_role(portal_role) if portal_role else normalize_role(user.get("role"))

    query = db.query(Ticket)
    if effective_role == "Client":
        query = query.filter(Ticket.creator_id == user_id, Ticket.creator_role == "Client")

    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query = query.filter(Ticket.updated_at > since_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid ISO timestamp format.")

    tickets = query.order_by(Ticket.updated_at.desc()).limit(50).all()
    results = []

    for t in tickets:
        unseen = check_ticket_unseen(t, effective_role, user_id, db)
        setattr(t, "has_unseen_messages", unseen)
        results.append({
            "ticket": t,
            "last_message": None
        })

    return results