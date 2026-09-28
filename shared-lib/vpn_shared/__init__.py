# shared-lib/vpn_shared/__init__.py
from vpn_shared.events import EventKeys
from vpn_shared.jwt_auth import verify_jwt_token, parse_bearer_header, get_current_user_payload
from vpn_shared.logger import setup_logging
from vpn_shared.schemas import (
    RoleEnum,
    CurrencyEnum,
    UserCreatedEvent,
    TrafficConsumedEvent,
    WalletEmptyEvent,
    WalletFundedEvent,
    ServerOfflineEvent,
    SystemAuditLogEvent
)
from vpn_shared.utils import bytes_to_gb, gb_to_bytes

__all__ = [
    "EventKeys",
    "RoleEnum",
    "CurrencyEnum",
    "setup_logging",
    "verify_jwt_token",
    "parse_bearer_header",
    "get_current_user_payload",
    "UserCreatedEvent",
    "TrafficConsumedEvent",
    "WalletEmptyEvent",
    "WalletFundedEvent",
    "ServerOfflineEvent",
    "SystemAuditLogEvent",
    "bytes_to_gb",
    "gb_to_bytes"
]