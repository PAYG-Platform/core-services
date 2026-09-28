# support-service/app/schemas.py
import uuid
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class TicketMessageResponse(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    sender_id: uuid.UUID
    sender_role: str
    content: Optional[str] = None
    attachment_url: Optional[str] = None
    is_seen_by_client: bool
    is_seen_by_admin: bool
    created_at: datetime

    class Config:
        from_attributes = True


class TicketResponse(BaseModel):
    id: uuid.UUID
    creator_id: uuid.UUID
    creator_role: str
    category: str
    subject: str
    status: str
    has_unseen_messages: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PaginatedTicketsResponse(BaseModel):
    items: List[TicketResponse]
    total: int
    page: int
    limit: int
    total_pages: int


class TicketDetailResponse(TicketResponse):
    messages: List[TicketMessageResponse] = []


class GlobalTicketPollResponse(BaseModel):
    ticket: TicketResponse
    last_message: Optional[TicketMessageResponse] = None