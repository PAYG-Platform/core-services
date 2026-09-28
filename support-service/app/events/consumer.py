# support-service/app/events/consumer.py
import uuid
import logging
from datetime import datetime, timezone
from faststream.rabbit import RabbitRouter

from app.database import SessionLocal
from app.models import Ticket, TicketMessage
from app.events.publisher import publish_ticket_message_event
from vpn_shared.events import EventKeys

logger = logging.getLogger(__name__)
support_consumer_router = RabbitRouter()


@support_consumer_router.subscriber(EventKeys.AUTO_SUPPORT_TICKET_REQUESTED)
async def handle_auto_ticket_requested(msg: dict):
    """
    Consumes automatic ticket dispatch events when a user encounters blocked routes
    and no working alternative Host/SNI could be provisioned.
    """
    raw_creator_id = msg.get("creator_id")
    subject = msg.get("subject", "Blocked Route Replacement Unavailable")
    content = msg.get("content", "Automated system notice.")

    if not raw_creator_id:
        return

    creator_id = uuid.UUID(str(raw_creator_id))
    db = SessionLocal()
    try:
        new_ticket = Ticket(
            id=uuid.uuid4(),
            creator_id=creator_id,
            creator_role="Client",
            category="Server Issue",
            subject=subject,
            status="Open"
        )
        db.add(new_ticket)
        db.flush()

        initial_message = TicketMessage(
            id=uuid.uuid4(),
            ticket_id=new_ticket.id,
            sender_id=creator_id,
            sender_role="Client",
            content=content,
            is_seen_by_client=True,
            is_seen_by_admin=False
        )
        db.add(initial_message)
        db.commit()

        region = msg.get("region") or "the selected region"
        isp = msg.get("isp") or "your ISP"
        ticket_code = f"#T-{str(new_ticket.id)[:4].upper()}"

        user_notif_title = "📡 Route Investigation Ticket Dispatched"
        user_notif_body = (
            f"All current routes for '{region}' are experiencing DPI filtering on your ISP ({isp}). "
            f"An urgent ticket ({ticket_code}) has been automatically assigned to our engineering team to deploy a new host/SNI for you."
        )

        # 1. Notify Admins in real time
        await publish_ticket_message_event(
            recipient_id=None,
            ticket_id=str(new_ticket.id),
            preview_text=f"Urgent route shortage: {subject}",
            recipient_role="Admin"
        )

        # 2. Notify the Client directly (Web in-app inbox, SSE, and Telegram)
        await publish_ticket_message_event(
            recipient_id=str(creator_id),
            ticket_id=str(new_ticket.id),
            preview_text=user_notif_body,
            recipient_role="Client",
            message_data={
                "title": user_notif_title,
                "is_auto_ticket": True,
                "ticket_code": ticket_code,
                "region": region,
                "isp": isp
            }
        )

        logger.info(f"Automatically created support ticket {ticket_code} for user {creator_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed creating automated support ticket: {e}")
    finally:
        db.close()