# backend/support-service/app/models.py
import enum
import uuid
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

# IMPORT SHARED Base FROM app.database
from app.database import Base


class TicketStatusEnum(str, enum.Enum):
    OPEN = "Open"
    IN_PROGRESS = "In Progress"
    DONE = "Done"
    CLOSED = "Closed"


class TicketCategoryEnum(str, enum.Enum):
    SERVER_ISSUE = "Server Issue"
    BILLING = "Billing"
    GENERAL = "General"


class BaseModel(Base):
    """Abstract base model providing timezone-aware created_at and updated_at fields."""
    __abstract__ = True
    
    created_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now(), 
        nullable=False
    )


class Ticket(BaseModel):
    """Represents a support ticket thread directly between Client and Admin."""
    __tablename__ = "tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    creator_id = Column(UUID(as_uuid=True), nullable=False, index=True)  # IAM User UUID
    creator_role = Column(String(20), nullable=False)                    # 'Client' or 'Admin'
    
    category = Column(String(50), nullable=False, index=True)            # 'Server Issue', 'Billing', 'General'
    subject = Column(String(255), nullable=False)
    status = Column(
        String(20), 
        default=TicketStatusEnum.OPEN.value, 
        nullable=False, 
        index=True
    )

    # Relationships
    messages = relationship(
        "TicketMessage", 
        back_populates="ticket", 
        order_by="TicketMessage.created_at",
        cascade="all, delete-orphan"
    )


class TicketMessage(BaseModel):
    """Represents an individual message (text, image, video, or file) inside a ticket thread."""
    __tablename__ = "ticket_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    ticket_id = Column(
        UUID(as_uuid=True), 
        ForeignKey("tickets.id", ondelete="CASCADE"), 
        nullable=False, 
        index=True
    )
    sender_id = Column(UUID(as_uuid=True), nullable=False, index=True)   # IAM User UUID
    sender_role = Column(String(20), nullable=False)                     # 'Client' or 'Admin'
    
    content = Column(Text, nullable=True)                                # Text body
    attachment_url = Column(String(500), nullable=True)                  # Path/URL to uploaded media or file
    
    # Read/Unread tracking flags for real-time polling updates
    is_seen_by_admin = Column(Boolean, default=False, nullable=False)
    is_seen_by_client = Column(Boolean, default=False, nullable=False)

    # Relationships
    ticket = relationship("Ticket", back_populates="messages")