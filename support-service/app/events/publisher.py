import os
import logging
from typing import Optional
from faststream.rabbit import RabbitBroker
from mng_shared import EventKeys

logger = logging.getLogger(__name__)
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://admin:supersecret@rabbitmq:5672/")
broker = RabbitBroker(RABBITMQ_URL)

async def publish_ticket_message_event(
    recipient_id: Optional[str], 
    ticket_id: str, 
    preview_text: str,
    recipient_role: str = "Client",
    message_data: Optional[dict] = None
):
    payload = {
        "recipient_id": str(recipient_id) if recipient_id else None,
        "recipient_role": recipient_role,
        "ticket_id": str(ticket_id),
        "preview_text": preview_text,
        "message_data": message_data
    }
    try:
        await broker.publish(payload, queue=EventKeys.TICKET_MESSAGE_CREATED)
        logger.info(f"Published ticket message event for recipient {recipient_id} ({recipient_role})")
    except Exception as e:
        logger.error(f"Failed to publish ticket event: {e}")