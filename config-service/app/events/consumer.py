# backend/config-service/app/events/consumer.py
import logging
from faststream.rabbit import RabbitRouter

logger = logging.getLogger(__name__)
config_router = RabbitRouter()

# FastStream event consumer router prepared for config-service lifecycle.